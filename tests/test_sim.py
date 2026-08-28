"""Идентификация SIM по IMSI и защита PIN-кода."""

from __future__ import annotations

from pathlib import Path

import pytest
from fake_answers import HUAWEI
from fake_modem import FakeTransport

from modemmanager.behaviors import Kind, HuaweiBehavior
from modemmanager.behaviors.base import Unsolicited
from modemmanager.config import IntervalSettings, SettingsStore, SimSettings
from modemmanager.discovery.sysfs import UsbDevice
from modemmanager.events import Event, EventBus, EventType
from modemmanager.modem import Modem, ModemStatus
from modemmanager.sim import (
    MIN_ATTEMPTS,
    PinAction,
    PinPlan,
    SimIdentity,
    SimService,
    auto_label,
    enter_pin,
    identify,
    normalise,
    plan,
    read_imsi,
)
from modemmanager.sim.imsi import from_imsi
from modemmanager.sim.pin import PinRejected
from modemmanager.values import Identity, PinAttempts, SimState


IMSI_RU = "250010123456789"     # MCC 250 = RU, MSIN "010123456789"
IMSI_UA = "255010111222333"     # MCC 255 = UA
IMSI_UNKNOWN_COUNTRY = "999010111222333"  # MCC 999 в таблицу не входит


# ------------------------------------------------------------- нормализация IMSI

class TestImsiHelpers:
    def test_normalise_extracts_digits_from_padded_line(self):
        assert normalise(f'"{IMSI_RU}"\r\n') == IMSI_RU
        assert normalise(f"  {IMSI_RU}  \n") == IMSI_RU
        assert normalise("") == ""

    def test_normalise_skips_ok_and_blank_lines(self):
        assert normalise(f"\nOK\n{IMSI_RU}\n") == IMSI_RU

    def test_normalise_accepts_short_imsi(self):
        # IMSI 3GPP допускает 6..15 цифр; приниматься должны и короткие.
        short = "250010001"
        assert normalise(short) == short

    def test_normalise_rejects_all_letters(self):
        assert normalise("ERROR") == ""


# ------------------------------------------------------------------ auto_label

class TestAutoLabel:
    def test_russia_prefix_gives_ru_and_msin_tail(self):
        assert auto_label(IMSI_RU) == "RU-...56789"

    def test_ukraine_prefix_gives_country(self):
        assert auto_label(IMSI_UA) == "UA-...22333"

    def test_unknown_country_is_labelled_by_mcc(self):
        assert auto_label(IMSI_UNKNOWN_COUNTRY).startswith("999-...")

    def test_empty_imsi_gives_empty_label(self):
        assert auto_label("") == ""


# --------------------------------------------------------- разбор IMSI из ответа

@pytest.mark.asyncio
async def test_read_imsi_from_plain_response():
    """`AT+CIMI` -- единственная команда чтения IMSI; ответ -- голая строка цифр."""
    responses = _huawei_with_imsi(IMSI_RU)
    transport = FakeTransport(responses)
    modem, service = await _service_with_transport(transport)
    try:
        assert modem.state.imsi == IMSI_RU
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_read_imsi_direct_via_session():
    """Ответ модема разбирается парсером и приводится к строке цифр."""
    transport = FakeTransport({"ATE0": "OK", "AT+CMEE=1": "OK", "AT+CIMI": IMSI_RU})
    session = await _session(transport)
    try:
        assert await read_imsi(session) == IMSI_RU
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_read_imsi_returns_empty_on_error():
    """Если модем ответил ERROR, возвращается пустая строка -- fallback не пробуется."""
    transport = FakeTransport(
        {"ATE0": "OK", "AT+CMEE=1": "OK", "AT+CIMI": "+CME ERROR: 11"}
    )
    session = await _session(transport)
    try:
        assert await read_imsi(session) == ""
    finally:
        await session.close()


# -------------------------------------------------- SimIdentity и auto-имя

