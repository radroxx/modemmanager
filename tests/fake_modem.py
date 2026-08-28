"""Заглушка последовательного порта с поведением модема.

Позволяет проверять сессию, семейства и обнаружение без подключённого железа:
задаётся таблица ответов на команды, можно вставлять незапрошенные сообщения в
произвольный момент и имитировать исчезновение устройства.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable

from modemmanager.at.errors import PortGone


class FakeTransport:
    """Порт, отвечающий по заданной таблице.

    ``responses`` -- команда без завершающего ``\\r`` -> либо строка/список строк
    ответа (завершитель ``OK`` добавляется, если его нет), либо вызываемый
    объект, получающий команду.
    """

    def __init__(
        self,
        responses: dict[str, object] | None = None,
        *,
        port: str = "/dev/ttyFAKE",
        default: object | None = "ERROR",
        echo: bool = False,
        delay: float = 0.0,
    ):
        self.port = port
        self.responses = dict(responses or {})
        self.default = default
        self.echo = echo
        self.delay = delay

        #: Всё, что было записано в порт (для проверки «ни одного байта»).
        self.written: list[bytes] = []
        #: Команды без завершителя строки, в порядке поступления.
        self.commands: list[str] = []
        self.opened = 0
        self.closed = 0
        self.is_open = False

        self._incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._gone: str | None = None
        self._tasks: set[asyncio.Task] = set()
        #: Команды, на которые порт молчит (имитация зависшего модема).
        self.silent: set[str] = set()

    # -------------------------------------------------------------- управление

    def queue_unsolicited(self, *lines: str) -> None:
        """Помещает незапрошенные сообщения в поток чтения."""
        for line in lines:
            self._incoming.put_nowait(("\r\n" + line + "\r\n").encode("latin-1"))

    def queue_raw(self, data: str) -> None:
        """Помещает произвольные байты (для проверки разбора строк)."""
        self._incoming.put_nowait(data.encode("latin-1"))

    def disappear(self, detail: str = "устройство отключено") -> None:
        """Имитирует исчезновение устройства."""
        self._gone = detail
        self._incoming.put_nowait(None)

    def set_response(self, command: str, response: object) -> None:
        self.responses[command] = response

    # --------------------------------------------------------------- транспорт

    async def open(self) -> None:
        if self._gone is not None:
            raise PortGone(self.port, self._gone)
        # Свежеоткрытый порт не содержит данных от предыдущего открытия.
        self._incoming = asyncio.Queue()
        self.opened += 1
        self.is_open = True

    async def close(self) -> None:
        self.closed += 1
        self.is_open = False
        self._incoming.put_nowait(None)

    async def write(self, data: bytes) -> None:
        if self._gone is not None:
            raise PortGone(self.port, self._gone)
        if not self.is_open:
            raise PortGone(self.port, "порт закрыт")
        self.written.append(data)
        command = data.decode("latin-1").strip()
        self.commands.append(command)
        if command in self.silent:
            return
        reply = self._reply_for(command)
        if reply is None:
            return
        payload = (command + "\r\n" + reply) if self.echo else reply
        encoded = payload.encode("latin-1")
        if self.delay:
            # Задержка относится к ответу, а не к записи: так таймаут команды
            # проверяется на том же участке, где он работает в жизни.
            task = asyncio.create_task(self._reply_later(encoded))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        self._incoming.put_nowait(encoded)

    async def _reply_later(self, payload: bytes) -> None:
        await asyncio.sleep(self.delay)
        self._incoming.put_nowait(payload)

    async def read(self) -> bytes:
        chunk = await self._incoming.get()
        if chunk is None:
            raise PortGone(self.port, self._gone or "порт закрыт")
        return chunk

    # ----------------------------------------------------------------- ответы

    def _reply_for(self, command: str) -> str | None:
        entry = self.responses.get(command, self.default)
        if entry is None:
            return None
        if callable(entry):
            entry = entry(command)
            if entry is None:
                return None
        return _format(entry)


def _format(entry: object) -> str:
    if isinstance(entry, str):
        lines: list[str] = [entry] if entry else []
    elif isinstance(entry, Iterable):
        lines = [str(item) for item in entry]
    else:  # pragma: no cover -- защита от опечатки в тесте
        raise TypeError(f"не понимаю ответ {entry!r}")
    terminators = {"OK", "ERROR"}
    if not lines or not (
        lines[-1].upper() in terminators or lines[-1].upper().startswith(("+CME ERROR", "+CMS ERROR"))
    ):
        lines.append("OK")
    return "\r\n" + "\r\n".join(lines) + "\r\n"


class FakePortSet:
    """Набор поддельных портов с отдельной таблицей ответов на каждый.

    ``ports`` -- порт -> таблица ответов, либо ``None`` для молчащего порта.
    ``baudrates`` ограничивает скорости, на которых порты вообще отвечают.
    Подходит на место фабрики транспорта: ``FakePortSet(...).factory``.
    """

    def __init__(
        self,
        ports: dict[str, dict[str, object] | None],
        *,
        baudrates: set[int] | None = None,
    ):
        self.tables = ports
        self.baudrates = baudrates
        #: Каждое открытие порта: (порт, скорость).
        self.touched: list[tuple[str, int]] = []
        self.transports: list[FakeTransport] = []

    def factory(self, port: str, baudrate: int) -> FakeTransport:
        self.touched.append((port, baudrate))
        table = self.tables.get(port)
        speaks = table is not None and (self.baudrates is None or baudrate in self.baudrates)
        transport = FakeTransport(
            # Молчащий порт не отвечает вообще: так проверяется таймаут пробы.
            dict(table or {}) if speaks else {},
            port=port,
            default="ERROR" if speaks else None,
        )
        self.transports.append(transport)
        return transport

    @property
    def ports_touched(self) -> list[str]:
        return [port for port, _baudrate in self.touched]

    def opened(self, port: str) -> list[FakeTransport]:
        """Все транспорты, созданные для этого порта, в порядке создания."""
        return [transport for transport in self.transports if transport.port == port]


def responder(table: dict[str, object]) -> Callable[[str], object]:
    """Обработчик команд с поиском по префиксу."""

    def handle(command: str) -> object:
        for prefix, value in table.items():
            if command.startswith(prefix):
                return value
        return "ERROR"

    return handle
