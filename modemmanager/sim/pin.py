"""Защита от блокировки SIM-карты по PUK.

Правило одно: попытка ввода PIN-кода делается только если модем сам сообщил,
что осталось три и более попыток. При остатке две и менее, при неизвестном
остатке и при отсутствии PIN-кода в настройках -- модему PIN не отправляется.
Персистентного «мы уже пробовали» файла нет: счётчик хранится на самой SIM,
и после первой неудачной попытки он становится равным двум, что автоматически
запрещает следующие попытки.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum

from ..at.errors import AtError, CommandError
from ..at.session import AtSession
from ..values import PinAttempts, SimState

log = logging.getLogger(__name__)

#: Ниже этого остатка попыток PIN-код не отправляется никогда.
MIN_ATTEMPTS = 3


class PinAction(str, Enum):
    """Что делать с SIM-картой, требующей PIN-кода."""

    #: Ввести заданный в настройках PIN-код -- остаток попыток достаточен.
    ENTER = "enter"
    #: Отказаться: остаток недостаточен или неизвестен, риск блокировки по PUK.
    REFUSE = "refuse"
    #: PIN-кода в настройках нет -- ждать ввода из интерфейса.
    WAIT = "wait"
    #: SIM-карта заблокирована по PUK -- модему ничего не отправляем.
    PUK_LOCKED = "puk_locked"
    #: SIM-карта не требует PIN-кода -- ничего делать не нужно.
    READY = "ready"
    #: SIM-карта не вставлена -- вводить некому.
    ABSENT = "absent"


@dataclass(frozen=True)
class PinPlan:
    """Решение о вводе PIN-кода со всеми доводами."""

    action: PinAction
    reason: str = ""
    #: Остаток попыток PIN-кода на момент решения, если он известен.
    attempts: int | None = None

    @property
    def should_enter(self) -> bool:
        return self.action is PinAction.ENTER

    @property
    def blocks(self) -> bool:
        """Требует ли решение уведомить администратора."""
        return self.action in (PinAction.REFUSE, PinAction.WAIT, PinAction.PUK_LOCKED)


def plan(
    state: SimState,
    attempts: PinAttempts,
    *,
    pin_configured: bool,
) -> PinPlan:
    """Решает, что делать с SIM в текущем состоянии.

    ``state`` -- ответ модема, ``attempts`` -- остаток попыток (в том числе
    неизвестный), ``pin_configured`` -- задан ли PIN-код в настройках.
    """
    if state is SimState.READY:
        return PinPlan(action=PinAction.READY)
    if state is SimState.ABSENT:
        return PinPlan(action=PinAction.ABSENT, reason="SIM-карта не вставлена")
    if state is SimState.PUK_REQUIRED:
        return PinPlan(
            action=PinAction.PUK_LOCKED,
            reason="SIM-карта заблокирована по PUK",
            attempts=attempts.pin,
        )
    if state is not SimState.PIN_REQUIRED:
        # Неизвестное состояние -- на карту ничего не пишем, ждём определённости.
        return PinPlan(
            action=PinAction.REFUSE,
            reason="состояние SIM неизвестно",
            attempts=attempts.pin,
        )
    if not pin_configured:
        return PinPlan(
            action=PinAction.WAIT,
            reason="PIN-код не задан в настройках",
            attempts=attempts.pin,
        )
    if not attempts.known:
        return PinPlan(
            action=PinAction.REFUSE,
            reason=attempts.reason or "остаток попыток PIN неизвестен",
            attempts=None,
        )
    if attempts.pin is not None and attempts.pin >= MIN_ATTEMPTS:
        return PinPlan(action=PinAction.ENTER, attempts=attempts.pin)
    return PinPlan(
        action=PinAction.REFUSE,
        reason=f"остаток попыток PIN {attempts.pin}: рискованно",
        attempts=attempts.pin,
    )


# --------------------------------------------------------------------- ввод

_PIN_VALID = re.compile(r"^\d{4,8}$")


class PinRejected(AtError):
    """Модем отклонил введённый PIN-код."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(f"PIN отклонён: {detail}")


async def enter_pin(session: AtSession, pin: str) -> None:
    """Отправляет модему ``AT+CPIN=<pin>`` с маскированием в диагностике.

    ``PinRejected`` поднимается, если модем ответил ошибкой -- вызывающий должен
    перечитать остаток попыток и не пытаться повторно.
    """
    if not _PIN_VALID.match(pin):
        raise ValueError("PIN-код должен состоять из 4..8 цифр")
    try:
        await session.execute(f'AT+CPIN="{pin}"', secret=True, timeout=15.0)
    except CommandError as exc:
        raise PinRejected(exc.final) from None
