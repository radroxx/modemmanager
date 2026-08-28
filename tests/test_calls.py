"""Обработка входящих вызовов."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fake_answers import HUAWEI
from fake_modem import FakeTransport

from modemmanager.at.errors import CommandError
from modemmanager.behaviors import HuaweiBehavior, Sim800Behavior
from modemmanager.calls import CallService
from modemmanager.config import CallSettings, SettingsStore
from modemmanager.discovery.sysfs import UsbDevice
from modemmanager.events import Event, EventBus, EventType
from modemmanager.modem import Modem
from modemmanager.values import Identity


IMSI = "89701020123456789042"


def _store(clip_wait: float = 0.05, ring_dedup: float = 5.0) -> SettingsStore:
    store = SettingsStore(Path("/tmp/settings_calls.json"))
    store.settings.web.password = "x"
    store.settings.telegram.token = "y"
    store.settings.telegram.admin_chat_id = "z"
    store.settings.calls = CallSettings(clip_wait=clip_wait, ring_dedup=ring_dedup)
    return store


async def _run_modem(
    transport: FakeTransport,
    store: SettingsStore,
    *,
    events: list[Event] | None = None,
    label: str = "Работа",
) -> tuple[Modem, CallService]:
    bus = EventBus()
    if events is not None:
        bus.subscribe(events.append, priority=0, name="recorder")
    service = CallService(store)
    modem = Modem(
        device=UsbDevice(usb_path="3-1", ports=[transport.port], drivers={"option1"}),
        behavior=HuaweiBehavior(),
        transport=transport,
        bus=bus,
        identity=Identity(manufacturer="huawei", model="E3372", imei="861234567890123"),
        components=[service],
    )
    modem.state.imsi = IMSI
    modem.state.sim_label = label
    await modem.start()
    return (modem, service)


async def _spin(rounds: int = 30) -> None:
    """Прокручивает event loop, чтобы диспетчер обработал очередь."""
    for _ in range(rounds):
        await asyncio.sleep(0)


# ---------------------------------- 7.1 включение CLIP в инициализации

@pytest.mark.asyncio
async def test_huawei_init_sends_at_clip():
    responses = dict(HUAWEI)
    transport = FakeTransport(responses)
    store = _store()
    modem, _ = await _run_modem(transport, store)
    try:
        assert "AT+CLIP=1" in transport.commands
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_sim800_init_sends_at_clip():
    """Инициализация SIM800 через generic-цепочку включает AT+CLIP=1."""
    responses = dict(HUAWEI)  # используем ту же таблицу ответов, поведение другое
    transport = FakeTransport(responses)
    bus = EventBus()
    modem = Modem(
        device=UsbDevice(usb_path="3-1", ports=[transport.port], drivers={"ch341-uart"}),
        behavior=Sim800Behavior(),
        transport=transport,
        bus=bus,
        identity=Identity(manufacturer="SIMCOM", model="SIM800L", imei="861234567890123"),
        components=[],
    )
    await modem.start()
    try:
        assert "AT+CLIP=1" in transport.commands
    finally:
        await modem.stop()


# ------------------------------------------- 7.2 окно ожидания сведений о номере

@pytest.mark.asyncio
async def test_call_with_caller_id_forms_event_with_number():
    """7.2: получен номер -> событие с номером."""
    responses = dict(HUAWEI)
    responses["AT+CHUP"] = "OK"
    transport = FakeTransport(responses)
    events: list[Event] = []
    store = _store(clip_wait=0.5)
    modem, _ = await _run_modem(transport, store, events=events)
    try:
        transport.queue_unsolicited("RING", '+CLIP: "+79990001122",145')
        await _spin(20)
        calls = [event for event in events if event.type == EventType.CALL]
        assert len(calls) == 1
        assert calls[0].data["number"] == "+79990001122"
        assert calls[0].data["hidden"] is False
        assert calls[0].data["decision"] == "number_received"
        assert calls[0].data["outcome"] == "rejected"
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_call_without_caller_id_forms_event_after_timeout():
    """7.2: номер не поступил за отведённое время -> hidden=True, decision=timeout."""
    responses = dict(HUAWEI)
    responses["AT+CHUP"] = "OK"
    transport = FakeTransport(responses)
    events: list[Event] = []
    store = _store(clip_wait=0.05)
    modem, _ = await _run_modem(transport, store, events=events)
    try:
        transport.queue_unsolicited("RING")
        await _spin(2)
        await asyncio.sleep(0.1)  # даём таймеру истечь
        await _spin(20)
        calls = [event for event in events if event.type == EventType.CALL]
        assert len(calls) == 1
        assert calls[0].data["hidden"] is True
        assert calls[0].data["decision"] == "timeout"
        assert calls[0].data["known_number"] is False
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_call_with_hidden_number_is_marked_hidden():
    """7.2: CLIP пришёл, но номер пустой -> hidden=True, decision=number_received."""
    responses = dict(HUAWEI)
    responses["AT+CHUP"] = "OK"
    transport = FakeTransport(responses)
    events: list[Event] = []
    store = _store(clip_wait=0.5)
    modem, _ = await _run_modem(transport, store, events=events)
    try:
        transport.queue_unsolicited("RING", '+CLIP: "",128')
        await _spin(20)
        calls = [event for event in events if event.type == EventType.CALL]
        assert len(calls) == 1
        assert calls[0].data["hidden"] is True
        assert calls[0].data["known_number"] is False
        assert calls[0].data["decision"] == "number_received"
    finally:
        await modem.stop()


# ----------------------------------------------- 7.3 дедуп и следующий вызов

@pytest.mark.asyncio
async def test_multiple_rings_produce_single_event():
    """7.3: серия RING одного вызова даёт ровно одно событие."""
    responses = dict(HUAWEI)
    responses["AT+CHUP"] = "OK"
    transport = FakeTransport(responses)
    events: list[Event] = []
    store = _store(clip_wait=0.5, ring_dedup=5.0)
    modem, _ = await _run_modem(transport, store, events=events)
    try:
        transport.queue_unsolicited("RING", "RING", '+CLIP: "+79990001122",145', "RING", "RING")
        await _spin(30)
        calls = [event for event in events if event.type == EventType.CALL]
        assert len(calls) == 1
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_new_call_after_dedup_window_forms_new_event():
    """7.3: RING после закрытия предыдущего вызова -- новое событие."""
    responses = dict(HUAWEI)
    responses["AT+CHUP"] = "OK"
    transport = FakeTransport(responses)
    events: list[Event] = []
    store = _store(clip_wait=0.05, ring_dedup=0.05)  # короткие интервалы для теста
    modem, service = await _run_modem(transport, store, events=events)
    try:
        transport.queue_unsolicited("RING")
        await _spin(2)
        await asyncio.sleep(0.1)  # закончилось окно ожидания CLIP
        await _spin(20)
        await asyncio.sleep(0.1)  # закончился ring_dedup
        # Новый вызов:
        transport.queue_unsolicited("RING", '+CLIP: "+79990009999",145')
        await _spin(30)
        calls = [event for event in events if event.type == EventType.CALL]
        assert len(calls) == 2
        assert calls[1].data["number"] == "+79990009999"
    finally:
        await modem.stop()


# ---------------------------------------- 7.4 отклонение и три исхода

@pytest.mark.asyncio
async def test_normal_reject_produces_hangup_command():
    """7.4: обычный сценарий -- в порт уходит AT+CHUP, outcome=rejected."""
    responses = dict(HUAWEI)
    responses["AT+CHUP"] = "OK"
    transport = FakeTransport(responses)
    events: list[Event] = []
    store = _store(clip_wait=0.5)
    modem, _ = await _run_modem(transport, store, events=events)
    try:
        transport.queue_unsolicited("RING", '+CLIP: "+79990001122",145')
        await _spin(30)
        assert "AT+CHUP" in transport.commands
        calls = [event for event in events if event.type == EventType.CALL]
        assert calls[0].data["outcome"] == "rejected"
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_caller_hangs_up_first_is_not_rejected():
    """7.4: NO CARRIER до отклонения -> outcome=ended_by_caller, AT+CHUP не идёт."""
    responses = dict(HUAWEI)
    responses["AT+CHUP"] = "OK"
    transport = FakeTransport(responses)
    events: list[Event] = []
    store = _store(clip_wait=1.0)  # длинное окно, чтобы отбой опередил
    modem, _ = await _run_modem(transport, store, events=events)
    try:
        transport.queue_unsolicited("RING", "NO CARRIER")
        await _spin(20)
        calls = [event for event in events if event.type == EventType.CALL]
        assert len(calls) == 1
        assert calls[0].data["outcome"] == "ended_by_caller"
        # Отклонение не должно было пытаться -- в командах нет AT+CHUP.
        assert "AT+CHUP" not in transport.commands
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_hangup_failure_still_forms_event_and_keeps_modem_alive():
    """7.4: неудача отклонения фиксируется в событии, модем продолжает работу."""
    responses = dict(HUAWEI)
    responses["AT+CHUP"] = "ERROR"
    transport = FakeTransport(responses)
    events: list[Event] = []
    store = _store(clip_wait=0.5)
    modem, _ = await _run_modem(transport, store, events=events)
    try:
        transport.queue_unsolicited("RING", '+CLIP: "+79990001122",145')
        await _spin(30)
        calls = [event for event in events if event.type == EventType.CALL]
        assert len(calls) == 1
        assert calls[0].data["outcome"] == "reject_failed"
        assert "AT+CHUP" in transport.commands
        # Модем не в состоянии FAULT: неудача отклонения не является поводом.
        assert modem.state.status.value == "online"
    finally:
        await modem.stop()


# ------------------------- 7.5 событие несёт идентификатор SIM и модема

@pytest.mark.asyncio
async def test_call_event_carries_sim_and_modem_identity():
    """7.5: событие вызова содержит IMSI, имя SIM и IMEI."""
    responses = dict(HUAWEI)
    responses["AT+CHUP"] = "OK"
    transport = FakeTransport(responses)
    events: list[Event] = []
    store = _store(clip_wait=0.5)
    modem, _ = await _run_modem(transport, store, events=events, label="Роуминг")
    try:
        transport.queue_unsolicited("RING", '+CLIP: "+79990001122",145')
        await _spin(20)
        calls = [event for event in events if event.type == EventType.CALL]
        assert len(calls) == 1
        event = calls[0]
        assert event.imsi == IMSI
        assert event.sim_label == "Роуминг"
        assert event.imei == "861234567890123"
    finally:
        await modem.stop()
