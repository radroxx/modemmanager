"""Обслуживание одного модема.

Модем -- это USB-устройство, а не порт: обслуживание идёт через один управляющий
порт, выбранный пробой. Здесь собраны состояние модема, регулярный опрос и
разбор незапрошенных сообщений; всё, что зависит от семейства, спрашивается у
поведения, а всё, что относится к SIM, сообщениям и вызовам, добавляется
отдельными частями через ``components``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from .at.errors import AtError, PortGone, RecoveryExhausted
from .at.recovery import RecoveryLadder, RecoveryPolicy
from .at.session import AtSession
from .at.transport import Transport
from .behaviors import Kind, ModemBehavior, Unsolicited
from .config import IntervalSettings
from .discovery.probe import ProbeResult
from .discovery.sysfs import UsbDevice
from .events import Event, EventBus, EventType
from .values import (
    Identity,
    Operator,
    PinAttempts,
    Registration,
    Signal,
    SimState,
    Storage,
)

log = logging.getLogger(__name__)


class ModemStatus(str, Enum):
    """Состояние обслуживания модема."""

    STARTING = "starting"
    ONLINE = "online"
    NO_SIM = "no_sim"
    PIN_REQUIRED = "pin_required"
    PUK_LOCKED = "puk_locked"
    NO_SERVICE = "no_service"
    SCANNING = "scanning"
    FAULT = "fault"
    GONE = "gone"


#: Состояния, в которых модем считается обслуживаемым.
ACTIVE_STATUSES = frozenset(
    {
        ModemStatus.STARTING,
        ModemStatus.ONLINE,
        ModemStatus.NO_SERVICE,
        ModemStatus.SCANNING,
    }
)


class Component(Protocol):
    """Часть обслуживания модема: SIM, приём сообщений, вызовы, оператор."""

    async def start(self, modem: Modem) -> None: ...

    async def stop(self) -> None: ...

    async def handle(self, event: Unsolicited) -> None: ...

    async def poll(self) -> None: ...


@dataclass
class ModemState:
    """Наблюдаемое состояние модема -- то, что показывает интерфейс и метрики."""

    usb_path: str
    control_port: str = ""
    baudrate: int = 0
    family: str = "generic"
    identity: Identity = field(default_factory=Identity)
    status: ModemStatus = ModemStatus.STARTING
    sim_state: SimState = SimState.UNKNOWN
    imsi: str = ""
    sim_label: str = ""
    signal: Signal = field(default_factory=Signal.unknown)
    registration: Registration = field(default_factory=Registration)
    operator: Operator = field(default_factory=Operator)
    storage: Storage = field(default_factory=Storage)
    pin_attempts: PinAttempts = field(default_factory=PinAttempts)
    #: Когда последний опрос завершился успешно.
    last_poll: float = 0.0
    #: Когда последнее сообщение было принято этим модемом.
    last_sms: float = 0.0
    reconnects: int = 0
    error_count: int = 0
    fault_reason: str = ""

    @property
    def imei(self) -> str:
        return self.identity.imei

    @property
    def label(self) -> str:
        """Как модем называется в уведомлениях и журнале."""
        return self.sim_label or self.imsi or self.identity.description or self.usb_path

    def public_dict(self) -> dict[str, Any]:
        """Состояние для интерфейса. PIN-код и прочие секреты сюда не попадают."""
        return {
            "usb_path": self.usb_path,
            "control_port": self.control_port,
            "baudrate": self.baudrate,
            "family": self.family,
            "model": self.identity.description,
            "imei": self.imei,
            "status": self.status.value,
            "sim_state": self.sim_state.value,
            "imsi": self.imsi,
            "sim_label": self.sim_label,
            "signal_dbm": self.signal.dbm,
            "signal_bars": self.signal.bars,
            "registration_voice": self.registration.voice.value,
            "registration_data": self.registration.data.value,
            "roaming": self.registration.roaming,
            "operator_plmn": self.operator.plmn,
            "operator_name": self.operator.name,
            "operator_manual": self.operator.manual,
            "storage_used": self.storage.used,
            "storage_total": self.storage.total,
            "storage_full": self.storage.full,
            "pin_attempts": self.pin_attempts.pin,
            "pin_attempts_known": self.pin_attempts.known,
            "last_poll": self.last_poll or None,
            "last_sms": self.last_sms or None,
            "reconnects": self.reconnects,
            "error_count": self.error_count,
            "fault_reason": self.fault_reason,
        }


class Modem:
    """Обслуживание одного модема: опрос, события, восстановление."""

    def __init__(
        self,
        *,
        device: UsbDevice,
        behavior: ModemBehavior,
        transport: Transport,
        bus: EventBus,
        identity: Identity | None = None,
        baudrate: int = 0,
        intervals: IntervalSettings | None = None,
        components: Sequence[Component] = (),
        recovery_policy: RecoveryPolicy | None = None,
        on_lost: Callable[[Modem], None] | None = None,
        trace: bool = False,
    ):
        self.device = device
        self.behavior = behavior
        self.bus = bus
        self.intervals = intervals or IntervalSettings()
        self.components: list[Component] = list(components)
        #: Кого известить, что устройство исчезло, не дожидаясь цикла сверки.
        self.on_lost = on_lost

        self.session = AtSession(
            transport,
            on_unsolicited=self._on_unsolicited,
            on_gone=self._on_gone,
            unsolicited_prefixes=behavior.unsolicited_prefixes,
            trace=trace,
        )
        self.ladder = RecoveryLadder(
            session=self.session,
            initialise=behavior.initialise,
            reset=behavior.reset if behavior.supports_reset else None,
            policy=recovery_policy or RecoveryPolicy(),
        )
        self.state = ModemState(
            usb_path=device.usb_path,
            control_port=getattr(transport, "port", ""),
            baudrate=baudrate,
            family=behavior.family,
            identity=identity or Identity(),
        )

        self._poll_task: asyncio.Task | None = None
        self._queue: asyncio.Queue[Unsolicited] = asyncio.Queue()
        self._dispatch_task: asyncio.Task | None = None
        self._stopping = False
        #: Когда модем сам присылал уровень сигнала последний раз.
        self._signal_pushed_at = 0.0
        self._next: dict[str, float] = {}

    # ---------------------------------------------------------------- фабрика

    @classmethod
    def from_probe(
        cls,
        result: ProbeResult,
        *,
        transport: Transport,
        bus: EventBus,
        **kwargs,
    ) -> Modem:
        assert result.behavior is not None
        return cls(
            device=result.device,
            behavior=result.behavior,
            transport=transport,
            bus=bus,
            identity=result.identity,
            baudrate=result.baudrate,
            **kwargs,
        )

    # ------------------------------------------------------------------ запуск

    @property
    def usb_path(self) -> str:
        return self.device.usb_path

    @property
    def key(self) -> str:
        """Устойчивый ключ модема: IMEI, если он известен, иначе путь на шине."""
        return self.state.imei or self.usb_path

    @property
    def alive(self) -> bool:
        return self.state.status in ACTIVE_STATUSES

    def find_component(self, kind: type) -> Any:
        """Возвращает часть обслуживания указанного типа, если она подключена.

        Веб-интерфейс запускает поиск сетей или применение оператора у
        конкретного модема, не зная, чем он оборудован; ``find_component``
        отдаёт часть, а вызывающий сам решает, что с ней делать.
        """
        for component in self.components:
            if isinstance(component, kind):
                return component
        return None

    async def start(self) -> None:
        """Открывает порт, инициализирует модем и запускает опрос.

        Событие появления публикует вызывающий: только он знает, новый это модем
        или тот же самый после короткого переподключения.
        """
        self._stopping = False
        await self.session.open()
        await self.behavior.initialise(self.session)
        self._dispatch_task = asyncio.create_task(
            self._dispatch_loop(), name=f"modem-events:{self.usb_path}"
        )
        for component in self.components:
            await component.start(self)
        # Часть обслуживания уже могла перевести модем в PIN_REQUIRED, NO_SIM или
        # PUK_LOCKED: свою оценку она сделала лучше, чем мы отсюда, поэтому
        # значение по умолчанию ставим только если состояние ещё стартовое.
        if self.state.status is ModemStatus.STARTING:
            self.state.status = ModemStatus.ONLINE
        await self.poll_once()
        self._poll_task = asyncio.create_task(self._poll_loop(), name=f"modem-poll:{self.usb_path}")

    async def stop(self, *, reason: str = "") -> None:
        """Прекращает обслуживание. Не публикует событий: это делает вызывающий."""
        self._stopping = True
        for task in (self._poll_task, self._dispatch_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._poll_task = None
        self._dispatch_task = None
        for component in reversed(self.components):
            try:
                await component.stop()
            except Exception:
                log.exception("%s: сбой остановки части обслуживания", self.usb_path)
        await self.session.close()
        if reason:
            log.info("%s: обслуживание прекращено (%s)", self.usb_path, reason)

    # ------------------------------------------------------------------- опрос

    async def _poll_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._sleep_for())
            if self._stopping:
                return
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except PortGone:
                return  # исчезновение обрабатывается через on_gone
            except RecoveryExhausted as exc:
                await self.mark_fault(str(exc))
                return
            except Exception:
                log.exception("%s: сбой опроса", self.usb_path)
                self.state.error_count += 1

    def _sleep_for(self) -> float:
        """Пауза до ближайшего запланированного опроса."""
        now = time.monotonic()
        due = [moment - now for moment in self._next.values()]
        if not due:
            return min(self.intervals.signal, self.intervals.registration)
        return max(0.5, min(due))

    def _is_due(self, name: str, interval: float) -> bool:
        now = time.monotonic()
        if self._next.get(name, 0.0) <= now:
            self._next[name] = now + interval
            return True
        return False

    async def poll_once(self) -> None:
        """Один проход регулярного опроса."""
        signal_interval = self.intervals.signal
        if self.behavior.pushes_signal:
            # Модем присылает уровень сам -- опрашивать часто незачем.
            signal_interval = max(signal_interval, self.intervals.registration) * 4
        if self._is_due("signal", signal_interval):
            self.state.signal = await self.behavior.read_signal(self.session)
        if self._is_due("registration", self.intervals.registration):
            self.state.registration = await self.behavior.read_registration(self.session)
            self.state.operator = await self.behavior.read_operator(self.session)
        if self._is_due("storage", self.intervals.storage):
            self.state.storage = await self.behavior.read_storage(self.session)

        for component in self.components:
            try:
                await component.poll()
            except AtError as exc:
                log.warning("%s: часть обслуживания не опрошена (%s)", self.usb_path, exc)

        self.state.last_poll = time.time()
        self.state.error_count = self.session.error_count

    # ------------------------------------------------- незапрошенные сообщения

    def _on_unsolicited(self, line: str) -> int:
        """Разбирает строку и ставит её в очередь обработки.

        Вызывается из читающего цикла, поэтому не должен ждать: разбор дешёвый,
        а всё остальное делается в отдельной задаче.
        """
        try:
            parsed = self.behavior.classify(line)
        except Exception:
            log.exception("%s: сбой разбора %r", self.usb_path, line)
            return 0
        self._queue.put_nowait(parsed)
        return parsed.continuation

    async def _dispatch_loop(self) -> None:
        while True:
            parsed = await self._queue.get()
            try:
                await self._handle(parsed)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s: сбой обработки %r", self.usb_path, parsed.raw)

    async def _handle(self, parsed: Unsolicited) -> None:
        if parsed.kind == Kind.SIGNAL:
            self._apply_pushed_signal(parsed)
        elif parsed.kind == Kind.REGISTRATION:
            # Состояние сети меняется: перечитать при ближайшем опросе.
            self._next.pop("registration", None)
        elif parsed.kind == Kind.STORAGE_FULL:
            self._next.pop("storage", None)
            log.warning("%s: модем сообщает, что память сообщений заполнена", self.usb_path)
        elif parsed.kind == Kind.BOOT:
            log.info("%s: модем сообщает о запуске (%s)", self.usb_path, parsed.raw)

        for component in self.components:
            await component.handle(parsed)

    def _apply_pushed_signal(self, parsed: Unsolicited) -> None:
        """Обновляет уровень сигнала по сообщению модема, не дожидаясь опроса."""
        if "rssi" in parsed.data:
            self.state.signal = Signal.from_csq(int(parsed.data["rssi"]))
        elif "bars" in parsed.data:
            self.state.signal = Signal.from_bars(
                int(parsed.data["bars"]), int(parsed.data.get("scale", 5))
            )
        else:
            return
        self._signal_pushed_at = time.monotonic()

    # -------------------------------------------------------------- состояния

    def _on_gone(self, exc: PortGone) -> None:
        """Ошибка чтения порта равносильна исчезновению устройства."""
        if self.state.status is ModemStatus.GONE:
            return
        log.warning("%s: порт сообщил об исчезновении устройства", self.usb_path)
        self.state.status = ModemStatus.GONE
        self.state.fault_reason = str(exc)
        if self.on_lost is None:
            return
        try:
            self.on_lost(self)
        except Exception:
            log.exception("%s: сбой обработчика потери устройства", self.usb_path)

    async def mark_fault(self, reason: str) -> None:
        self.state.status = ModemStatus.FAULT
        self.state.fault_reason = reason
        await self._publish(EventType.MODEM_FAULT, {"reason": reason})

    async def mark_recovered(self) -> None:
        self.state.status = ModemStatus.ONLINE
        self.state.fault_reason = ""
        await self._publish(EventType.MODEM_RECOVERED, {})

    def event(self, event_type: str, data: dict[str, Any] | None = None) -> Event:
        """Собирает событие, подставляя признаки этого модема."""
        return Event(
            type=event_type,
            imsi=self.state.imsi or None,
            sim_label=self.state.sim_label or None,
            imei=self.state.imei or None,
            usb_path=self.usb_path,
            tty=self.state.control_port or None,
            data=dict(data or {}),
        )

    async def _publish(self, event_type: str, data: dict[str, Any]) -> None:
        await self.bus.publish(self.event(event_type, data))

    def __repr__(self) -> str:  # pragma: no cover -- диагностика
        return f"<Modem {self.usb_path} {self.behavior.family} {self.state.status.value}>"
