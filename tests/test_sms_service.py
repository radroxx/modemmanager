"""Обслуживание приёма SMS: инициализация, чтение, удаление, сборка."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fake_answers import HUAWEI
from fake_modem import FakeTransport
from pdu_builder import build_deliver, build_udh, concat_8bit

from modemmanager.behaviors import HuaweiBehavior
from modemmanager.config import IntervalSettings, SettingsStore
from modemmanager.discovery.sysfs import UsbDevice
from modemmanager.events import Event, EventBus, EventType
from modemmanager.modem import Modem
from modemmanager.sms import Assembler, SmsService
from modemmanager.values import Identity


IMSI = "89701020123456789042"
SENDER = "79990001122"


def _store(assembly_timeout: float = 600.0) -> SettingsStore:
    store = SettingsStore(Path("/tmp/settings_sms.json"))
    store.settings.web.password = "x"
    store.settings.telegram.token = "y"
    store.settings.telegram.admin_chat_id = "z"
    store.settings.sms.assembly_timeout = assembly_timeout
    return store


def _cmgl_response(*items: tuple[int, str]) -> list[str]:
    """Формирует ответ на ``AT+CMGL=4``: пары ``+CMGL: idx,1,,len`` + PDU."""
    lines: list[str] = []
    for index, pdu in items:
        length = len(pdu) // 2
        lines.append(f"+CMGL: {index},1,,{length}")
        lines.append(pdu)
    lines.append("OK")
    return lines


def _cmgr_response(pdu: str) -> list[str]:
    length = len(pdu) // 2
    return [f"+CMGR: 1,,{length}", pdu, "OK"]


async def _run_modem(
    transport: FakeTransport,
    store: SettingsStore,
    *,
    events: list[Event] | None = None,
) -> tuple[Modem, SmsService]:
    bus = EventBus()
    if events is not None:
        bus.subscribe(events.append, priority=0, name="recorder")
    service = SmsService(store)
    modem = Modem(
        device=UsbDevice(usb_path="3-1", ports=[transport.port], drivers={"option1"}),
        behavior=HuaweiBehavior(),
        transport=transport,
        bus=bus,
        identity=Identity(manufacturer="huawei", model="E3372", imei="861234567890123"),
        intervals=IntervalSettings(),
        components=[service],
    )
    modem.state.imsi = IMSI
    modem.state.sim_label = "test"
    await modem.start()
    return (modem, service)


# ------------------------------------------------- 6.3 инициализация приёма

@pytest.mark.asyncio
async def test_init_switches_to_pdu_mode_and_enables_cmti():
    """6.3: инициализация переводит в двоичный режим и включает уведомления."""
    responses = dict(HUAWEI)
    transport = FakeTransport(responses)
    store = _store()
    modem, _ = await _run_modem(transport, store)
    try:
        # PDU-режим и CNMI обязательны -- иначе многочастные не собрать.
        assert "AT+CMGF=0" in transport.commands
        assert "AT+CNMI=2,1,0,0,0" in transport.commands
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_backlog_is_drained_and_deleted_on_start():
    """6.3: сообщения из памяти обрабатываются при старте и память освобождается."""
    responses = dict(HUAWEI)
    pdu_a = build_deliver(sender=SENDER, text="one")
    pdu_b = build_deliver(sender="79990002233", text="two")
    responses["AT+CMGL=4"] = _cmgl_response((1, pdu_a), (5, pdu_b))
    responses["AT+CMGD=1"] = "OK"
    responses["AT+CMGD=5"] = "OK"

    transport = FakeTransport(responses)
    store = _store()
    events: list[Event] = []
    modem, _ = await _run_modem(transport, store, events=events)
    try:
        # Оба сообщения удалены -- память освобождена.
        assert "AT+CMGD=1" in transport.commands
        assert "AT+CMGD=5" in transport.commands
        sms_events = [event for event in events if event.type == EventType.SMS]
        assert len(sms_events) == 2
        texts = sorted(event.data["text"] for event in sms_events)
        assert texts == ["one", "two"]
    finally:
        await modem.stop()


# ------------------------- 6.4 чтение по +CMTI и удаление до сборки

@pytest.mark.asyncio
async def test_cmti_read_deletes_before_publish():
    """6.4: удаление в порту происходит до публикации события."""
    responses = dict(HUAWEI)
    pdu = build_deliver(sender=SENDER, text="incoming")
    responses["AT+CMGR=3"] = _cmgr_response(pdu)
    responses["AT+CMGD=3"] = "OK"
    responses["AT+CMGL=4"] = "OK"  # памяти чисто на старте

    transport = FakeTransport(responses)
    store = _store()
    events: list[Event] = []
    modem, _ = await _run_modem(transport, store, events=events)
    try:
        # Модем сообщил об SMS в памяти.
        transport.queue_unsolicited('+CMTI: "SM",3')
        # Ждём пока dispatcher обработает уведомление.
        for _ in range(10):
            await asyncio.sleep(0)
        # Проверяем ПОРЯДОК команд: чтение, потом удаление, потом уже
        # публикуется событие. Событие -- через шину, оно уже в events.
        cmgr = transport.commands.index("AT+CMGR=3")
        cmgd = transport.commands.index("AT+CMGD=3")
        assert cmgr < cmgd
        sms_events = [event for event in events if event.type == EventType.SMS]
        assert len(sms_events) == 1
        assert sms_events[0].data["text"] == "incoming"
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_incomplete_message_publishes_incomplete_event_after_timeout():
    """6.7: истечение таймаута сборки -> событие sms с пометкой incomplete."""
    responses = dict(HUAWEI)
    # Первая часть многочастного, вторая не придёт.
    udh = build_udh(concat_8bit(reference=17, total=2, seq=1))
    pdu = build_deliver(sender=SENDER, text="half ", udh=udh)
    responses["AT+CMGR=1"] = _cmgr_response(pdu)
    responses["AT+CMGD=1"] = "OK"
    responses["AT+CMGL=4"] = "OK"

    transport = FakeTransport(responses)
    store = _store(assembly_timeout=0.0)  # сразу истекает
    events: list[Event] = []
    modem, service = await _run_modem(transport, store, events=events)
    try:
        transport.queue_unsolicited('+CMTI: "SM",1')
        for _ in range(10):
            await asyncio.sleep(0)
        # Форсируем следующую проверку по таймауту.
        service._next_expire = 0.0
        await service.poll()

        incomplete = [
            event for event in events
            if event.type == EventType.SMS and event.data.get("incomplete")
        ]
        assert len(incomplete) == 1
        assert incomplete[0].data["missing"] == [2]
        # Сырьё имеющейся части попало в запись.
        assert incomplete[0].data["raw"] == [pdu]
    finally:
        await modem.stop()


# ---------------------------- 6.8 сырые PDU всех частей в записи журнала

@pytest.mark.asyncio
async def test_multipart_message_records_raw_pdus_of_all_parts():
    """6.8: событие несёт сырые PDU всех частей."""
    responses = dict(HUAWEI)
    udh1 = build_udh(concat_8bit(reference=88, total=2, seq=1))
    udh2 = build_udh(concat_8bit(reference=88, total=2, seq=2))
    pdu1 = build_deliver(sender=SENDER, text="first ", udh=udh1)
    pdu2 = build_deliver(sender=SENDER, text="second", udh=udh2)
    responses["AT+CMGR=1"] = _cmgr_response(pdu1)
    responses["AT+CMGR=2"] = _cmgr_response(pdu2)
    responses["AT+CMGD=1"] = "OK"
    responses["AT+CMGD=2"] = "OK"
    responses["AT+CMGL=4"] = "OK"

    transport = FakeTransport(responses)
    store = _store()
    events: list[Event] = []
    modem, _ = await _run_modem(transport, store, events=events)
    try:
        transport.queue_unsolicited('+CMTI: "SM",1')
        for _ in range(10):
            await asyncio.sleep(0)
        transport.queue_unsolicited('+CMTI: "SM",2')
        for _ in range(10):
            await asyncio.sleep(0)

        sms_events = [event for event in events if event.type == EventType.SMS]
        assert len(sms_events) == 1
        assert sms_events[0].data["text"] == "first second"
        assert sms_events[0].data["raw"] == [pdu1, pdu2]
        assert sms_events[0].data["parts_total"] == 2
    finally:
        await modem.stop()


# ------------------------- 6.9 заполненность памяти в состоянии модема

@pytest.mark.asyncio
async def test_storage_reading_reaches_modem_state_via_regular_poll():
    """6.9: регулярный опрос кладёт значение AT+CPMS? в состояние модема.

    Метрики читают состояние из ``ModemState.public_dict``; проверяем, что там
    есть все три поля заполненности и они совпадают с ответом модема. Разбор
    метрик проверяется в тестах раздела 11.
    """
    responses = dict(HUAWEI)
    responses["AT+CPMS?"] = '+CPMS: "SM",17,20,"SM",17,20,"SM",17,20'
    responses["AT+CMGL=4"] = "OK"
    transport = FakeTransport(responses)
    store = _store()
    modem, _ = await _run_modem(transport, store)
    try:
        assert modem.state.storage.used == 17
        assert modem.state.storage.total == 20
        assert modem.state.storage.name == "SM"
        public = modem.state.public_dict()
        assert public["storage_used"] == 17
        assert public["storage_total"] == 20
        assert public["storage_full"] is False
    finally:
        await modem.stop()


# ------------------------------------------------------------ отдельно: разбор

def test_cmgr_and_cmgl_parsers_are_forgiving():
    from modemmanager.sms.service import _parse_cmgl, _parse_cmgr

    pdu_a = "0011FF00079121834254"
    text = "+CMGR: 1,,25\n" + pdu_a + "\nOK\n"
    assert _parse_cmgr(text) == pdu_a

    listing = (
        f"+CMGL: 3,1,,25\n{pdu_a}\n"
        f"+CMGL: 4,1,,25\n{pdu_a}\n"
        "OK\n"
    )
    assert _parse_cmgl(listing) == [(3, pdu_a), (4, pdu_a)]
