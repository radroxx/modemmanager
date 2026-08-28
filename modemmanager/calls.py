"""Обслуживание входящих голосовых вызовов одного модема.

Часть обслуживания модема (``Component``). Логика от одного вызова к событию:

    t=0.0   +RING  ------> открыть окно ожидания CLIP, запустить таймер
    t=0.1   +CLIP  ------> записать номер, погасить таймер, отклонить,
                          опубликовать событие
    t=1.9   RING   ------> дубль в пределах окна: молча игнорировать
    таймер  --------> номер не пришёл: пометить hidden, отклонить, опубликовать
    +CEND  --------> вызывающий сам положил трубку: пометить cancelled, событие,
                     отклонения не пытаемся
    после   RING   ------> новый вызов, новое окно и новое событие

Отправка ответа на вызов запрещена конструктивно (``at.guard._FORBIDDEN`` знает
про ATA); отклонение бесплатно (``ATH`` и ``AT+CHUP`` в списке разрешённых
исключений).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .at.errors import AtError
from .behaviors.base import Kind, Unsolicited
from .config import CallSettings, SettingsStore
from .events import EventType
from .modem import Modem

log = logging.getLogger(__name__)


@dataclass
class _Call:
    """Внутреннее состояние текущего входящего вызова."""

    started_at: float
    number: str | None = None
    number_hidden: bool = False
    #: Флаг, что событие о вызове уже опубликовано.
    published: bool = False
    #: Флаг, что отклонение уже пробовалось.
    hangup_attempted: bool = False
    reject_error: str = ""
    ended_by_caller: bool = False
    #: Таймер ожидания CLIP.
    timer: asyncio.Task | None = None
    #: Момент, до которого RING считается повтором и не открывает новый вызов.
    dedup_until: float = 0.0


class CallService:
    """Часть обслуживания модема, отвечающая за входящие вызовы."""

    def __init__(self, store: SettingsStore):
        self.store = store
        self.modem: Modem | None = None
        self._current: _Call | None = None
        #: Итог последнего вызова (для тестов и диагностики).
        self.last_event: dict[str, Any] | None = None

    # ------------------------------------------------------------- жизненный цикл

    async def start(self, modem: Modem) -> None:
        self.modem = modem

    async def stop(self) -> None:
        current, self._current = self._current, None
        if current is not None and current.timer is not None:
            current.timer.cancel()
        self.modem = None

    async def poll(self) -> None:
        return None

    async def handle(self, unsolicited: Unsolicited) -> None:
        if unsolicited.kind == Kind.RING:
            await self._on_ring()
        elif unsolicited.kind == Kind.CALLER_ID:
            number = unsolicited.data.get("number") or ""
            hidden = bool(unsolicited.data.get("hidden"))
            await self._on_clip(number, hidden)
        elif unsolicited.kind == Kind.CALL_ENDED:
            await self._on_call_ended(unsolicited)

    # -------------------------------------------------------------- обработчики

    async def _on_ring(self) -> None:
        """RING открывает окно ожидания CLIP -- если ещё не открыто."""
        assert self.modem is not None
        current = self._current
        now = time.monotonic()
        if current is not None:
            if not current.published or now < current.dedup_until:
                # Повторный сигнал уже известного вызова -- ничего не делаем.
                return
            # Прежний вызов уже закрыт: следующий RING -- новый вызов.
            self._current = None
        wait = self._settings().clip_wait
        call = _Call(started_at=now)
        call.timer = asyncio.create_task(
            self._wait_for_clip(call, wait),
            name=f"call-clip-wait:{self.modem.usb_path}",
        )
        self._current = call

    async def _on_clip(self, number: str, hidden: bool) -> None:
        current = self._current
        if current is None or current.published:
            return
        current.number = number or None
        current.number_hidden = hidden or not number
        self._cancel_timer(current)
        await self._finish(current, reason_after_number=True)

    async def _on_call_ended(self, unsolicited: Unsolicited) -> None:
        """Вызывающий сам положил трубку: событие есть, отклонять некого."""
        current = self._current
        if current is None or current.published:
            return
        current.ended_by_caller = True
        self._cancel_timer(current)
        await self._finish(current, reason_after_number=False)

    async def _wait_for_clip(self, call: _Call, wait: float) -> None:
        """Ждёт CLIP; по истечении -- отклоняет вызов с пометкой ``hidden``.

        Не пытается сам себя погасить: ``call.timer`` очищается до ``_finish``,
        чтобы более поздний обработчик не отменил уже отработавший таймер.
        """
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return
        if call is not self._current or call.published:
            return
        call.timer = None  # отменять больше нечего -- мы это и есть
        call.number_hidden = True
        await self._finish(call, reason_after_number=False)

    def _cancel_timer(self, call: _Call) -> None:
        """Гасит ожидающий CLIP таймер, если он ещё не отработал.

        Вынесено отдельно, потому что ``asyncio.Task.cancel`` из внутри самого
        таймера отменил бы текущий вызов и оборвал бы публикацию события.
        """
        timer, call.timer = call.timer, None
        if timer is not None and not timer.done():
            timer.cancel()

    async def _finish(self, call: _Call, *, reason_after_number: bool) -> None:
        """Отклоняет вызов (если это ещё имеет смысл) и публикует событие."""
        assert self.modem is not None
        if call.published:
            return

        if not call.ended_by_caller:
            # Отклонить нужно всегда, кроме случая, когда вызывающий сам ушёл.
            await self._hangup(call)

        payload = self._build_payload(call, after_number=reason_after_number)
        call.published = True
        call.dedup_until = time.monotonic() + self._settings().ring_dedup
        self.last_event = payload
        await self.modem.bus.publish(self.modem.event(EventType.CALL, payload))

    async def _hangup(self, call: _Call) -> None:
        assert self.modem is not None
        call.hangup_attempted = True
        try:
            await self.modem.session.execute("AT+CHUP", timeout=5.0)
        except AtError as exc:
            call.reject_error = str(exc)
            log.info("%s: отклонение вызова не удалось (%s)", self.modem.usb_path, exc)

    def _build_payload(self, call: _Call, *, after_number: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "number": call.number or "",
            "hidden": call.number_hidden,
        }
        if call.ended_by_caller:
            payload["outcome"] = "ended_by_caller"
        elif call.reject_error:
            payload["outcome"] = "reject_failed"
            payload["reject_error"] = call.reject_error
        else:
            payload["outcome"] = "rejected"
        payload["known_number"] = bool(call.number)
        payload["decision"] = "number_received" if after_number else "timeout"
        return payload

    def _settings(self) -> CallSettings:
        return self.store.settings.calls