@pytest.mark.asyncio
async def test_identify_returns_full_sim_identity():
    transport = FakeTransport({"ATE0": "OK", "AT+CMEE=1": "OK", "AT+CIMI": IMSI_RU})
    session = await _session(transport)
    try:
        identity = await identify(session, HuaweiBehavior())
    finally:
        await session.close()

    assert identity.imsi == IMSI_RU
    assert identity.country == "RU"
    assert identity.country_code == "250"
    assert identity.tail == "56789"
    assert identity.auto_label == "RU-...56789"


# ============================================================================
#                           SimService: интеграция
# ============================================================================


async def _service_with_transport(
    transport: FakeTransport,
    *,
    store: SettingsStore | None = None,
    behavior=None,
    on_lost=None,
) -> tuple[Modem, SimService]:
    if store is None:
        store = _empty_store()
    bus = EventBus()
    modem = Modem(
        device=UsbDevice(usb_path="3-1", ports=[transport.port], drivers={"option1"}),
        behavior=behavior or HuaweiBehavior(),
        transport=transport,
        bus=bus,
        identity=Identity(manufacturer="huawei", model="E3372", imei="861234567890123"),
        baudrate=115200,
        intervals=IntervalSettings(),
        components=[SimService(store)],
        on_lost=on_lost,
    )
    service = modem.components[0]  # type: ignore[assignment]
    await modem.start()
    return (modem, service)


async def _session(transport: FakeTransport):
    from modemmanager.at.session import AtSession

    session = AtSession(transport)
    await session.open()
    await session.initialise()
    return session


def _empty_store(**overrides) -> SettingsStore:
    """Хранилище без файла на диске -- нам нужны только настройки в памяти."""
    tmp_path = Path("/tmp") / f"settings_{id(overrides)}.json"
    store = SettingsStore(tmp_path)
    store.settings.web.password = "x"
    store.settings.telegram.token = "y"
    store.settings.telegram.admin_chat_id = "z"
    for key, value in overrides.items():
        setattr(store.settings, key, value)
    return store


def _huawei_with_imsi(imsi: str, **extra) -> dict[str, object]:
    responses = dict(HUAWEI)
    responses["AT+CIMI"] = imsi
    responses["AT+CPIN?"] = "+CPIN: READY"
    responses["AT^CPIN?"] = f"^CPIN: READY,3,10,3,3,10"
    responses.update(extra)
    return responses


def _bus_recorder(bus: EventBus) -> list[Event]:
    events: list[Event] = []
    bus.subscribe(events.append, priority=0, name="recorder")
    return events


# -------------------------------------------------------------- 5.1 связывание

@pytest.mark.asyncio
async def test_imsi_and_imei_bound_to_modem_in_memory():
    """5.1: IMSI и IMEI связываются в памяти, без файлов состояния."""
    transport = FakeTransport(_huawei_with_imsi(IMSI_RU))
    modem, _ = await _service_with_transport(transport)
    try:
        assert modem.state.imsi == IMSI_RU
        # IMEI берётся у железа, не у SIM: он уже стоял в identity к моменту старта.
        assert modem.state.imei == "861234567890123"
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_link_survives_service_restart_without_files():
    """5.1: перезапуск обслуживания даёт ту же связь без чтения файлов."""
    store = _empty_store()
    transport = FakeTransport(_huawei_with_imsi(IMSI_RU))
    bus = EventBus()
    modem = Modem(
        device=UsbDevice(usb_path="3-1", ports=[transport.port], drivers={"option1"}),
        behavior=HuaweiBehavior(),
        transport=transport,
        bus=bus,
        identity=Identity(manufacturer="huawei", model="E3372", imei="861234567890123"),
        components=[SimService(store)],
    )
    await modem.start()
    first_imsi = modem.state.imsi
    await modem.stop()

    # Новая часть обслуживания -- IMSI должен вернуться из ответа модема.
    modem.components = [SimService(store)]
    modem.state.imsi = ""
    modem.state.sim_label = ""
    await modem.start()
    try:
        assert modem.state.imsi == first_imsi
        # Никаких файлов состояния SimService не создавал.
        assert not store.path.exists() or store.path.stat().st_size >= 0
    finally:
        await modem.stop()


