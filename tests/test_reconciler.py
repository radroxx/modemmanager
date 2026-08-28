"""Цикл сверки: появление, пропадание, дебаунс, изоляция неисправностей."""

from __future__ import annotations

import asyncio

import pytest
from fake_answers import HUAWEI, HUAWEI_SECOND, NO_NOTIFICATIONS
from fake_modem import FakePortSet

from modemmanager.config import SettingsStore
from modemmanager.discovery.reconciler import Reconciler
from modemmanager.discovery.sysfs import UsbDevice
from modemmanager.events import Event, EventBus, EventType
from modemmanager.modem import ModemStatus
from modemmanager.modem_registry import ModemRegistry


def device(usb_path: str, *ports: str) -> UsbDevice:
    return UsbDevice(
        usb_path=usb_path,
        vendor_id="12d1",
        product_id="1001",
        manufacturer="HUAWEI Technology",
        product="HUAWEI Mobile",
        ports=list(ports),
        drivers={"option1"},
    )


class Harness:
    """Цикл сверки поверх поддельного sysfs и поддельных портов."""

    def __init__(self, tmp_path, ports, *, devices=(), **discovery):
        self.store = SettingsStore(tmp_path / "settings.json")
        self.store.load()
        settings = self.store.settings.discovery
        settings.probe_timeout = 0.05
        settings.probe_max_attempts = 1
        settings.probe_retry_backoff = 0.0
        settings.scan_interval = 0.01
        settings.gone_debounce = 5.0
        settings.fault_retry_interval = 60.0
        for key, value in discovery.items():
            setattr(settings, key, value)

        self.bus = EventBus()
        self.events: list[Event] = []
        self.bus.subscribe(self.events.append, priority=0, name="recorder")
        self.registry = ModemRegistry()
        self.ports = FakePortSet(ports)
        #: Устройства, которые «видит» система; тест меняет список между проходами.
        self.devices = list(devices)
        self.reconciler = Reconciler(
            store=self.store,
            bus=self.bus,
            registry=self.registry,
            transport_factory=self.ports.factory,
            enumerator=self.enumerate,
        )

    def enumerate(self, drivers, *, sysfs_root="/sys", dev_root="/dev"):
        return list(self.devices)

    def types(self, event_type: str) -> list[Event]:
        return [event for event in self.events if event.type == event_type]

    async def sweep(self) -> None:
        await self.reconciler.sweep()
        # Обработчики потери порта работают отдельными задачами.
        await asyncio.sleep(0)


@pytest.fixture
def one_modem(tmp_path):
    return Harness(
        tmp_path,
        {"/dev/ttyUSB0": HUAWEI},
        devices=[device("3-1", "/dev/ttyUSB0")],
    )


# ------------------------------------------------------------------ появление

async def test_component_factory_attaches_parts_to_new_modem(tmp_path):
    """Заводимая фабрика частей попадает на каждый новый модем."""
    from modemmanager.modem import Modem

    class Marker:
        async def start(self, modem: Modem) -> None: ...
        async def stop(self) -> None: ...
        async def handle(self, event) -> None: ...
        async def poll(self) -> None: ...

    harness = Harness(
        tmp_path,
        {"/dev/ttyUSB0": HUAWEI},
        devices=[device("3-1", "/dev/ttyUSB0")],
    )
    harness.reconciler.component_factory = lambda _modem: [Marker(), Marker()]

    await harness.sweep()

    modem = harness.registry.get("3-1")
    assert modem is not None
    assert len(modem.components) == 2
    assert all(isinstance(component, Marker) for component in modem.components)
    await harness.reconciler.stop()


async def test_at_trace_reaches_the_serving_session(tmp_path, caplog):
    """Настройка at_trace прокидывается и в модем: обслуживание тоже трассируется."""
    import logging

    harness = Harness(
        tmp_path,
        {"/dev/ttyUSB0": HUAWEI},
        devices=[device("3-1", "/dev/ttyUSB0")],
        at_trace=True,
    )

    with caplog.at_level(logging.DEBUG, logger="modemmanager.at.session"):
        await harness.sweep()

    modem = harness.registry.get("3-1")
    assert modem is not None
    assert modem.session.trace is True
    assert harness.reconciler.prober.trace is True
    assert any(
        "/dev/ttyUSB0" in record.getMessage()
        and (" > AT" in record.getMessage() or " < " in record.getMessage())
        for record in caplog.records
    )
    await harness.reconciler.stop()


