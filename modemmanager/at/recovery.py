"""Лестница восстановления порта.

Меры применяются по возрастанию тяжести. Смысл лестницы -- не «починить любой
ценой», а дойти до честного состояния неисправности: модем, который не отвечает
после сброса, требует человека, и об этом нужно уведомить, а не продолжать
бесконечные попытки, скрывая проблему.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum

from .errors import CommandError, CommandTimeout, PortGone, RecoveryExhausted
from .session import AtSession

log = logging.getLogger(__name__)


class Step(str, Enum):
    """Ступени лестницы в порядке применения."""

    RETRY = "retry"
    REOPEN = "reopen"
    RESET = "reset"
    FAULT = "fault"


@dataclass
class RecoveryPolicy:
    command_retries: int = 2
    reopen_attempts: int = 1
    reset_attempts: int = 1
    #: Пауза перед повторным открытием: устройству нужно время после сброса.
    reopen_delay: float = 1.0
    reset_delay: float = 5.0
    #: Таймаут проверочного `AT` после применённой меры.
    probe_timeout: float = 2.0


@dataclass
class RecoveryLadder:
    """Выполняет команды, восстанавливая порт при повторяющихся ошибках."""

    session: AtSession
    #: Повторная инициализация порта после переоткрытия (последовательность семейства).
    initialise: Callable[[AtSession], Awaitable[None]]
    #: Программный сброс модема способом его семейства; ``None`` -- сброса нет.
    reset: Callable[[AtSession], Awaitable[None]] | None = None
    policy: RecoveryPolicy = field(default_factory=RecoveryPolicy)
    #: Вызывается при каждой применённой мере -- для журнала и метрик.
    on_step: Callable[[Step, str], None] | None = None

    steps_taken: list[Step] = field(default_factory=list)
    reconnects: int = 0

    async def execute(self, command: str, *, timeout: float, secret: bool = False):
        """Выполняет команду, поднимаясь по лестнице при таймаутах."""
        try:
            return await self.session.execute(
                command, timeout=timeout, secret=secret, retries=self.policy.command_retries
            )
        except CommandTimeout:
            self._note(Step.RETRY, command)
        except PortGone:
            # Порт исчез: переоткрытие -- единственное, что может помочь, но
            # обычно это означает, что устройство отключили физически.
            self._note(Step.REOPEN, command)
            await self._reopen()
            return await self.session.execute(command, timeout=timeout, secret=secret)

        await self.recover()
        return await self.session.execute(command, timeout=timeout, secret=secret)

    async def recover(self) -> None:
        """Проходит лестницу до первой меры, после которой модем отвечает."""
        for _ in range(self.policy.reopen_attempts):
            self._note(Step.REOPEN, "переоткрытие порта")
            try:
                await self._reopen()
                if await self._responds():
                    return
            except PortGone as exc:
                log.warning("%s: переоткрытие не удалось (%s)", self.session.port, exc)

        if self.reset is not None:
            for _ in range(self.policy.reset_attempts):
                self._note(Step.RESET, "программный сброс")
                try:
                    await self.reset(self.session)
                except Exception as exc:
                    log.warning("%s: сброс не удался (%s)", self.session.port, exc)
                await asyncio.sleep(self.policy.reset_delay)
                try:
                    await self._reopen()
                except PortGone as exc:
                    log.warning("%s: порт не открылся после сброса (%s)", self.session.port, exc)
                    continue
                if await self._responds():
                    return

        self._note(Step.FAULT, "меры исчерпаны")
        raise RecoveryExhausted(self.session.port, self.steps_taken[-1].value)

    # ------------------------------------------------------------------ ступени

    async def _reopen(self) -> None:
        await self.session.close()
        await asyncio.sleep(self.policy.reopen_delay)
        await self.session.open()
        self.reconnects += 1
        await self.initialise(self.session)

    async def _responds(self) -> bool:
        """Отвечает ли модем вообще.

        Отказ на `AT` -- тоже ответ: модем жив, и лестница дальше не нужна.
        Признаком неисправности считается только молчание или исчезновение.
        """
        try:
            await self.session.execute("AT", timeout=self.policy.probe_timeout)
        except CommandError:
            return True
        except (CommandTimeout, PortGone) as exc:
            log.debug("%s: модем не отвечает после восстановления (%s)", self.session.port, exc)
            return False
        return True

    def _note(self, step: Step, detail: str) -> None:
        self.steps_taken.append(step)
        log.warning("%s: мера восстановления %s (%s)", self.session.port, step.value, detail)
        if self.on_step is not None:
            try:
                self.on_step(step, detail)
            except Exception:
                log.exception("%s: сбой обработчика мер восстановления", self.session.port)