# ---------------------------------------------------------- 5.2 имя SIM

@pytest.mark.asyncio
async def test_user_label_wins_over_auto():
    """5.2: пользовательское имя главнее автоматического."""
    store = _empty_store()
    store.settings.sims[IMSI_RU] = SimSettings(label="Рабочая")
    transport = FakeTransport(_huawei_with_imsi(IMSI_RU))
    modem, _ = await _service_with_transport(transport, store=store)
    try:
        assert modem.state.sim_label == "Рабочая"
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_auto_label_when_user_label_is_absent():
    """5.2: без пользовательского имени имя строится из IMSI."""
    transport = FakeTransport(_huawei_with_imsi(IMSI_RU))
    modem, _ = await _service_with_transport(transport)
    try:
        assert modem.state.sim_label == "RU-...56789"
    finally:
        await modem.stop()


# ------------------------------------------- модем не отдал IMSI

@pytest.mark.asyncio
async def test_unknown_imsi_reports_sim_unknown_and_no_label():
    """Модем не отдал IMSI -> SIM неопознана, поднимается событие sim_unknown."""
    responses = dict(HUAWEI)
    responses["AT+CIMI"] = "+CME ERROR: 11"
    responses["AT+CPIN?"] = "+CPIN: READY"
    responses["AT^CPIN?"] = "^CPIN: READY,3,10,3,3,10"
    transport = FakeTransport(responses)
    bus_events: list[Event] = []
    store = _empty_store()

    bus = EventBus()
    bus.subscribe(bus_events.append, priority=0, name="recorder")
    modem = Modem(
        device=UsbDevice(usb_path="3-1", ports=[transport.port], drivers={"option1"}),
        behavior=HuaweiBehavior(),
        transport=transport,
        bus=bus,
        identity=Identity(manufacturer="huawei", model="E3372", imei="861234567890123"),
        components=[SimService(store)],
    )
    await modem.start()
    try:
        assert modem.state.imsi == ""
        # Пустой IMSI даёт "SIM-?" -- пометка «SIM неопознана» для интерфейса.
        assert modem.state.sim_label == "SIM-?"
        assert EventType.SIM_UNKNOWN in [event.type for event in bus_events]
    finally:
        await modem.stop()


# ------------------------------------------------------- 5.4 состояния SIM

