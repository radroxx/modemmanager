"""Обслуживание приёма SMS для одного модема.

Часть обслуживания модема (``Component``): реагирует на уведомления о новых
сообщениях, читает их и немедленно освобождает память, ведёт сборку многочастных
и публикует результат на шине. Отправка сообщений не поддерживается совсем -- см.
запрет на уровне транспорта в ``at.guard``.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any

from ..at.errors import AtError
from ..behaviors.base import Kind, Unsolicited
from ..config import SettingsStore
from ..events import EventType
from ..modem import Modem
from .assembly import Assembled, Assembler
from .pdu import Deliver, PduError, parse_deliver

log = logging.getLogger(__name__)


#: Как часто проверять сборку на истёкшие группы. Отдельный интервал --
#: события неполноты не должны опаздывать дольше, чем сама сборка ждёт.
_EXPIRE_CHECK_INTERVAL = 30.0


class SmsService:
    """Часть обслуживания модема: приём SMS."""

    def __init__(
        self,
        store: SettingsStore,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.modem: Modem | None = None
        self.assembler = Assembler(
            timeout=store.settings.sms.assembly_timeout,
            clock=clock,
            wall_clock=wall_clock,
        )
        self._clock = clock
        self._next_expire: float = 0.0
        #: Индексы, для которых не удалось прочитать сообщение (журнал уже написан).
        #: Хранится, чтобы то же самое уведомление не считалось ещё раз.
        self._failed_reads: set[int] = set()

    # ------------------------------------------------------------- жизненный цикл

    async def start(self, modem: Modem) -> None:
        self.modem = modem
        # Двоичный режим и уведомления уже установлены поведением через
        # ``initialise``. Здесь мы только выгребаем то, что накопилось за время
        # простоя приложения (см. spec, 6.3): память сообщений нельзя оставлять
        # занятой -- это одна из вероятных причин молчаливой потери SMS.
        await self._drain_backlog()

    async def stop(self) -> None:
        self.modem = None

    async def handle(self, unsolicited: Unsolicited) -> None:
        if unsolicited.kind != Kind.SMS_STORED:
            return
        index = unsolicited.data.get("index")
        if not isinstance(index, int):
            return
        await self._read_and_publish(index)

    async def poll(self) -> None:
        """Проверяет, не пора ли снять группы сборки по таймауту."""
        now = self._clock()
        if now < self._next_expire:
            return
        self._next_expire = now + _EXPIRE_CHECK_INTERVAL
        for assembled in self.assembler.expire():
            await self._publish_assembled(assembled)

    # ---------------------------------------------------------------- операции

    async def _drain_backlog(self) -> None:
        """Читает и удаляет всё, что накопилось в памяти сообщений.

        Каждое сообщение сначала удаляется, потом отдаётся в сборку и в
        уведомление -- порядок такой же, как для сообщений, пришедших по
        уведомлению (см. spec, 6.4).
        """
        assert self.modem is not None
        try:
            response = await self.modem.session.execute("AT+CMGL=4", timeout=15.0)
        except AtError as exc:
            log.info("%s: выгребание сообщений не удалось (%s)", self.modem.usb_path, exc)
            return
        for index, pdu_hex in _parse_cmgl(response.text):
            await self._delete(index)
            await self._process(index, pdu_hex)

    async def _read_and_publish(self, index: int) -> None:
        assert self.modem is not None
        try:
            response = await self.modem.session.execute(f"AT+CMGR={index}", timeout=10.0)
        except AtError as exc:
            log.warning(
                "%s: сообщение %d не прочитано (%s)", self.modem.usb_path, index, exc
            )
            self._failed_reads.add(index)
            return
        pdu_hex = _parse_cmgr(response.text)
        if not pdu_hex:
            log.warning(
                "%s: ответ на AT+CMGR=%d не содержит PDU (%r)",
                self.modem.usb_path,
                index,
                response.text,
            )
            return
        # Освобождаем память ДО сборки и до публикации события. Даже если
        # сборка выкинет исключение, память уже свободна: следующее сообщение
        # придёт нормально.
        await self._delete(index)
        await self._process(index, pdu_hex)

    async def _delete(self, index: int) -> None:
        assert self.modem is not None
        try:
            await self.modem.session.execute(f"AT+CMGD={index}", timeout=10.0)
        except AtError as exc:
            log.warning(
                "%s: удаление сообщения %d не удалось (%s)",
                self.modem.usb_path,
                index,
                exc,
            )

    async def _process(self, index: int, pdu_hex: str) -> None:
        assert self.modem is not None
        try:
            deliver = parse_deliver(pdu_hex)
        except PduError as exc:
            log.warning(
                "%s: PDU %d не разобран (%s)", self.modem.usb_path, index, exc
            )
            return
        imsi = self.modem.state.imsi
        assembled = self.assembler.add(imsi, deliver, raw=pdu_hex)
        if assembled is not None:
            await self._publish_assembled(assembled)

    # ---------------------------------------------------------------- события

    async def _publish_assembled(self, assembled: Assembled) -> None:
        assert self.modem is not None
        payload: dict[str, Any] = {
            "from": assembled.sender.display,
            "from_ton": assembled.sender.ton,
            "text": assembled.text,
            "encoding": assembled.encoding.value,
            "raw": list(assembled.raw_pdus),
            "parts_total": assembled.key.total,
            "parts_present": list(assembled.parts_present),
        }
        if assembled.incomplete:
            payload["incomplete"] = True
            payload["missing"] = list(assembled.parts_missing)
        self.modem.state.last_sms = assembled.timestamp
        await self.modem.bus.publish(self.modem.event(EventType.SMS, payload))


# ----------------------------------------------------- разбор ответов модема

_CMGR_HEADER = re.compile(r"^\+CMGR:\s*(\d+)\s*,\s*[^,]*\s*,\s*(\d+)", re.IGNORECASE)
_CMGL_HEADER = re.compile(
    r"^\+CMGL:\s*(\d+)\s*,\s*\d+\s*,\s*[^,]*\s*,\s*(\d+)",
    re.IGNORECASE,
)
_PDU_LINE = re.compile(r"^[0-9A-Fa-f]+$")


def _parse_cmgr(text: str) -> str:
    """Извлекает PDU из ответа на ``AT+CMGR``.

    Заголовок несёт длину, PDU идёт в следующей строке; всё, что не hex, --
    отбрасывается.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if _CMGR_HEADER.match(line):
            for candidate in lines[index + 1 :]:
                if _PDU_LINE.match(candidate):
                    return candidate
    return ""


def _parse_cmgl(text: str) -> list[tuple[int, str]]:
    """Извлекает пары (индекс, PDU) из ответа на ``AT+CMGL``."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    result: list[tuple[int, str]] = []
    pos = 0
    while pos < len(lines):
        match = _CMGL_HEADER.match(lines[pos])
        if not match:
            pos += 1
            continue
        index = int(match.group(1))
        # PDU может идти не сразу следующей строкой, если модем впихнул пустоту.
        pdu = ""
        pos += 1
        while pos < len(lines):
            candidate = lines[pos]
            if _CMGL_HEADER.match(candidate):
                break
            if _PDU_LINE.match(candidate):
                pdu = candidate
                pos += 1
                break
            pos += 1
        if pdu:
            result.append((index, pdu))
    return result
