"""Обслуживание одного модема: опрос, незапрошенные сообщения, состояния."""

from __future__ import annotations

import asyncio

from fake_answers import HUAWEI
from fake_modem import FakeTransport

from modemmanager.behaviors import HuaweiBehavior, Kind, Sim800Behavior, Unsolicited
from modemmanager.config import IntervalSettings
from modemmanager.discovery.sysfs import UsbDevice
from modemmanager.events import Event, EventBus, EventType
from modemmanager.modem import Modem, ModemStatus
from modemmanager.modem_registry import ModemRegistry
from modemmanager.values import Identity


class RecordingComponent:
    """Часть обслуживания, запоминающая всё, что ей досталось."""

    def __init__(self) -> None:
        self.modem: Modem | None = None
        self.events: list[Unsolicited] = []
        self.polls = 0
        self.stopped = 0

    async def start(self, modem: Modem) -> None:
        self.modem = modem

    async def stop(self) -> None:
        self.stopped += 1

    async def handle(self, event: Unsolicited) -> None:
        self.events.append(event)

    async def poll(self) -> None:
        self.polls += 1


def make_modem(
    *,
    responses: dict[str, object] | None = None,
    components=(),
    intervals: IntervalSettings | None = None,
    on_lost=None,
    behavior=None,
) -> tuple[Modem, FakeTransport, list[Event]]:
    transport = FakeTransport(dict(responses or HUAWEI), port="/dev/ttyUSB0")
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(events.append, priority=0, name="recorder")
    modem = Modem(
        device=UsbDevice(usb_path="3-1", ports=["/dev/ttyUSB0"], drivers={"option1"}),
        behavior=behavior or HuaweiBehavior(),
        transport=transport,
        bus=bus,
        identity=Identity(manufacturer="huawei", model="E3372", imei="861234567890123"),
        baudrate=115200,
        intervals=intervals or IntervalSettings(),
        components=components,
        on_lost=on_lost,
    )
    return (modem, transport, events)


# ------------------------------------------------------------------- запуск

async def test_start_initialises_and_polls():
    modem, transport, _events = make_modem()

    await modem.start()

    assert modem.state.status is ModemStatus.ONLINE
    assert "AT+CMGF=0" in transport.commands
    assert modem.state.signal.dbm == -79
    assert modem.state.operator.plmn == "25002"
    assert modem.state.storage.used == 3
    assert modem.state.registration.roaming is False
    await modem.stop()


async def test_start_does_not_announce_by_itself():
    """О появлении модема сообщает цикл сверки: только он знает, новый ли он."""
    modem, _transport, events = make_modem()

    await modem.start()

    assert events == []
    await modem.stop()


async def test_components_are_started_polled_and_stopped():
    component = RecordingComponent()
    modem, _transport, _events = make_modem(components=[component])

    await modem.start()
    await modem.poll_once()
    await modem.stop()

    assert component.modem is modem
    assert component.polls >= 1
    assert component.stopped == 1


async def test_stop_closes_the_port_and_the_loops():
    modem, transport, _events = make_modem()
    await modem.start()

    await modem.stop(reason="проверка")

    assert transport.is_open is False
    assert modem.session.alive is False


# ------------------------------------------------- незапрошенные сообщения

async def test_pushed_signal_updates_state_without_polling():
    """Huawei присылает уровень сам -- ждать опроса незачем."""
    modem, transport, _events = make_modem()
    await modem.start()
    before = modem.state.signal.dbm

    transport.queue_unsolicited("^RSSI:31")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert modem.state.signal.dbm == -51
    assert modem.state.signal.dbm != before
    await modem.stop()


async def test_pushed_bars_are_normalised():
    """SIM800 присылает шкалу в делениях -- в состоянии она приведена к дБм."""
    modem, transport, _events = make_modem(behavior=Sim800Behavior())
    await modem.start()
    assert modem.state.signal.dbm == -79  # значение из опроса

    transport.queue_unsolicited("+CIEV: 2,5")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert modem.state.signal.bars == 5
    assert modem.state.signal.dbm != -79
    await modem.stop()


async def test_unsolicited_messages_reach_the_components():
    component = RecordingComponent()
    modem, transport, _events = make_modem(components=[component])
    await modem.start()

    transport.queue_unsolicited("RING", '+CLIP: "+79990001122",145')
    for _ in range(4):
        await asyncio.sleep(0)

    kinds = [event.kind for event in component.events]
    assert Kind.RING in kinds
    assert Kind.CALLER_ID in kinds
    await modem.stop()