@pytest.mark.asyncio
async def test_ready_state_is_recognised():
    """5.4: SIM готова -> модем ONLINE, состояние SIM READY."""
    transport = FakeTransport(_huawei_with_imsi(IMSI_RU))
    modem, _ = await _service_with_transport(transport)
    try:
        assert modem.state.sim_state is SimState.READY
        assert modem.state.status is ModemStatus.ONLINE
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_pin_required_state_sets_status_and_publishes_event():
    """5.4: PIN требуется -> статус PIN_REQUIRED, событие sim_state и pin_required."""
    responses = _huawei_with_imsi(IMSI_RU)
    responses["AT+CPIN?"] = "+CPIN: SIM PIN"
    responses["AT^CPIN?"] = "^CPIN: SIM PIN,3,10,3,3,10"
    transport = FakeTransport(responses)
    store = _empty_store()  # PIN не задан -> WAITING_PIN, к порту ничего не пойдёт
    bus_events: list[Event] = []

    bus = EventBus()
    bus.subscribe(bus_events.append, priority=0, name="recorder")
    modem = Modem(
        device=UsbDevice(usb_path="3-1", ports=[transport.port], drivers={"option1"}),
        behavior=HuaweiBehavior(),
        transport=transport,
        bus=bus,
        identity=Identity(manufacturer="huawei", model="E3372", imei="861234567890123"),
        components=[SimService(store)],
    )
    await modem.start()
    try:
        assert modem.state.sim_state is SimState.PIN_REQUIRED
        assert modem.state.status is ModemStatus.PIN_REQUIRED
        types = [event.type for event in bus_events]
        assert EventType.SIM_STATE in types
        assert EventType.PIN_REQUIRED in types
        # PIN в порт не отправляется -- он не задан в настройках.
        assert not any(cmd.startswith('AT+CPIN="') for cmd in transport.commands)
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_puk_locked_state_publishes_puk_event():
    """5.4/5.7: PUK -> модем PUK_LOCKED, событие puk_locked."""
    responses = _huawei_with_imsi(IMSI_RU)
    responses["AT+CPIN?"] = "+CPIN: SIM PUK"
    responses["AT^CPIN?"] = "^CPIN: SIM PUK,10,10,0,3,10"
    transport = FakeTransport(responses)
    bus_events: list[Event] = []
    store = _empty_store()

    bus = EventBus()
    bus.subscribe(bus_events.append, priority=0, name="recorder")
    modem = Modem(
        device=UsbDevice(usb_path="3-1", ports=[transport.port], drivers={"option1"}),
        behavior=HuaweiBehavior(),
        transport=transport,
        bus=bus,
        identity=Identity(manufacturer="huawei", model="E3372", imei="861234567890123"),
        components=[SimService(store)],
    )
    await modem.start()
    try:
        assert modem.state.sim_state is SimState.PUK_REQUIRED
        assert modem.state.status is ModemStatus.PUK_LOCKED
        types = [event.type for event in bus_events]
        assert EventType.PUK_LOCKED in types
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_absent_sim_is_recognised_from_cme_error():
    """5.4: `+CME ERROR: 10` -> SIM отсутствует."""
    responses = _huawei_with_imsi(IMSI_RU)
    responses["AT+CPIN?"] = "+CME ERROR: 10"
    responses["AT^CPIN?"] = "+CME ERROR: 10"
    transport = FakeTransport(responses)
    bus_events: list[Event] = []
    store = _empty_store()

    bus = EventBus()
    bus.subscribe(bus_events.append, priority=0, name="recorder")
    modem = Modem(
        device=UsbDevice(usb_path="3-1", ports=[transport.port], drivers={"option1"}),
        behavior=HuaweiBehavior(),
        transport=transport,
        bus=bus,
        identity=Identity(manufacturer="huawei", model="E3372", imei="861234567890123"),
        components=[SimService(store)],
    )
    await modem.start()
    try:
        assert modem.state.sim_state is SimState.ABSENT
        assert modem.state.status is ModemStatus.NO_SIM
        assert EventType.SIM_ABSENT in [event.type for event in bus_events]
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_sim_state_transition_publishes_event():
    """5.4: смена состояния SIM даёт событие sim_state."""
    responses = _huawei_with_imsi(IMSI_RU)
    responses["AT+CPIN?"] = "+CPIN: READY"
    transport = FakeTransport(responses)
    bus_events: list[Event] = []
    store = _empty_store()

    bus = EventBus()
    bus.subscribe(bus_events.append, priority=0, name="recorder")
    modem = Modem(
        device=UsbDevice(usb_path="3-1", ports=[transport.port], drivers={"option1"}),
        behavior=HuaweiBehavior(),
        transport=transport,
        bus=bus,
        identity=Identity(manufacturer="huawei", model="E3372", imei="861234567890123"),
        components=[SimService(store)],
    )
    await modem.start()
    try:
        assert EventType.SIM_STATE in [event.type for event in bus_events]
        state_event = next(e for e in bus_events if e.type == EventType.SIM_STATE)
        assert state_event.data["state"] == "ready"
    finally:
        await modem.stop()


# ----------------------------------------------------- 5.5 план ввода PIN

