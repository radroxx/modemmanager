"""Транспорт последовательного порта.

Сессия работает с абстракцией, чтобы её поведение проверялось на заглушке без
подключённого модема. Задача транспорта -- байты и распознавание исчезновения
устройства: `/dev/ttyUSB*` пропадает вместе с USB-устройством, и чтение из уже
открытого дескриптора начинает возвращать ошибку.
"""

from __future__ import annotations

import errno
import logging
from typing import Protocol

from .errors import ForbiddenCommand, PortGone
from .guard import forbidden_reason, mask

log = logging.getLogger(__name__)

#: Ошибки чтения и записи, означающие, что устройства больше нет.
GONE_ERRNOS = frozenset(
    {
        errno.ENODEV,  # No such device
        errno.ENXIO,  # No such device or address
        errno.EIO,  # Input/output error -- ttyUSB отдаёт его при отключении
        errno.EBADF,
        errno.ESHUTDOWN,
        errno.ENOENT,
    }
)


def is_gone_error(exc: BaseException) -> bool:
    """Отличает исчезновение устройства от прочих ошибок ввода-вывода."""
    number = getattr(exc, "errno", None)
    if number in GONE_ERRNOS:
        return True
    # pyserial заворачивает ошибки устройства в SerialException без errno.
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "device reports readiness to read but returned no data",
            "device disconnected",
            "no such device",
            "input/output error",
            "порт закрыт",
        )
    )


class Transport(Protocol):
    """Минимальный контракт порта, достаточный для сессии."""

    port: str

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def write(self, data: bytes) -> None: ...

    async def read(self) -> bytes:
        """Возвращает следующую порцию байтов; бросает ``PortGone``."""
        ...


class SerialTransport:
    """Порт через ``pyserial-asyncio``."""

    def __init__(self, port: str, baudrate: int = 115200, *, read_chunk: int = 4096):
        self.port = port
        self.baudrate = baudrate
        self.read_chunk = read_chunk
        self._reader = None
        self._writer = None

    async def open(self) -> None:
        import serial_asyncio

        try:
            self._reader, self._writer = await serial_asyncio.open_serial_connection(
                url=self.port,
                baudrate=self.baudrate,
                bytesize=8,
                parity="N",
                stopbits=1,
                rtscts=False,
                dsrdtr=False,
                timeout=None,
            )
        except OSError as exc:
            if is_gone_error(exc):
                raise PortGone(self.port, str(exc)) from exc
            raise
        except Exception as exc:  # pyserial.SerialException и производные
            if is_gone_error(exc):
                raise PortGone(self.port, str(exc)) from exc
            raise

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is None:
            return
        try:
            writer.close()
            wait_closed = getattr(writer, "wait_closed", None)
            if wait_closed is not None:
                await wait_closed()
        except Exception as exc:
            # Закрытие исчезнувшего порта штатно завершается ошибкой.
            log.debug("%s: ошибка при закрытии порта: %s", self.port, exc)

    async def write(self, data: bytes) -> None:
        if self._writer is None:
            raise PortGone(self.port, "порт закрыт")
        # Последняя линия обороны: платная команда не должна попасть в порт даже
        # если проверка выше по стеку была пропущена.
        reason = forbidden_reason(data.decode("latin-1", "replace"))
        if reason:
            raise ForbiddenCommand(mask(data.decode("latin-1", "replace")), reason)
        try:
            self._writer.write(data)
            await self._writer.drain()
        except Exception as exc:
            if is_gone_error(exc):
                raise PortGone(self.port, str(exc)) from exc
            raise

    async def read(self) -> bytes:
        if self._reader is None:
            raise PortGone(self.port, "порт закрыт")
        try:
            chunk = await self._reader.read(self.read_chunk)
        except Exception as exc:
            if is_gone_error(exc):
                raise PortGone(self.port, str(exc)) from exc
            raise
        if not chunk:
            raise PortGone(self.port, "чтение вернуло конец потока")
        return chunk