async def test_failing_component_does_not_break_the_dispatch():
    class Broken(RecordingComponent):
        async def handle(self, event):
            raise RuntimeError("обработчик сломался")

    broken = Broken()
    good = RecordingComponent()
    modem, transport, _events = make_modem(components=[broken, good])
    await modem.start()

    transport.queue_unsolicited("RING", "RING")
    for _ in range(6):
        await asyncio.sleep(0)

    # Первая часть падает, вторая всё равно получает следующее сообщение.
    assert modem.state.status is ModemStatus.ONLINE
    await modem.stop()


async def test_registration_change_schedules_a_reread():
    modem, transport, _events = make_modem(
        intervals=IntervalSettings(registration=3600.0)
    )
    await modem.start()
    assert "registration" in modem._next

    transport.queue_unsolicited("+CREG: 1")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert "registration" not in modem._next
    await modem.stop()


# ------------------------------------------------------------------ состояния

async def test_read_error_marks_the_modem_gone_and_reports_it():
    lost: list[Modem] = []
    modem, transport, _events = make_modem(on_lost=lost.append)
    await modem.start()

    transport.disappear("устройство отключено")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert modem.state.status is ModemStatus.GONE
    assert lost == [modem]
    await modem.stop()


async def test_fault_and_recovery_are_published():
    modem, _transport, events = make_modem()
    await modem.start()

    await modem.mark_fault("порт не отвечает")
    assert modem.state.status is ModemStatus.FAULT
    await modem.mark_recovered()

    assert [event.type for event in events] == [
        EventType.MODEM_FAULT,
        EventType.MODEM_RECOVERED,
    ]
    assert events[0].data["reason"] == "порт не отвечает"
    assert events[0].imei == "861234567890123"
    assert modem.state.fault_reason == ""
    await modem.stop()


async def test_event_carries_the_identity_of_the_modem():
    modem, _transport, _events = make_modem()
    modem.state.imsi = "89701010000000000001"
    modem.state.sim_label = "рабочая"

    record = modem.event(EventType.SMS, {"text": "привет"}).to_record()

    assert record["imsi"] == "89701010000000000001"
    assert record["sim_label"] == "рабочая"
    assert record["imei"] == "861234567890123"
    assert record["usb_path"] == "3-1"
    assert record["text"] == "привет"


async def test_label_falls_back_to_hardware_when_sim_is_unknown():
    modem, _transport, _events = make_modem()

    assert modem.state.label == "huawei E3372"

    modem.state.imsi = "89701010000000000001"
    assert modem.state.label == "89701010000000000001"

    modem.state.sim_label = "рабочая"
    assert modem.state.label == "рабочая"


async def test_public_state_never_contains_secrets():
    modem, _transport, _events = make_modem()
    await modem.start()

    public = modem.state.public_dict()

    assert "pin" not in public
    assert public["pin_attempts_known"] is False
    assert public["status"] == "online"
    assert public["family"] == "huawei"
    await modem.stop()


# ------------------------------------------------------------------ регистратор

async def test_registry_finds_modems_by_every_identifier():
    modem, _transport, _events = make_modem()
    modem.state.imsi = "89701010000000000001"
    registry = ModemRegistry()

    registry.add(modem)

    assert registry.get("3-1") is modem
    assert registry.by_imsi("89701010000000000001") is modem
    assert registry.by_imei("861234567890123") is modem
    assert registry.by_imsi("нет такой") is None
    assert len(registry) == 1
    assert "3-1" in registry


async def test_registry_counts_states():
    modem, _transport, _events = make_modem()
    registry = ModemRegistry()
    registry.add(modem)

    counts = registry.counts()

    assert counts["starting"] == 1
    assert counts["online"] == 0


async def test_registry_marks_the_time_of_the_last_message():
    modem, _transport, _events = make_modem()
    modem.state.imsi = "89701010000000000001"
    registry = ModemRegistry()
    registry.add(modem)

    await registry.on_event(
        Event(type=EventType.SMS, at=1000.0, usb_path="3-1", imsi=modem.state.imsi)
    )

    assert modem.state.last_sms == 1000.0
    assert registry.snapshot()[0]["last_sms"] == 1000.0


async def test_registry_ignores_events_of_unknown_modems():
    registry = ModemRegistry()

    await registry.on_event(Event(type=EventType.SMS, usb_path="9-9"))

    assert registry.snapshot() == []
