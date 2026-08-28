"""Проба устройства: модем ли это, какого семейства и через какой порт им управлять.

Принадлежность к драйверу -- недостаточное подтверждение: за `ch341-uart` может
стоять что угодно, а за `option1` -- порт, который на AT-команды не отвечает.
Поэтому решение принимается по ответу устройства, а управляющий порт выбирается
опытным путём: у Huawei он не первый, и полагаться на номер нельзя.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..at.errors import AtError, CommandError, CommandTimeout, PortGone
from ..at.session import AtSession
from ..at.transport import SerialTransport, Transport
from ..behaviors import ModemBehavior, select
from ..behaviors.base import parse_identity
from ..values import Identity
from .sysfs import UsbDevice

log = logging.getLogger(__name__)

#: Команды опознания. `ATI` даёт больше всего, остальные уточняют.
IDENTITY_COMMANDS = ("ATI", "AT+CGMI", "AT+CGMM")

TransportFactory = Callable[[str, int], Transport]


def default_transport_factory(port: str, baudrate: int) -> Transport:
    return SerialTransport(port, baudrate)


@dataclass
class ProbeResult:
    """Итог пробы одного устройства."""

    device: UsbDevice
    ok: bool = False
    behavior: ModemBehavior | None = None
    identity: Identity = field(default_factory=Identity)
    control_port: str = ""
    baudrate: int = 0
    #: Порты, ответившие на пробную команду.
    responding_ports: list[str] = field(default_factory=list)
    #: Почему проба не удалась.
    reason: str = ""
    #: Устройство отвечает на AT, но управляющий порт выбрать не удалось.
    fault: bool = False

    @property
    def imei(self) -> str:
        return self.identity.imei


class Prober:
    """Определяет по устройству, модем ли это, и как с ним работать."""

    def __init__(
        self,
        *,
        transport_factory: TransportFactory = default_transport_factory,
        baudrates: tuple[int, ...] = (115200, 9600),
        port_baudrate: dict[str, int] | None = None,
        timeout: float = 5.0,
        max_attempts: int = 5,
        retry_backoff: float = 2.0,
        trace: bool = False,
    ):
        self.transport_factory = transport_factory
        self.baudrates = tuple(baudrates)
        self.port_baudrate = dict(port_baudrate or {})
        self.timeout = timeout
        #: Диагностическая построчная трассировка обмена. Управляется настройкой.
        self.trace = bool(trace)
        #: Сколько молчаливых проб нужно накопить, прежде чем поставить крестик.
        #: Модем может задерживаться с ответом после появления портов; из-за одной
        #: неудачи навсегда его вычёркивать нельзя.
        self.max_attempts = max(1, int(max_attempts))
        #: База экспоненциальной задержки между попытками: 1-я задержка равна ей,
        #: каждая следующая -- вдвое больше.
        self.retry_backoff = max(0.0, float(retry_backoff))
        #: Устройства, признанные «не модемами». Ключ -- путь на шине вместе с
        #: идентификаторами: переподключение меняет ключ и снимает вердикт.
        self._rejected: dict[str, str] = {}
        #: Счётчик подряд неудачных проб на устройство -- до вердикта.
        self._attempts: dict[str, int] = {}
        #: Время, до которого повторно пробовать не стоит: экспоненциальный backoff.
        self._next_try: dict[str, float] = {}

    # ------------------------------------------------------------ негативный кэш

    def _key(self, device: UsbDevice) -> str:
        return f"{device.usb_path}|{device.hint}|{','.join(sorted(device.ports))}"

    def rejected(self, device: UsbDevice) -> str | None:
        """Вердикт «не модем», если он был вынесен ранее."""
        return self._rejected.get(self._key(device))

    def forget(self, device: UsbDevice) -> None:
        """Снимает вердикт: устройство переподключено и должно быть проверено снова."""
        key = self._key(device)
        self._rejected.pop(key, None)
        self._attempts.pop(key, None)
        self._next_try.pop(key, None)

    def forget_missing(self, present: list[UsbDevice]) -> None:
        """Убирает из кэша устройства, которых больше нет в системе."""
        keys = {self._key(device) for device in present}
        for cache in (self._rejected, self._attempts, self._next_try):
            for stale in [key for key in cache if key not in keys]:
                del cache[stale]

    # -------------------------------------------------------------------- проба

    async def probe(self, device: UsbDevice) -> ProbeResult:
        """Проверяет устройство и выбирает управляющий порт."""
        key = self._key(device)
        verdict = self._rejected.get(key)
        if verdict is not None:
            return ProbeResult(device=device, reason=verdict)

        # Между попытками устройство не трогаем: модем после появления портов
        # может отвечать не сразу, и открытие порта его же и подпинывает.
        deadline = self._next_try.get(key)
        if deadline is not None and time.monotonic() < deadline:
            attempts = self._attempts.get(key, 0)
            return ProbeResult(
                device=device,
                reason=f"пауза перед попыткой {attempts + 1}/{self.max_attempts}",
            )

        identity, behavior, responding = await self._identify(device)
        if behavior is None:
            attempts = self._attempts.get(key, 0) + 1
            reason = f"ни один порт {device.description} не ответил на AT"
            if attempts >= self.max_attempts:
                self._rejected[key] = reason
                self._attempts.pop(key, None)
                self._next_try.pop(key, None)
                log.info(
                    "%s: не модем (%s), исчерпаны %d попыток",
                    device.usb_path,
                    reason,
                    attempts,
                )
                return ProbeResult(
                    device=device, reason=reason, responding_ports=list(responding)
                )
            delay = self.retry_backoff * (2 ** (attempts - 1))
            self._attempts[key] = attempts
            self._next_try[key] = time.monotonic() + delay
            log.info(
                "%s: попытка %d/%d не удалась (%s), повтор через %.1f с",
                device.usb_path,
                attempts,
                self.max_attempts,
                reason,
                delay,
            )
            return ProbeResult(
                device=device, reason=reason, responding_ports=list(responding)
            )
        # Успешная проба обнуляет накопленные неудачи: временное молчание
        # не должно приближать вердикт «не модем».
        self._attempts.pop(key, None)
        self._next_try.pop(key, None)

        control = await self._choose_control_port(device, behavior, responding)
        if control is None:
            # Устройство отвечает на AT, значит это модем; но управлять им нельзя.
            # Это неисправность, а не «не модем»: вердикт в кэш не заносится,
            # чтобы попытки восстановления имели смысл.
            reason = "ни на одном порту не удалось включить незапрошенные уведомления"
            log.warning("%s: %s", device.usb_path, reason)
            return ProbeResult(
                device=device,
                behavior=behavior,
                identity=identity,
                responding_ports=list(responding),
                reason=reason,
                fault=True,
            )

        port, baudrate = control
        log.info(
            "%s: модем %s (%s), управляющий порт %s на %d",
            device.usb_path,
            identity.description,
            behavior.family,
            port,
            baudrate,
        )
        return ProbeResult(
            device=device,
            ok=True,
            behavior=behavior,
            identity=identity,
            control_port=port,
            baudrate=baudrate,
            responding_ports=list(responding),
        )

    async def _identify(
        self, device: UsbDevice
    ) -> tuple[Identity, ModemBehavior | None, dict[str, int]]:
        """Ищет первый отвечающий порт и опознаёт по его ответу семейство.

        Возвращает вместе с опознанием скорости, на которых порты ответили: они
        уже проверены, и повторять перебор при выборе управляющего порта незачем.
        """
        responding: dict[str, int] = {}
        identity = Identity()
        behavior: ModemBehavior | None = None
        for port in device.ports:
            for baudrate in self._baudrates_for(port):
                answered, text = await self._ask_identity(port, baudrate)
                if not answered:
                    continue
                responding[port] = baudrate
                # Перебор скоростей прекращается на первом ответе.
                if behavior is None:
                    identity = parse_identity(text)
                    behavior = select(identity, hint=device.hint)
                break
            if behavior is not None:
                # Семейство определено; остальные порты проверит выбор управляющего.
                break
        return (identity, behavior, responding)

    def _baudrates_for(self, port: str, known: dict[str, int] | None = None) -> tuple[int, ...]:
        fixed = self.port_baudrate.get(port)
        if fixed:
            return (fixed,)
        if known and port in known:
            # Скорость этого порта уже подтверждена ответом -- перебирать нечего.
            return (known[port],)
        return self.baudrates

    async def _ask_identity(self, port: str, baudrate: int) -> tuple[bool, str]:
        """Открывает порт, проверяет отклик и собирает ответы опознания."""
        session = AtSession(self.transport_factory(port, baudrate), trace=self.trace)
        try:
            await session.open()
        except (PortGone, OSError) as exc:
            log.debug("%s: порт не открылся на %d (%s)", port, baudrate, exc)
            return (False, "")
        try:
            try:
                await session.execute("AT", timeout=self.timeout, retries=1)
            except (CommandTimeout, PortGone) as exc:
                log.debug("%s: нет отклика на %d (%s)", port, baudrate, exc)
                return (False, "")
            except CommandError:
                pass  # отказ -- тоже ответ: устройство говорит на AT
            await session.initialise(timeout=self.timeout)
            chunks: list[str] = []
            for command in IDENTITY_COMMANDS:
                try:
                    response = await session.execute(command, timeout=self.timeout)
                except AtError as exc:
                    log.debug("%s: %s не выполнена (%s)", port, command, exc)
                    continue
                if response.text:
                    chunks.append(response.text)
            imei = await self._read_imei(session)
            text = "\n".join(chunks)
            if imei:
                text = text + f"\nIMEI: {imei}"
            return (True, text)
        finally:
            await session.close()

    async def _read_imei(self, session: AtSession) -> str:
        for command in ("AT+CGSN", "AT+GSN"):
            try:
                response = await session.execute(command, timeout=self.timeout)
            except AtError:
                continue
            for line in response.lines:
                digits = "".join(char for char in line if char.isdigit())
                if len(digits) >= 14:
                    return digits
        return ""

    async def _choose_control_port(
        self,
        device: UsbDevice,
        behavior: ModemBehavior,
        responding: dict[str, int],
    ) -> tuple[str, int] | None:
        """Выбирает порт, на котором включаются незапрошенные уведомления.

        Порядок проверки предлагает семейство, но это только порядок: годность
        порта подтверждается тем, что на нём удалось выполнить инициализацию.
        """
        for port in behavior.rank_ports(device.ports):
            for baudrate in self._baudrates_for(port, responding):
                if await self._can_control(port, baudrate, behavior):
                    return (port, baudrate)
        return None

    async def _can_control(self, port: str, baudrate: int, behavior: ModemBehavior) -> bool:
        session = AtSession(
            self.transport_factory(port, baudrate),
            unsolicited_prefixes=behavior.unsolicited_prefixes,
            trace=self.trace,
        )
        try:
            await session.open()
        except (PortGone, OSError) as exc:
            log.debug("%s: порт не открылся на %d (%s)", port, baudrate, exc)
            return False
        try:
            await behavior.initialise(session)
        except AtError as exc:
            log.debug("%s: не годится как управляющий на %d (%s)", port, baudrate, exc)
            return False
        finally:
            await session.close()
        return True


async def probe_all(prober: Prober, devices: list[UsbDevice]) -> list[ProbeResult]:
    """Пробует устройства по очереди, изолируя сбой каждого.

    Последовательно, а не параллельно: одновременное открытие нескольких портов
    одного USB-устройства даёт неверные результаты.
    """
    results: list[ProbeResult] = []
    for device in devices:
        try:
            results.append(await prober.probe(device))
        except Exception as exc:
            log.exception("%s: сбой пробы", device.usb_path)
            results.append(ProbeResult(device=device, reason=f"сбой пробы: {exc}"))
    return results