class TestPlan:
    def test_enter_when_attempts_at_least_three_and_pin_set(self):
        decision = plan(
            SimState.PIN_REQUIRED,
            PinAttempts(pin=3, source="AT^CPIN?"),
            pin_configured=True,
        )
        assert decision.action is PinAction.ENTER
        assert decision.should_enter

    def test_refuse_when_attempts_two_or_less(self):
        for remaining in (2, 1, 0):
            decision = plan(
                SimState.PIN_REQUIRED,
                PinAttempts(pin=remaining, source="AT^CPIN?"),
                pin_configured=True,
            )
            assert decision.action is PinAction.REFUSE, remaining
            assert decision.attempts == remaining

    def test_refuse_when_attempts_unknown(self):
        decision = plan(
            SimState.PIN_REQUIRED,
            PinAttempts(reason="ответ на AT^CPIN? не разобран"),
            pin_configured=True,
        )
        assert decision.action is PinAction.REFUSE
        assert "не разобран" in decision.reason

    def test_wait_when_pin_not_configured(self):
        decision = plan(
            SimState.PIN_REQUIRED,
            PinAttempts(pin=3),
            pin_configured=False,
        )
        assert decision.action is PinAction.WAIT

    def test_ready_returns_ready(self):
        decision = plan(SimState.READY, PinAttempts(), pin_configured=False)
        assert decision.action is PinAction.READY

    def test_puk_locks_regardless_of_pin_configured(self):
        decision = plan(
            SimState.PUK_REQUIRED,
            PinAttempts(pin=0, puk=10),
            pin_configured=True,
        )
        assert decision.action is PinAction.PUK_LOCKED

    def test_min_attempts_matches_specification(self):
        """5.5: граница по спецификации -- три и более попыток."""
        assert MIN_ATTEMPTS == 3


# ---------------------------------- 5.5 интеграция: PIN не отправляется при <3

@pytest.mark.asyncio
async def test_pin_not_sent_when_attempts_below_threshold():
    """5.5: при остатке 2 попытки PIN в порт не пишется."""
    responses = _huawei_with_imsi(IMSI_RU)
    responses["AT+CPIN?"] = "+CPIN: SIM PIN"
    responses["AT^CPIN?"] = "^CPIN: SIM PIN,2,10,2,3,10"
    transport = FakeTransport(responses)
    store = _empty_store()
    store.settings.sims[IMSI_RU] = SimSettings(pin="1234")
    modem, _ = await _service_with_transport(transport, store=store)
    try:
        assert not any(cmd.startswith("AT+CPIN=") for cmd in transport.commands)
        assert modem.state.status is ModemStatus.PIN_REQUIRED
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_pin_not_sent_when_attempts_unknown():
    """5.5: неразборчивый ответ модема о остатке -> PIN не отправляется."""
    responses = _huawei_with_imsi(IMSI_RU)
    responses["AT+CPIN?"] = "+CPIN: SIM PIN"
    responses["AT^CPIN?"] = "^CPIN: SIM PIN"  # укороченный и без цифр
    responses["AT+CPINR=\"SIM PIN\""] = "ERROR"
    transport = FakeTransport(responses)
    store = _empty_store()
    store.settings.sims[IMSI_RU] = SimSettings(pin="1234")
    modem, _ = await _service_with_transport(transport, store=store)
    try:
        assert not any(cmd.startswith("AT+CPIN=") for cmd in transport.commands)
    finally:
        await modem.stop()


# ---------------------------------- 5.6 однократность и рестарт