async def test_new_device_is_taken_into_service(one_modem):
    await one_modem.sweep()

    modem = one_modem.registry.get("3-1")
    assert modem is not None
    assert modem.state.status is ModemStatus.ONLINE
    assert modem.state.family == "huawei"
    assert modem.state.imei == "861234567890123"
    assert modem.state.signal.dbm == -79
    assert [event.type for event in one_modem.events] == [EventType.MODEM_UP]
    assert one_modem.events[0].usb_path == "3-1"
    await one_modem.reconciler.stop()


async def test_appearing_device_needs_no_restart(one_modem):
    """Второй модем подхватывается на следующем проходе, первый не тронут."""
    await one_modem.sweep()
    first = one_modem.registry.get("3-1")

    one_modem.ports.tables["/dev/ttyUSB1"] = HUAWEI_SECOND
    one_modem.devices.append(device("3-2", "/dev/ttyUSB1"))
    await one_modem.sweep()

    assert len(one_modem.registry) == 2
    assert one_modem.registry.get("3-1") is first
    assert len(one_modem.types(EventType.MODEM_UP)) == 2
    await one_modem.reconciler.stop()


async def test_repeated_sweeps_do_not_restart_a_healthy_modem(one_modem):
    await one_modem.sweep()
    modem = one_modem.registry.get("3-1")
    opens = modem.session.transport.opened

    await one_modem.sweep()
    await one_modem.sweep()

    assert one_modem.registry.get("3-1") is modem
    assert modem.session.transport.opened == opens
    assert len(one_modem.types(EventType.MODEM_UP)) == 1
    await one_modem.reconciler.stop()


# ------------------------------------------------------------------ пропадание

async def test_disappeared_device_stops_being_served(tmp_path):
    """Дебаунс истёк -- обслуживание прекращено, администратор уведомлён."""
    harness = Harness(
        tmp_path,
        {"/dev/ttyUSB0": HUAWEI},
        devices=[device("3-1", "/dev/ttyUSB0")],
        gone_debounce=0.0,
    )
    await harness.sweep()

    harness.devices.clear()
    await harness.sweep()

    assert len(harness.registry) == 0
    gone = harness.types(EventType.MODEM_GONE)
    assert len(gone) == 1
    assert gone[0].usb_path == "3-1"
    assert gone[0].imei == "861234567890123"
    assert harness.ports.transports[-1].is_open is False
    await harness.reconciler.stop()


async def test_short_disappearance_is_not_a_loss(one_modem):
    """Модем пропал и вернулся в пределах дебаунса: уведомления нет, счётчик вырос."""
    await one_modem.sweep()

    one_modem.devices.clear()
    await one_modem.sweep()
    assert one_modem.types(EventType.MODEM_GONE) == []

    one_modem.devices.append(device("3-1", "/dev/ttyUSB0"))
    await one_modem.sweep()

    modem = one_modem.registry.get("3-1")
    assert modem is not None
    assert modem.state.status is ModemStatus.ONLINE
    assert modem.state.reconnects == 1
    assert one_modem.types(EventType.MODEM_GONE) == []
    assert len(one_modem.types(EventType.MODEM_RECOVERED)) == 1
    await one_modem.reconciler.stop()


async def test_reconnect_counter_grows_with_every_return(one_modem):
    await one_modem.sweep()
    for _ in range(2):
        one_modem.devices.clear()
        await one_modem.sweep()
        one_modem.devices.append(device("3-1", "/dev/ttyUSB0"))
        await one_modem.sweep()

    assert one_modem.registry.get("3-1").state.reconnects == 2
    await one_modem.reconciler.stop()


async def test_read_error_is_treated_as_disappearance(one_modem):
    """Ошибка чтения порта равносильна исчезновению, ждать сверки не нужно."""
    await one_modem.sweep()
    modem = one_modem.registry.get("3-1")
    transport = modem.session.transport

    transport.disappear("устройство отключено")
    await asyncio.sleep(0)  # читающий цикл замечает исчезновение
    await asyncio.sleep(0)

    assert modem.state.status is ModemStatus.GONE
    assert "3-1" in one_modem.reconciler._missing

    # Устройство на месте: следующий проход поднимает обслуживание заново.
    await one_modem.sweep()

    restored = one_modem.registry.get("3-1")
    assert restored is not None and restored is not modem
    assert restored.state.status is ModemStatus.ONLINE
    assert restored.state.reconnects == 1
    assert one_modem.types(EventType.MODEM_GONE) == []
    await one_modem.reconciler.stop()


