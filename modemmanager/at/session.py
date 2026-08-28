"""Сессия обмена AT-командами на одном порту.

Один читающий цикл и одна команда в работе. Всё, что приходит из порта, проходит
через один разбор: строки делятся на завершители ответа, строки результата и
незапрошенные сообщения. Незапрошенное сообщение может прийти в любой момент, в
том числе между строками ответа, поэтому решение принимается для каждой строки, а
не для порции байтов.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from .errors import CommandError, CommandTimeout, ForbiddenCommand, PortGone
from .guard import forbidden_reason, mask
from .transport import Transport

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5.0
LINE_TERMINATOR = b"\r"

#: Завершители успешного ответа.
_FINAL_OK = ("OK",)
#: Завершители ответа с ошибкой. ``NO CARRIER`` появляется в ответ на ``ATH``,
#: если вызывающий отключился раньше.
_FINAL_ERROR = ("ERROR", "NO CARRIER", "BUSY", "NO ANSWER", "NO DIALTONE", "ABORTED")
_ERROR_CODE = re.compile(r"^\+(CME|CMS)\s+ERROR:\s*(.*)$", re.IGNORECASE)

#: Незапрошенные сообщения, общие для всех семейств. Свои префиксы семейства
#: добавляют через ``unsolicited_prefixes``.
BASE_UNSOLICITED = (
    "RING",
    "+CLIP:",
    "+CRING:",
    "+CMTI:",
    "+CMT:",
    "+CDS:",
    "+CDSI:",
    "+CBM:",
    "+CREG:",
    "+CGREG:",
    "+CEREG:",
    "NO CARRIER",
    "+CUSD:",
    "+CPIN:",
    "+CIEV:",
    "RDY",
    "NORMAL POWER DOWN",
    "UNDER-VOLTAGE",
    "OVER-VOLTAGE",
    "CALL READY",
    "SMS READY",
    "+CFUN:",
)


@dataclass
class Response:
    """Результат одной команды."""

    command: str
    lines: list[str] = field(default_factory=list)
    final: str = "OK"
    duration: float = 0.0

    def first(self, prefix: str) -> str | None:
        """Первая строка результата с указанным префиксом, без префикса."""
        upper = prefix.upper()
        for line in self.lines:
            if line.upper().startswith(upper):
                return line[len(prefix) :].strip()
        return None

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


#: Извлекает из команды префикс, с которым модем отвечает на неё.
#: ``AT+CREG?`` -> ``+CREG:``; ``AT^CPIN?`` -> ``^CPIN:``. Нужно, чтобы строка
#: ответа не была принята за одноимённое незапрошенное сообщение.
_COMMAND_PREFIX = re.compile(r"^AT([+^*$])([A-Z0-9]+)", re.IGNORECASE)


def expected_prefix(command: str) -> str:
    match = _COMMAND_PREFIX.match(command.strip())
    if not match:
        return ""
    return f"{match.group(1)}{match.group(2).upper()}:"


@dataclass
class _Pending:
    command: str
    lines: list[str] = field(default_factory=list)
    future: asyncio.Future | None = None
    started: float = 0.0
    #: Строка эха, которую нужно проглотить, если модем всё же его вернул.
    echo: str = ""
    #: Префикс строк, которые принадлежат ответу этой команды.
    expects: str = ""


class AtSession:
    """Обмен командами с модемом через один порт."""

    def __init__(
        self,
        transport: Transport,
        *,
        on_unsolicited: Callable[[str], None] | None = None,
        on_gone: Callable[[PortGone], None] | None = None,
        unsolicited_prefixes: Sequence[str] = (),
        trace: bool = False,
    ):
        self.transport = transport
        self.port = getattr(transport, "port", "?")
        self._on_unsolicited = on_unsolicited
        self._on_gone = on_gone
        self._prefixes = tuple(BASE_UNSOLICITED) + tuple(unsolicited_prefixes)
        self.trace = trace

        self._lock = asyncio.Lock()  # FIFO: команды выполняются в порядке поступления
        self._pending: _Pending | None = None
        self._buffer = bytearray()
        self._reader_task: asyncio.Task | None = None
        self._gone: PortGone | None = None
        self._closed = False
        self._continuation = 0
        #: Счётчик ошибок обмена для метрик.
        self.error_count = 0
        #: Сколько раз была отклонена запрещённая команда.
        self.forbidden_attempts = 0
        #: Строки, которые не удалось отнести ни к ответу, ни к событию.
        self.unknown_lines: list[str] = []

    # ------------------------------------------------------------- жизненный цикл

    @property
    def alive(self) -> bool:
        return not self._closed and self._gone is None

    async def open(self) -> None:
        """Открывает порт и запускает читающий цикл."""
        await self.transport.open()
        self._closed = False
        self._gone = None
        self._buffer.clear()
        self._continuation = 0
        self._reader_task = asyncio.create_task(self._read_loop(), name=f"at-read:{self.port}")

    async def close(self) -> None:
        """Останавливает читающий цикл и закрывает порт."""
        self._closed = True
        task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.transport.close()
        self._fail_pending(PortGone(self.port, "сессия закрыта"))

    async def initialise(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        """Приводит порт в предсказуемое состояние.

        Эхо выключается, чтобы отражённая команда не попадала в результат;
        числовые коды ошибок включаются, чтобы ошибку можно было различить в
        журнале и метриках. Отказ на ``AT+CMEE`` не критичен: старые прошивки её
        не знают, разбор ответа от этого не ломается.
        """
        await self.execute("ATE0", timeout=timeout, retries=1)
        try:
            await self.execute("AT+CMEE=1", timeout=timeout)
        except CommandError as exc:
            log.debug("%s: числовые коды ошибок недоступны (%s)", self.port, exc.final)

    def set_unsolicited_prefixes(self, prefixes: Iterable[str]) -> None:
        """Расширяет набор известных незапрошенных сообщений (для семейства)."""
        self._prefixes = tuple(BASE_UNSOLICITED) + tuple(prefixes)

    def set_unsolicited_handler(self, handler: Callable[[str], None]) -> None:
        self._on_unsolicited = handler

    # -------------------------------------------------------------- выполнение

    async def execute(
        self,
        command: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        secret: bool = False,
        retries: int = 0,
    ) -> Response:
        """Выполняет команду и возвращает её ответ.

        Запрещённые команды отклоняются до захвата порта: ни одного байта в порт
        не уходит. ``secret`` влияет только на диагностику -- в порт уходит
        настоящее значение.
        """
        reason = forbidden_reason(command)
        if reason:
            self.forbidden_attempts += 1
            log.error("%s: отклонена запрещённая команда %s (%s)", self.port, mask(command), reason)
            raise ForbiddenCommand(mask(command), reason)

        attempt = 0
        while True:
            try:
                return await self._execute_once(command, timeout=timeout, secret=secret)
            except CommandTimeout:
                if attempt >= retries:
                    raise
                attempt += 1
                log.warning(
                    "%s: повтор %s после таймаута (%d/%d)",
                    self.port,
                    self._display(command, secret),
                    attempt,
                    retries,
                )

    async def _execute_once(self, command: str, *, timeout: float, secret: bool) -> Response:
        if self._gone is not None:
            raise self._gone
        if self._closed:
            raise PortGone(self.port, "сессия закрыта")

        async with self._lock:
            if self._gone is not None:
                raise self._gone
            loop = asyncio.get_running_loop()
            pending = _Pending(
                command=command,
                future=loop.create_future(),
                echo=command.strip(),
                expects=expected_prefix(command),
            )
            pending.started = time.monotonic()
            self._pending = pending
            try:
                if self.trace:
                    log.debug("%s > %s", self.port, self._display(command, secret))
                await self.transport.write(command.encode("latin-1") + LINE_TERMINATOR)
                try:
                    final = await asyncio.wait_for(pending.future, timeout)
                except asyncio.TimeoutError:
                    self.error_count += 1
                    raise CommandTimeout(self._display(command, secret), timeout) from None
            finally:
                self._pending = None

            duration = time.monotonic() - pending.started
            display = self._display(command, secret)
            if final in _FINAL_OK:
                return Response(command=display, lines=pending.lines, final=final, duration=duration)
            self.error_count += 1
            kind, code = _parse_error(final)
            raise CommandError(display, final, code=code, kind=kind)

    def _display(self, command: str, secret: bool) -> str:
        return mask(command) if secret else command

    # ------------------------------------------------------------ чтение порта

    async def _read_loop(self) -> None:
        try:
            while True:
                chunk = await self.transport.read()
                self._feed(chunk)
        except asyncio.CancelledError:
            raise
        except PortGone as exc:
            self._handle_gone(exc)
        except Exception as exc:  # pragma: no cover -- неожидаемая ошибка порта
            log.exception("%s: сбой читающего цикла", self.port)
            self._handle_gone(PortGone(self.port, str(exc)))

    def _handle_gone(self, exc: PortGone) -> None:
        if self._gone is not None:
            return
        self._gone = exc
        self.error_count += 1
        log.warning("%s: устройство исчезло (%s)", self.port, exc.detail or "без подробностей")
        self._fail_pending(exc)
        if self._on_gone is not None:
            try:
                self._on_gone(exc)
            except Exception:
                log.exception("%s: сбой обработчика исчезновения", self.port)

    def _fail_pending(self, exc: BaseException) -> None:
        pending, self._pending = self._pending, None
        if pending is not None and pending.future is not None and not pending.future.done():
            pending.future.set_exception(exc)

    def _feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)
        # Модемы разделяют строки \r\n, но встречается и одиночный \r или \n.
        normalised = self._buffer.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        *complete, tail = normalised.split(b"\n")
        self._buffer = bytearray(tail)
        for raw in complete:
            line = raw.decode("latin-1", "replace").strip()
            if line:
                self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        if self.trace:
            # Эхо команды с секретом может прилететь сюда до того, как ATE0
            # успело выключить его. Маска на входящем канале это закрывает.
            log.debug("%s < %s", self.port, mask(line))

        # Продолжение незапрошенного сообщения (например, PDU после +CMT:)
        # отдаётся тому же обработчику без разбора.
        if self._continuation > 0:
            self._continuation -= 1
            self._dispatch(line, raw=True)
            return

        pending = self._pending

        if pending is not None:
            # Эхо включено не было, но некоторые прошивки его всё равно шлют.
            if line == pending.echo:
                return
            if self._is_final(line, pending):
                return
            # Строка своего ответа главнее одноимённого события: на `AT+CREG?`
            # модем отвечает той же строкой `+CREG:`, что присылает сам.
            if pending.expects and line.upper().startswith(pending.expects):
                pending.lines.append(line)
                return
            if self._is_unsolicited(line):
                self._dispatch(line)
                return
            pending.lines.append(line)
            return

        if self._is_unsolicited(line):
            self._dispatch(line)
            return
        # Ответ на команду, которая уже завершилась таймаутом, тоже приходит сюда.
        self.unknown_lines.append(line)
        if len(self.unknown_lines) > 100:
            del self.unknown_lines[:-100]
        log.debug("%s: строка вне контекста: %s", self.port, line)

    def _is_final(self, line: str, pending: _Pending) -> bool:
        upper = line.upper()
        if upper in _FINAL_OK or upper in _FINAL_ERROR:
            # NO CARRIER бывает и незапрошенным: когда команда не выполняется,
            # это событие завершения вызова, а не завершитель ответа.
            self._complete(pending, upper)
            return True
        if _ERROR_CODE.match(line):
            self._complete(pending, line)
            return True
        return False

    def _complete(self, pending: _Pending, final: str) -> None:
        if pending.future is not None and not pending.future.done():
            pending.future.set_result(final)

    def _is_unsolicited(self, line: str) -> bool:
        upper = line.upper()
        return any(upper.startswith(prefix.upper()) for prefix in self._prefixes)

    def _dispatch(self, line: str, *, raw: bool = False) -> None:
        if self._on_unsolicited is None:
            self.unknown_lines.append(line)
            return
        try:
            wants = self._on_unsolicited(line)
        except Exception:
            log.exception("%s: сбой обработки незапрошенного сообщения %s", self.port, line)
            return
        # Обработчик может сообщить, сколько следующих строк принадлежат ему:
        # так `+CMT:` получает строку с данными сообщения.
        if not raw and isinstance(wants, int) and wants > 0:
            self._continuation = wants


def _parse_error(final: str) -> tuple[str, int | None]:
    """Разбирает завершитель ответа с ошибкой на вид и числовой код."""
    match = _ERROR_CODE.match(final)
    if not match:
        return ("", None)
    kind = match.group(1).upper()
    value = match.group(2).strip()
    try:
        return (kind, int(value))
    except ValueError:
        # Модем ответил текстом ошибки вместо кода (AT+CMEE=2).
        return (kind, None)
