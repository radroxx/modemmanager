"""Ошибки обмена с портом.

Ошибка команды отличается от исчезновения устройства: первая означает, что
модем ответил отказом, вторая -- что отвечать больше некому.
"""

from __future__ import annotations


class AtError(Exception):
    """Базовая ошибка обмена с модемом."""


class CommandError(AtError):
    """Модем ответил признаком ошибки.

    Код сохраняется отдельно: он нужен журналу и метрикам, а не только тексту.
    """

    def __init__(self, command: str, final: str, code: int | None = None, kind: str = ""):
        self.command = command
        self.final = final
        self.code = code
        #: ``CME``, ``CMS`` или пустая строка для простого ``ERROR``.
        self.kind = kind
        super().__init__(f"{command} -> {final}")


class CommandTimeout(AtError):
    """Модем не завершил ответ за отведённое команде время."""

    def __init__(self, command: str, timeout: float):
        self.command = command
        self.timeout = timeout
        super().__init__(f"{command}: нет ответа за {timeout:g} с")


class ForbiddenCommand(AtError):
    """Команда запрещена навсегда: её выполнение стоит денег.

    Отклоняется до записи в порт, поэтому модем такую команду не увидит.
    """

    def __init__(self, command: str, reason: str):
        self.command = command
        self.reason = reason
        super().__init__(f"{command}: {reason}")


class PortGone(AtError):
    """Устройство исчезло: порт закрыт или чтение вернуло ошибку отсутствия."""

    def __init__(self, port: str, detail: str = ""):
        self.port = port
        self.detail = detail
        super().__init__(f"{port} исчез" + (f": {detail}" if detail else ""))


class RecoveryExhausted(AtError):
    """Все меры восстановления исчерпаны, требуется вмешательство человека."""

    def __init__(self, port: str, last_step: str):
        self.port = port
        self.last_step = last_step
        super().__init__(f"{port}: восстановление не удалось (последняя мера: {last_step})")