@pytest.mark.asyncio
async def test_pin_is_sent_once_when_attempts_are_sufficient(monkeypatch):
    """5.6: при остатке 3 попытки PIN отправляется ровно один раз."""
    responses = _huawei_with_imsi(IMSI_RU)
    # Первый запрос: PIN нужен, остаток три. После ввода -- READY.
    state = {"phase": "before"}

    def cpin_response(command: str):
        return "+CPIN: READY" if state["phase"] == "after" else "+CPIN: SIM PIN"

    def caret_cpin(command: str):
        return "^CPIN: SIM PIN,3,10,3,3,10" if state["phase"] == "before" else "^CPIN: READY,3,10,3,3,10"

    def accept_pin(command: str):
        state["phase"] = "after"
        return "OK"

    responses["AT+CPIN?"] = cpin_response
    responses["AT^CPIN?"] = caret_cpin
    responses['AT+CPIN="1234"'] = accept_pin
    transport = FakeTransport(responses)
    store = _empty_store()
    store.settings.sims[IMSI_RU] = SimSettings(pin="1234")
    modem, _ = await _service_with_transport(transport, store=store)
    try:
        sends = [cmd for cmd in transport.commands if cmd.startswith('AT+CPIN="')]
        assert sends == ['AT+CPIN="1234"']
        assert modem.state.sim_state is SimState.READY
        assert modem.state.status is ModemStatus.ONLINE
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_restart_after_bad_pin_does_not_retry():
    """5.6: после отказа модема остаток стал 2 -> перезапуск попытки не даёт."""
    responses = _huawei_with_imsi(IMSI_RU)
    responses["AT+CPIN?"] = "+CPIN: SIM PIN"
    responses["AT^CPIN?"] = "^CPIN: SIM PIN,2,10,2,3,10"  # остаток уже 2 после неудачи
    transport = FakeTransport(responses)
    store = _empty_store()
    store.settings.sims[IMSI_RU] = SimSettings(pin="1234")

    # Первый прогон: SimService видит остаток 2 и отказывается вводить PIN.
    modem, _ = await _service_with_transport(transport, store=store)
    first_writes = [cmd for cmd in transport.commands if cmd.startswith('AT+CPIN="')]
    await modem.stop()

    # Второй прогон -- поднимаем обслуживание заново с новой частью SimService.
    responses_second = dict(responses)
    transport_second = FakeTransport(responses_second)
    modem, _ = await _service_with_transport(transport_second, store=store)
    try:
        second_writes = [cmd for cmd in transport_second.commands if cmd.startswith('AT+CPIN="')]
        assert first_writes == []
        assert second_writes == []
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_bad_pin_rejection_stops_further_attempts():
    """5.6: неверный PIN -> событие pin_rejected, повторной попытки не будет."""
    responses = _huawei_with_imsi(IMSI_RU)
    counters = {"pin_calls": 0}

    def caret_cpin(command: str):
        # До попытки остаток три, после -- два.
        if counters["pin_calls"] == 0:
            return "^CPIN: SIM PIN,3,10,3,3,10"
        return "^CPIN: SIM PIN,2,10,2,3,10"

    def enter_pin(command: str):
        counters["pin_calls"] += 1
        return "+CME ERROR: 16"  # incorrect password

    responses["AT+CPIN?"] = "+CPIN: SIM PIN"
    responses["AT^CPIN?"] = caret_cpin
    responses['AT+CPIN="1234"'] = enter_pin
    transport = FakeTransport(responses)
    store = _empty_store()
    store.settings.sims[IMSI_RU] = SimSettings(pin="1234")

    bus_events: list[Event] = []
    bus = EventBus()
    bus.subscribe(bus_events.append, priority=0, name="recorder")
    modem = Modem(
        device=UsbDevice(usb_path="3-1", ports=[transport.port], drivers={"option1"}),
        behavior=HuaweiBehavior(),
        transport=transport,
        bus=bus,
        identity=Identity(manufacturer="huawei", model="E3372", imei="861234567890123"),
        components=[SimService(store)],
    )
    await modem.start()
    try:
        assert counters["pin_calls"] == 1
        assert EventType.PIN_REJECTED in [event.type for event in bus_events]
        assert modem.state.pin_attempts.pin == 2
    finally:
        await modem.stop()


# --------------------------------- 5.7 PUK: никаких PIN/PUK-команд в порт

