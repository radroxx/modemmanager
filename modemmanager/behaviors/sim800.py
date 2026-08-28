"""Поведение модулей SIM800 и родственных SIMCom.

Особенности: одно USB-устройство даёт один последовательный порт (модуль подключён
через универсальный мост, поэтому идентификаторы USB принадлежат мосту, а не
модулю); скорость порта заранее неизвестна и подбирается; остаток попыток ввода
PIN-кода читается командой SIMCom; сам модуль об уровне сигнала не сообщает, пока
это не включено отдельной командой.
"""

from __future__ import annotations

import logging
import re

from ..at.errors import CommandError
from ..at.session import AtSession
from ..values import Identity, PinAttempts, Storage
from .base import Kind, Unsolicited, classify_standard, parse_cpms
from .generic import GenericAtBehavior

log = logging.getLogger(__name__)

#: Незапрошенные сообщения SIMCom.
SIMCOM_PREFIXES = (
    "+CIEV:",
    "+CSQN:",
    "RDY",
    "CALL READY",
    "SMS READY",
    "NORMAL POWER DOWN",
    "UNDER-VOLTAGE",
    "OVER-VOLTAGE",
    "+SAPBR",
    "*PSUTTZ:",
    "DST:",
    "+CPIN:",
)

#: Хранилища сообщений, которые опрашиваются: у модуля память карты и своя память.
STORAGE_PREFERENCE = ("SM", "ME")


class Sim800Behavior(GenericAtBehavior):
    """Работа с модулями SIM800/SIM900 и совместимыми."""

    family = "sim800"
    supports_reset = True
    unsolicited_prefixes = SIMCOM_PREFIXES
    #: Мост часто настроен на 115200, но встречаются модули на 9600.
    baudrates = (115200, 9600, 57600, 38400, 19200)

    def __init__(self) -> None:
        # Самостоятельная отправка уровня сигнала есть не во всех прошивках:
        # признак уточняется при инициализации, чтобы интервал опроса выбирался
        # по факту, а не по предположению.
        self.pushes_signal = False

    @classmethod
    def matches(cls, identity: Identity, *, hint: str = "") -> bool:
        haystack = " ".join(
            (identity.manufacturer, identity.model, identity.revision, identity.raw)
        ).upper()
        return any(
            marker in haystack
            for marker in ("SIM800", "SIM900", "SIM868", "SIM808", "SIMCOM")
        )

    # ------------------------------------------------------------ инициализация

    async def enable_notifications(self, session: AtSession) -> None:
        await super().enable_notifications(session)
        # Не выводить незапрошенные сообщения о запуске в произвольный момент --
        # они мешают разбору ответов, а состояние всё равно опрашивается.
        await self._safe(session, "AT+CIURC=1")
        # Присылать уровень сигнала самостоятельно, если прошивка это умеет.
        try:
            await session.execute("AT+AUTOCSQ=1,1")
        except CommandError as exc:
            log.debug(
                "%s: самостоятельная отправка уровня сигнала недоступна (%s)",
                session.port,
                exc.final,
            )
            self.pushes_signal = False
        else:
            self.pushes_signal = True

    # ------------------------------------------------- незапрошенные сообщения

    def classify(self, line: str) -> Unsolicited:
        match = _CSQN.match(line)
        if match:
            return Unsolicited(Kind.SIGNAL, {"rssi": int(match.group(1))}, raw=line)

        match = _CIEV.match(line)
        if match:
            indicator, value = int(match.group(1)), int(match.group(2))
            # Индикатор 2 -- уровень сигнала в шкале 0..5, не в шкале +CSQ.
            if indicator == 2:
                return Unsolicited(
                    Kind.SIGNAL,
                    {"bars": value, "scale": 5},
                    raw=line,
                )
            return Unsolicited(Kind.UNKNOWN, {"indicator": indicator, "value": value}, raw=line)

        if line.upper().startswith("+SMMEMFULL") or "SMS FULL" in line.upper():
            return Unsolicited(Kind.STORAGE_FULL, {"full": True}, raw=line)

        return classify_standard(line)

    # -------------------------------------------------------- остаток попыток

    async def read_pin_attempts(self, session: AtSession) -> PinAttempts:
        """Читает остаток попыток командой SIMCom `AT+SPIC`."""
        try:
            response = await session.execute("AT+SPIC")
        except CommandError as exc:
            log.debug("%s: AT+SPIC отклонена (%s)", session.port, exc.final)
        else:
            attempts = parse_spic(response.text)
            if attempts.known:
                return attempts
            log.info("%s: ответ на AT+SPIC не разобран: %r", session.port, response.text)
        standard = await super().read_pin_attempts(session)
        if standard.known:
            return standard
        return PinAttempts(
            reason="ни AT+SPIC, ни AT+CPINR не дали разборчивого остатка попыток"
        )

    async def read_storage(self, session: AtSession) -> Storage:
        """Читает заполненность памяти сообщений.

        Модуль хранит сообщения либо на карте, либо в своей памяти; фактическое
        имя хранилища зависит от прошивки, поэтому берётся то, которое модуль
        сообщает сам.
        """
        try:
            response = await session.execute("AT+CPMS?")
        except CommandError as exc:
            log.debug("%s: заполненность памяти недоступна (%s)", session.port, exc.final)
            return Storage()
        return parse_cpms(response.text)

    async def reset(self, session: AtSession) -> None:
        """Программный сброс: перезапуск набора функций модуля."""
        await session.execute("AT+CFUN=1,1", timeout=15.0)


# --------------------------------------------------------------------- разбор

_CSQN = re.compile(r"^\+CSQN:\s*(\d+)", re.IGNORECASE)
_CIEV = re.compile(r"^\+CIEV:\s*(\d+)\s*,\s*(\d+)", re.IGNORECASE)

#: `+SPIC: <pin1>,<pin2>,<puk1>,<puk2>` -- остатки попыток по каждому коду.
_SPIC = re.compile(
    r"^\+SPIC:\s*(\d+)(?:\s*,\s*(\d+))?(?:\s*,\s*(\d+))?(?:\s*,\s*(\d+))?",
    re.IGNORECASE,
)


def parse_spic(text: str) -> PinAttempts:
    """Разбирает ответ на `AT+SPIC`."""
    for raw in text.splitlines():
        match = _SPIC.match(raw.strip())
        if match:
            puk1 = int(match.group(3)) if match.group(3) else None
            return PinAttempts(pin=int(match.group(1)), puk=puk1, source="AT+SPIC")
    return PinAttempts(reason="ответ на AT+SPIC не разобран")
