"""Универсальное AT-поведение.

Основа для остальных семейств и запасной вариант для модема, который отвечает на
AT-команды, но не опознан. Использует только команды из общего стандарта, поэтому
работает почти везде, но знает про модем меньше, чем специализированное семейство.
"""

from __future__ import annotations

import logging
import re

from ..at.errors import AtError, CommandError
from ..at.session import AtSession
from ..values import (
    Identity,
    Operator,
    PinAttempts,
    Registration,
    RegistrationState,
    Signal,
    SimState,
    Storage,
    registration_state,
)
from .base import (
    ModemBehavior,
    parse_cops,
    parse_cpin,
    parse_cpms,
    parse_creg,
    parse_csq,
    sim_state_from_error,
)

log = logging.getLogger(__name__)


class GenericAtBehavior(ModemBehavior):
    """Работа с модемом по общему набору AT-команд."""

    family = "generic"
    supports_reset = False
    baudrates = (115200, 9600)

    @classmethod
    def matches(cls, identity: Identity, *, hint: str = "") -> bool:
        # Запасной вариант выбирается реестром явно, а не по совпадению.
        return False

    async def read_signal(self, session: AtSession) -> Signal:
        try:
            response = await session.execute("AT+CSQ")
        except CommandError as exc:
            log.debug("%s: уровень сигнала недоступен (%s)", session.port, exc.final)
            return Signal.unknown()
        return parse_csq(response.text)

    async def read_registration(self, session: AtSession) -> Registration:
        voice_code, area, cell = await self._registration("AT+CREG?", session)
        data_code, data_area, data_cell = await self._registration("AT+CGREG?", session)
        return Registration(
            voice=registration_state(voice_code)
            if voice_code is not None
            else RegistrationState.UNKNOWN,
            data=registration_state(data_code)
            if data_code is not None
            else RegistrationState.UNKNOWN,
            area=area or data_area,
            cell=cell or data_cell,
        )

    async def _registration(self, command: str, session: AtSession):
        try:
            response = await session.execute(command)
        except CommandError as exc:
            log.debug("%s: %s отклонена (%s)", session.port, command, exc.final)
            return (None, "", "")
        return parse_creg(response.text)

    async def read_operator(self, session: AtSession) -> Operator:
        """Читает оператора дважды: числовой код и название.

        Числовой код нужен настройкам и метрикам (он не меняется), название --
        интерфейсу. Один запрос даёт только одно из двух, поэтому формат
        переключается.
        """
        plmn = ""
        name = ""
        mode = None
        technology = ""
        for fmt in (2, 0):
            try:
                await session.execute(f"AT+COPS=3,{fmt}")
                response = await session.execute("AT+COPS?")
            except CommandError as exc:
                log.debug("%s: оператор в формате %d недоступен (%s)", session.port, fmt, exc.final)
                continue
            current_mode, current_fmt, value, act = parse_cops(response.text)
            mode = current_mode if current_mode is not None else mode
            technology = act or technology
            if not value:
                continue
            if current_fmt == 2 or value.isdigit():
                plmn = value
            else:
                name = value
        return Operator(
            plmn=plmn,
            name=name,
            manual=mode == 1,
            technology=technology,
        )

    async def read_pin_attempts(self, session: AtSession) -> PinAttempts:
        """Читает остаток попыток стандартной командой `AT+CPINR`.

        Многие модемы её не поддерживают. Отказ или неразборчивый ответ дают
        «остаток неизвестен», и это осознанно запрещает ввод PIN-кода: рисковать
        блокировкой карты по PUK нельзя.
        """
        try:
            response = await session.execute('AT+CPINR="SIM PIN"')
        except CommandError as exc:
            return PinAttempts(reason=f"AT+CPINR отклонена модемом ({exc.final})")
        except AtError as exc:
            return PinAttempts(reason=f"AT+CPINR не выполнена ({exc})")
        return parse_cpinr(response.text)

    async def read_storage(self, session: AtSession) -> Storage:
        try:
            response = await session.execute("AT+CPMS?")
        except CommandError as exc:
            log.debug("%s: заполненность памяти недоступна (%s)", session.port, exc.final)
            return Storage()
        return parse_cpms(response.text)

    async def read_sim_state(self, session: AtSession) -> tuple[SimState, str]:
        try:
            response = await session.execute("AT+CPIN?")
        except CommandError as exc:
            state = sim_state_from_error(exc)
            return (state, f"+CME ERROR: {exc.code if exc.code is not None else exc.final}")
        return parse_cpin(response.text)


_CPINR = re.compile(
    r'^\+CPINR:\s*"?([A-Za-z0-9 ]+)"?\s*,\s*(\d+)(?:\s*,\s*(\d+))?',
    re.IGNORECASE,
)


def parse_cpinr(text: str) -> PinAttempts:
    """Разбирает ответ на `AT+CPINR`: `+CPINR: "SIM PIN",3,3`."""
    for line in text.splitlines():
        match = _CPINR.match(line.strip())
        if match:
            return PinAttempts(pin=int(match.group(2)), source="AT+CPINR")
    return PinAttempts(reason="ответ на AT+CPINR не разобран")