async def test_gone_device_is_probed_again_when_it_returns(tmp_path):
    """Вердикт «не модем» не переживает исчезновения устройства."""
    harness = Harness(
        tmp_path,
        {"/dev/ttyUSB0": None},
        devices=[device("3-1", "/dev/ttyUSB0")],
    )
    target = harness.devices[0]

    await harness.sweep()
    assert harness.reconciler.prober.rejected(target) is not None

    harness.devices.clear()
    await harness.sweep()

    assert harness.reconciler.prober.rejected(target) is None
    await harness.reconciler.stop()


# -------------------------------------------------------------- неисправность

async def test_faulty_device_does_not_affect_the_others(tmp_path):
    """Один модем неисправен -- остальные обслуживаются как обычно."""
    harness = Harness(
        tmp_path,
        {"/dev/ttyUSB0": NO_NOTIFICATIONS, "/dev/ttyUSB1": HUAWEI},
        devices=[device("3-1", "/dev/ttyUSB0"), device("3-2", "/dev/ttyUSB1")],
        fault_retry_interval=0.0,
    )

    for _ in range(3):
        await harness.sweep()

    healthy = harness.registry.get("3-2")
    assert healthy is not None and healthy.state.status is ModemStatus.ONLINE
    assert harness.registry.get("3-1") is None
    assert harness.reconciler.faults["3-1"]
    # Об одной и той же неисправности сообщают один раз.
    assert len(harness.types(EventType.MODEM_FAULT)) == 1
    assert len(harness.types(EventType.MODEM_UP)) == 1
    await harness.reconciler.stop()


async def test_faulty_device_is_retried(tmp_path):
    harness = Harness(
        tmp_path,
        {"/dev/ttyUSB0": NO_NOTIFICATIONS},
        devices=[device("3-1", "/dev/ttyUSB0")],
        fault_retry_interval=0.0,
    )
    await harness.sweep()
    attempts = len(harness.ports.transports)

    await harness.sweep()

    assert len(harness.ports.transports) > attempts
    await harness.reconciler.stop()


async def test_retry_waits_for_the_interval(tmp_path):
    """Между попытками неисправное устройство не трогают."""
    harness = Harness(
        tmp_path,
        {"/dev/ttyUSB0": NO_NOTIFICATIONS},
        devices=[device("3-1", "/dev/ttyUSB0")],
        fault_retry_interval=60.0,
    )
    await harness.sweep()
    attempts = len(harness.ports.transports)

    await harness.sweep()

    assert len(harness.ports.transports) == attempts
    await harness.reconciler.stop()


async def test_recovered_device_reports_recovery(tmp_path):
    harness = Harness(
        tmp_path,
        {"/dev/ttyUSB0": NO_NOTIFICATIONS},
        devices=[device("3-1", "/dev/ttyUSB0")],
        fault_retry_interval=0.0,
    )
    await harness.sweep()

    harness.ports.tables["/dev/ttyUSB0"] = HUAWEI
    await harness.sweep()

    modem = harness.registry.get("3-1")
    assert modem is not None and modem.state.status is ModemStatus.ONLINE
    assert len(harness.types(EventType.MODEM_RECOVERED)) == 1
    assert harness.reconciler.faults == {}
    await harness.reconciler.stop()


async def test_broken_enumeration_does_not_stop_the_loop(one_modem):
    """Сбой перечисления не должен ронять цикл сверки."""
    await one_modem.sweep()
    calls = {"n": 0}
    good = one_modem.enumerate

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("sysfs недоступен")
        return good(*args, **kwargs)

    one_modem.reconciler.enumerator = flaky
    await one_modem.reconciler.start()
    await asyncio.sleep(0.05)
    await one_modem.reconciler.stop()

    assert calls["n"] >= 2
    assert one_modem.registry.paths == []  # остановка убрала модемы из списка


# -------------------------------------------------------------------- остановка

async def test_stop_releases_every_port(tmp_path):
    harness = Harness(
        tmp_path,
        {"/dev/ttyUSB0": HUAWEI, "/dev/ttyUSB1": HUAWEI_SECOND},
        devices=[device("3-1", "/dev/ttyUSB0"), device("3-2", "/dev/ttyUSB1")],
    )
    await harness.sweep()
    assert len(harness.registry) == 2

    await harness.reconciler.stop()

    assert len(harness.registry) == 0
    assert all(not transport.is_open for transport in harness.ports.transports)


async def test_loop_sweeps_until_stopped(one_modem):
    await one_modem.reconciler.start()
    await asyncio.sleep(0.05)
    await one_modem.reconciler.stop()

    assert one_modem.reconciler.sweeps >= 2
