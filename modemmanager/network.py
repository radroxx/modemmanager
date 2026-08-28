"""Выбор оператора и watchdog регистрации.

Часть обслуживания модема, отвечающая за то, к какой сети он подключён:

- при запуске применяется принудительный выбор, если он задан в настройках;
  иначе включается автоматический;
- пользователь запускает поиск сетей отдельной операцией -- в регулярный опрос
  скан не входит: он выводит модем из сети на десятки секунд, всё это время
  сообщения не принимаются;
- watchdog отслеживает, если модем не может зарегистрироваться дольше заданного
  времени, и уведомляет администратора. Автоматически переключаться на
  автоматический выбор система не будет: если пользователь задал оператора,
  значит того и хочет.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .at.errors import AtError
from .behaviors.base import Kind, Unsolicited
from .config import SettingsStore
from .events import EventType
from .modem import Modem, ModemStatus
from .values import NetworkCandidate, RegistrationState

log = logging.getLogger(__name__)


class ScanBusy(RuntimeError):
    """Скан уже идёт: параллельно запускать нельзя."""


@dataclass
class ScanResult:
    """Результат поиска доступных сетей."""

    candidates: list[NetworkCandidate] = field(default_factory=list)
    started_at: float = 0.0
    duration: float = 0.0

    def public_dict(self) -> dict[str, Any]:
        return {
            "candidates": [
                {
                    "plmn": item.plmn,
                    "name": item.name,
                    "status": item.status,
                    "technology": item.technology,
                    "forbidden": item.forbidden,
                }
                for item in self.candidates
            ],
            "started_at": self.started_at,
            "duration": self.duration,
        }


class NetworkService:
    """Управление выбором оператора и watchdog регистрации."""

    def __init__(self, store: SettingsStore):
        self.store = store
        self.modem: Modem | None = None
        self.last_scan: ScanResult | None = None
        self._scan_lock = asyncio.Lock()
        #: Момент, когда модем последний раз был замечен без регистрации.
        #: ``0.0`` -- либо регистрация есть, либо ещё не проверяли.
        self._no_service_since: float = 0.0
        #: Ключ последнего заявленного «нет регистрации» уведомления. Пока
        #: значение совпадает, повторять не нужно (ковыряется дедупом самого
        #: notifier'а тоже, но проверять на своей стороне дешевле).
        self._alerted_plmn: str = ""
        #: Последний применённый принудительный выбор (для 9.2).
        self._applied_plmn: str = ""

    # ------------------------------------------------------------- жизненный цикл

    async def start(self, modem: Modem) -> None:
        self.modem = modem
        await self.apply_operator()

    async def stop(self) -> None:
        self.modem = None

    async def handle(self, unsolicited: Unsolicited) -> None:
        # ``+CREG:``/``+CGREG:`` уже вызвали перечитывание регистрации в Modem.
        # Здесь ничего дополнительного не делаем: watchdog работает в poll.
        return None

    async def poll(self) -> None:
        await self._watchdog()

    # ------------------------------------------------------ применение оператора

    async def apply_operator(self) -> None:
        """Применяет принудительный выбор оператора из настроек.

        Вызывается при старте и вручную, когда настройки SIM изменились --
        например, из веб-интерфейса. Без IMSI применять некуда: настройки
        ключуются по нему.
        """
        assert self.modem is not None
        if self.modem.state.status is ModemStatus.SCANNING:
            # Во время скана модем и так вне сети; выбор применится после его
            # завершения.
            return
        imsi = self.modem.state.imsi
        if not imsi:
            return
        plmn = self.store.settings.sim(imsi).plmn.strip()
        try:
            if plmn:
                await self.modem.session.execute(
                    f'AT+COPS=1,2,"{plmn}"', timeout=30.0
                )
                self._applied_plmn = plmn
            else:
                await self.modem.session.execute("AT+COPS=0", timeout=30.0)
                self._applied_plmn = ""
        except AtError as exc:
            log.warning(
                "%s: не удалось применить выбор оператора (%s)",
                self.modem.usb_path,
                exc,
            )

    @property
    def applied_plmn(self) -> str:
        return self._applied_plmn

    # --------------------------------------------------------------- скан сетей

    async def scan(self, timeout: float = 120.0) -> ScanResult:
        """Запускает поиск доступных сетей.

        Отдельная операция с увеличенным таймаутом и своим состоянием модема;
        параллельно с регулярным опросом не выполняется. Событие с результатом
        уходит в журнал независимо от того, был ли скан запущен из интерфейса
        или как-то ещё.
        """
        assert self.modem is not None
        if self._scan_lock.locked():
            raise ScanBusy("скан уже выполняется")
        async with self._scan_lock:
            previous_status = self.modem.state.status
            self.modem.state.status = ModemStatus.SCANNING
            started = time.time()
            monotonic_started = time.monotonic()
            try:
                candidates = await self.modem.behavior.scan_networks(
                    self.modem.session, timeout=timeout
                )
            finally:
                self.modem.state.status = previous_status
            duration = time.monotonic() - monotonic_started
            result = ScanResult(
                candidates=candidates, started_at=started, duration=duration
            )
            self.last_scan = result
            await self.modem.bus.publish(
                self.modem.event(EventType.SCAN_RESULT, result.public_dict())
            )
            # После скана заданный оператор мог быть сброшен модемом --
            # применяем его заново.
            await self.apply_operator()
            return result

    # -------------------------------------------------- watchdog регистрации

    async def _watchdog(self) -> None:
        """Уведомляет о длительном отсутствии регистрации при заданном операторе."""
        assert self.modem is not None
        if self.modem.state.status is ModemStatus.SCANNING:
            self._no_service_since = 0.0
            return

        imsi = self.modem.state.imsi
        if not imsi:
            return
        plmn = self.store.settings.sim(imsi).plmn.strip()
        if not plmn:
            # Без заданного оператора о недоступности говорить нечего --
            # автоматический выбор сам подбирает сеть.
            self._no_service_since = 0.0
            return

        registration = self.modem.state.registration
        registered = registration.usable_for_sms or registration.data in (
            RegistrationState.REGISTERED,
            RegistrationState.ROAMING,
        )
        now = time.monotonic()
        if registered:
            self._no_service_since = 0.0
            self._alerted_plmn = ""
            return

        if self._no_service_since == 0.0:
            self._no_service_since = now
            return

        threshold = self.store.settings.intervals.no_service_alert
        if now - self._no_service_since < threshold:
            return
        if self._alerted_plmn == plmn:
            return
        self._alerted_plmn = plmn
        await self.modem.bus.publish(
            self.modem.event(
                EventType.NO_SERVICE,
                {
                    "operator": plmn,
                    "duration": now - self._no_service_since,
                    "voice": registration.voice.value,
                    "data": registration.data.value,
                },
            )
        )
