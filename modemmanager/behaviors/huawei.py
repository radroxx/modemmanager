"""Поведение модемов Huawei.

Особенности, из-за которых семейство существует отдельно: одно USB-устройство
отдаёт несколько последовательных портов, и управляющий из них не первый; модем
сам присылает уровень сигнала и состояние сети; остаток попыток ввода PIN-кода
читается своей командой, формат ответа которой различается между прошивками; есть
своя команда программного сброса.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from ..at.errors import CommandError
from ..at.session import AtSession
from ..values import Identity, PinAttempts, SimState
from .base import Kind, Unsolicited, classify_standard
from .generic import GenericAtBehavior

log = logging.getLogger(__name__)

#: Незапрошенные сообщения Huawei.
HUAWEI_PREFIXES = (
    "^RSSI:",
    "^HCSQ:",
    "^MODE:",
    "^BOOT:",
    "^SIMST:",
    "^SRVST:",
    "^SMMEMFULL:",
    "^CSNR:",
    "^STIN:",
    "^CONN:",
    "^ORIG:",
    "^CEND:",
    "^DSFLOWRPT:",
    "^NDISSTAT:",
)

#: Управляющий порт Huawei обычно второй по счёту: первый занят передачей данных,
#: третий -- диагностикой, которая на AT-команды не отвечает. Это лишь порядок
#: проверки: окончательный выбор делается пробой (см. modem-discovery).
_PREFERRED_PORT_ORDER = (1, 2, 0, 3, 4)


class HuaweiBehavior(GenericAtBehavior):
    """Работа с модемами Huawei."""

    family = "huawei"
    pushes_signal = True
    pushes_registration = True
    supports_reset = True
    unsolicited_prefixes = HUAWEI_PREFIXES
    baudrates = (115200,)

    @classmethod
    def matches(cls, identity: Identity, *, hint: str = "") -> bool:
        haystack = " ".join(
            (identity.manufacturer, identity.model, identity.revision, identity.raw)
        ).upper()
        return "HUAWEI" in haystack

    # ------------------------------------------------------------------ порты

    def rank_ports(self, ports: Sequence[str]) -> list[str]:
        """Ставит вероятный управляющий порт первым, сохраняя остальные."""
        remaining = list(ports)
        ordered: list[str] = []
        for index in _PREFERRED_PORT_ORDER:
            if index < len(ports) and ports[index] in remaining:
                ordered.append(ports[index])
                remaining.remove(ports[index])
        ordered.extend(remaining)
        return ordered

    # ------------------------------------------------------------ инициализация

    async def enable_notifications(self, session: AtSession) -> None:
        await super().enable_notifications(session)
        # Присылать уровень сигнала и режим сети самостоятельно: тогда изменения
        # видны сразу, а регулярный опрос нужен реже.
        await self._safe(session, "AT^CURC=1")

    # ------------------------------------------------- незапрошенные сообщения

    def classify(self, line: str) -> Unsolicited:
        match = _RSSI.match(line)
        if match:
            return Unsolicited(Kind.SIGNAL, {"rssi": int(match.group(1))}, raw=line)

        match = _MODE.match(line)
        if match:
            return Unsolicited(
                Kind.NETWORK_MODE,
                {"sys_mode": int(match.group(1)), "sub_mode": int(match.group(2))},
                raw=line,
            )

        match = _SIMST.match(line)
        if match:
            code = int(match.group(1))
            # 0 -- карта недоступна, 1 -- работоспособна, 255 -- не вставлена.
            state = SimState.READY if code == 1 else SimState.ABSENT
            return Unsolicited(Kind.SIM_STATE, {"code": code, "state": state.value}, raw=line)

        match = _SRVST.match(line)
        if match:
            return Unsolicited(
                Kind.REGISTRATION,
                {"domain": "SRVST", "service": int(match.group(1))},
                raw=line,
            )

        if line.upper().startswith("^BOOT:"):
            return Unsolicited(Kind.BOOT, {"detail": line}, raw=line)

        if line.upper().startswith("^SMMEMFULL:"):
            return Unsolicited(Kind.STORAGE_FULL, {"full": True}, raw=line)

        if line.upper().startswith("^CEND:"):
            return Unsolicited(Kind.CALL_ENDED, {"detail": line}, raw=line)

        return classify_standard(line)

    # -------------------------------------------------------- остаток попыток

    async def read_pin_attempts(self, session: AtSession) -> PinAttempts:
        """Читает остаток попыток командой Huawei, затем стандартной.

        Формат ответа `AT^CPIN?` различается между прошивками, поэтому разбор
        допускает несколько видов и честно сообщает «неизвестно», если не понял
        ответ: неизвестный остаток запрещает ввод PIN-кода.
        """
        try:
            response = await session.execute("AT^CPIN?")
        except CommandError as exc:
            log.debug("%s: AT^CPIN? отклонена (%s)", session.port, exc.final)
        else:
            attempts = parse_huawei_cpin(response.text)
            if attempts.known:
                return attempts
            log.info(
                "%s: остаток попыток из AT^CPIN? не разобран: %r",
                session.port,
                response.text,
            )
        # Часть прошивок понимает стандартную команду.
        standard = await super().read_pin_attempts(session)
        if standard.known:
            return standard
        return PinAttempts(
            reason="ни AT^CPIN?, ни AT+CPINR не дали разборчивого остатка попыток"
        )

    async def reset(self, session: AtSession) -> None:
        """Программный сброс модема."""
        await session.execute("AT^RESET", timeout=10.0)


# --------------------------------------------------------------------- разбор

_RSSI = re.compile(r"^\^RSSI:\s*(\d+)", re.IGNORECASE)
_MODE = re.compile(r"^\^MODE:\s*(\d+)\s*,\s*(\d+)", re.IGNORECASE)
_SIMST = re.compile(r"^\^SIMST:\s*(\d+)", re.IGNORECASE)
_SRVST = re.compile(r"^\^SRVST:\s*(\d+)", re.IGNORECASE)

#: `^CPIN: <state>,<remain_times>,<puk_times>,<pin_times>,<pin2_times>,<puk2_times>`
_CPIN_FULL = re.compile(
    #r"^\^CPIN:\s*([A-Za-z0-9 _-]+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)"
    r"^\^CPIN:\s*([A-Za-z0-9 _-]+)\s*,(-?\d*),(\d*),(\d*),(\d*),(\d*)"
    r"(?:\s*,\s*(-?\d+))?(?:\s*,\s*(-?\d+))?",
    re.IGNORECASE,
)
#: Укороченный вид, встречающийся на части прошивок: `^CPIN: SIM PIN,3`
_CPIN_SHORT = re.compile(r"^\^CPIN:\s*([A-Za-z0-9 _-]+)\s*,\s*(-?\d+)\s*$", re.IGNORECASE)


def parse_huawei_cpin(text: str) -> PinAttempts:
    """Разбирает `AT^CPIN?`.

    В полном виде поля идут так: состояние, остаток попыток текущего запроса,
    остаток PUK1, остаток PIN1, остаток PIN2, остаток PUK2. Разбирается остаток
    PIN1 -- именно он определяет, можно ли пробовать вводить PIN-код. Значение
    ``-1`` означает «неприменимо» и трактуется как неизвестное.
    """
    for raw in text.splitlines():
        line = raw.strip()
        match = _CPIN_FULL.match(line)
        if match:
            puk1 = _positive(match.group(3))
            pin1 = _positive(match.group(4))
            if pin1 is None:
                # На части прошивок остаток PIN1 стоит во втором поле.
                pin1 = _positive(match.group(2))
            if pin1 is None:
                continue
            return PinAttempts(pin=pin1, puk=puk1, source="AT^CPIN?")
        match = _CPIN_SHORT.match(line)
        if match:
            pin1 = _positive(match.group(2))
            if pin1 is None:
                continue
            return PinAttempts(pin=pin1, source="AT^CPIN?")
    return PinAttempts(reason="ответ на AT^CPIN? не разобран")


def _positive(value: str | None) -> int | None:
    if value is None:
        return None
    number = int(value)
    return number if number >= 0 else None