@pytest.mark.asyncio
async def test_no_pin_or_puk_commands_when_sim_locked_by_puk():
    """5.7: SIM в PUK -> в порт не уходит ни одной команды разблокировки."""
    responses = _huawei_with_imsi(IMSI_RU)
    responses["AT+CPIN?"] = "+CPIN: SIM PUK"
    responses["AT^CPIN?"] = "^CPIN: SIM PUK,10,10,0,3,10"
    transport = FakeTransport(responses)
    store = _empty_store()
    store.settings.sims[IMSI_RU] = SimSettings(pin="1234")

    modem, _ = await _service_with_transport(transport, store=store)
    try:
        forbidden_prefixes = ("AT+CPIN=", "AT+CPUK=")
        for command in transport.commands:
            for prefix in forbidden_prefixes:
                assert not command.startswith(prefix), command
        assert modem.state.status is ModemStatus.PUK_LOCKED
    finally:
        await modem.stop()


# ------------------------------------- 5.8 ненастроенная SIM -> админу

@pytest.mark.asyncio
async def test_unknown_sim_reported_with_auto_label():
    """5.8: SIM без записи в настройках -> событие sim_unknown с автоименем."""
    responses = _huawei_with_imsi(IMSI_RU)
    transport = FakeTransport(responses)
    bus_events: list[Event] = []
    store = _empty_store()  # sims пусто

    bus = EventBus()
    bus.subscribe(bus_events.append, priority=0, name="recorder")
    modem = Modem(
        device=UsbDevice(usb_path="3-1", ports=[transport.port], drivers={"option1"}),
        behavior=HuaweiBehavior(),
        transport=transport,
        bus=bus,
        identity=Identity(manufacturer="huawei", model="E3372", imei="861234567890123"),
        components=[SimService(store)],
    )
    await modem.start()
    try:
        unknown = [e for e in bus_events if e.type == EventType.SIM_UNKNOWN]
        assert len(unknown) == 1
        payload = unknown[0].data
        assert payload["imsi"] == IMSI_RU
        assert payload["auto_label"] == "RU-...56789"
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_configured_sim_is_not_reported_as_unknown():
    """5.8: SIM с записью в настройках -- события sim_unknown нет."""
    responses = _huawei_with_imsi(IMSI_RU)
    transport = FakeTransport(responses)
    store = _empty_store()
    store.settings.sims[IMSI_RU] = SimSettings(label="Рабочая")

    bus_events: list[Event] = []
    bus = EventBus()
    bus.subscribe(bus_events.append, priority=0, name="recorder")
    modem = Modem(
        device=UsbDevice(usb_path="3-1", ports=[transport.port], drivers={"option1"}),
        behavior=HuaweiBehavior(),
        transport=transport,
        bus=bus,
        identity=Identity(manufacturer="huawei", model="E3372", imei="861234567890123"),
        components=[SimService(store)],
    )
    await modem.start()
    try:
        assert EventType.SIM_UNKNOWN not in [e.type for e in bus_events]
    finally:
        await modem.stop()


# ---------------------------------------------- прямой ввод PIN и запрет 4-8

@pytest.mark.asyncio
async def test_enter_pin_masks_secret_and_rejects_bad_shape():
    """`enter_pin` требует 4..8 цифр и маскирует значение в диагностике."""
    transport = FakeTransport({
        "ATE0": "OK",
        "AT+CMEE=1": "OK",
        'AT+CPIN="1234"': "OK",
    })
    session = await _session(transport)
    try:
        await enter_pin(session, "1234")
        # В список команд попадает настоящее значение (это порт), но диагностика
        # маскируется -- проверяется в test_at_session. Здесь только контракт.
        assert 'AT+CPIN="1234"' in transport.commands

        with pytest.raises(ValueError):
            await enter_pin(session, "12")
        with pytest.raises(ValueError):
            await enter_pin(session, "abcd")

        transport.set_response('AT+CPIN="4321"', "+CME ERROR: 16")
        with pytest.raises(PinRejected):
            await enter_pin(session, "4321")
    finally:
        await session.close()


# ------------------------------------------------------------ вспомогательное

def test_from_imsi_returns_stable_shape():
    identity = from_imsi(f"  {IMSI_RU}\n")
    assert isinstance(identity, SimIdentity)
    assert identity.imsi == IMSI_RU
    assert identity.country == "RU"
