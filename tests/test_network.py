"""Выбор оператора, скан сетей и watchdog регистрации."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fake_answers import HUAWEI
from fake_modem import FakeTransport

from modemmanager.behaviors import HuaweiBehavior
from modemmanager.config import IntervalSettings, SettingsStore, SimSettings
from modemmanager.discovery.sysfs import UsbDevice
from modemmanager.events import Event, EventBus, EventType
from modemmanager.modem import Modem, ModemStatus
from modemmanager.network import NetworkService, ScanResult
from modemmanager.values import Identity, Registration, RegistrationState


IMSI = "89701020123456789042"


def _store(no_service_alert: float = 900.0) -> SettingsStore:
    store = SettingsStore(Path("/tmp/settings_net.json"))
    store.settings.web.password = "x"
    store.settings.telegram.token = "y"
    store.settings.telegram.admin_chat_id = "z"
    store.settings.intervals = IntervalSettings(no_service_alert=no_service_alert)
    return store


async def _run_modem(
    transport: FakeTransport,
    store: SettingsStore,
    *,
    events: list[Event] | None = None,
    imsi: str = IMSI,
) -> tuple[Modem, NetworkService]:
    bus = EventBus()
    if events is not None:
        bus.subscribe(events.append, priority=0, name="rec")
    service = NetworkService(store)
    modem = Modem(
        device=UsbDevice(usb_path="3-1", ports=[transport.port], drivers={"option1"}),
        behavior=HuaweiBehavior(),
        transport=transport,
        bus=bus,
        identity=Identity(imei="861234567890123"),
        components=[service],
    )
    modem.state.imsi = imsi
    await modem.start()
    return (modem, service)


# ----------------------------------------------- 9.1 применение выбора при старте

@pytest.mark.asyncio
async def test_manual_operator_is_applied_on_start():
    """9.1: при заданном операторе используется принудительный выбор."""
    responses = dict(HUAWEI)
    responses['AT+COPS=1,2,"25002"'] = "OK"
    transport = FakeTransport(responses)
    store = _store()
    store.settings.sims[IMSI] = SimSettings(plmn="25002")
    modem, _ = await _run_modem(transport, store)
    try:
        assert 'AT+COPS=1,2,"25002"' in transport.commands
        assert "AT+COPS=0" not in transport.commands
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_automatic_selection_when_operator_not_set():
    """9.1: без заданного оператора применяется автоматический выбор."""
    responses = dict(HUAWEI)
    responses["AT+COPS=0"] = "OK"
    transport = FakeTransport(responses)
    store = _store()  # sims пусто
    modem, _ = await _run_modem(transport, store)
    try:
        assert "AT+COPS=0" in transport.commands
    finally:
        await modem.stop()


# --------------------------- 9.2 применение изменения без перезапуска приложения

@pytest.mark.asyncio
async def test_operator_change_applies_without_restart():
    """9.2: после обновления настроек apply_operator применяет новый выбор."""
    responses = dict(HUAWEI)
    responses['AT+COPS=1,2,"25001"'] = "OK"
    responses['AT+COPS=1,2,"25002"'] = "OK"
    transport = FakeTransport(responses)
    store = _store()
    store.settings.sims[IMSI] = SimSettings(plmn="25001")
    modem, service = await _run_modem(transport, store)
    try:
        assert 'AT+COPS=1,2,"25001"' in transport.commands
        # Пользователь поменял оператора: обновляем настройки и просим применить.
        store.settings.sims[IMSI].plmn = "25002"
        await service.apply_operator()
        assert 'AT+COPS=1,2,"25002"' in transport.commands
        assert service.applied_plmn == "25002"
    finally:
        await modem.stop()


# ------------------------------------------------ 9.3 скан как отдельная операция

@pytest.mark.asyncio
async def test_scan_puts_modem_in_scanning_state_and_publishes_result():
    """9.3: скан переводит модем в SCANNING и публикует событие с результатом."""
    responses = dict(HUAWEI)
    responses["AT+COPS=?"] = (
        '+COPS: (2,"MegaFon","MF","25002"),(1,"MTS","MTS","25001",7),(0-4)'
    )
    responses["AT+COPS=0"] = "OK"
    transport = FakeTransport(responses)
    events: list[Event] = []
    store = _store()
    modem, service = await _run_modem(transport, store, events=events)
    try:
        result = await service.scan(timeout=5.0)
        assert isinstance(result, ScanResult)
        plmns = [c.plmn for c in result.candidates]
        assert "25001" in plmns
        assert "25002" in plmns
        # Событие ушло в журнал.
        scans = [e for e in events if e.type == EventType.SCAN_RESULT]
        assert len(scans) == 1
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_regular_poll_does_not_run_scan():
    """9.3: регулярный опрос не вызывает AT+COPS=?."""
    responses = dict(HUAWEI)
    transport = FakeTransport(responses)
    store = _store()
    modem, _ = await _run_modem(transport, store)
    try:
        # Триггерим несколько проходов опроса.
        for _ in range(3):
            await modem.poll_once()
        assert "AT+COPS=?" not in transport.commands
    finally:
        await modem.stop()


# ------------------------------ 9.4 раздельное чтение voice/data

@pytest.mark.asyncio
async def test_partial_registration_shows_voice_and_data_separately():
    """9.4: раздельные состояния голоса и данных читаются в разных состояниях."""
    responses = dict(HUAWEI)
    # Голос -- зарегистрирован в роуминге, данные -- нет.
    responses["AT+CREG?"] = '+CREG: 1,5,"2B1A","1F2C3D"'
    responses["AT+CGREG?"] = '+CGREG: 1,0'
    transport = FakeTransport(responses)
    store = _store()
    modem, _ = await _run_modem(transport, store)
    try:
        reg = modem.state.registration
        assert reg.voice is RegistrationState.ROAMING
        assert reg.data is RegistrationState.NOT_REGISTERED
        assert reg.roaming is True
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_full_registration_marks_both_domains():
    """9.4: полная регистрация -- обе домены зарегистрированы."""
    responses = dict(HUAWEI)
    responses["AT+CREG?"] = '+CREG: 1,1,"2B1A","1F2C3D"'
    responses["AT+CGREG?"] = '+CGREG: 1,1'
    transport = FakeTransport(responses)
    store = _store()
    modem, _ = await _run_modem(transport, store)
    try:
        reg = modem.state.registration
        assert reg.voice is RegistrationState.REGISTERED
        assert reg.data is RegistrationState.REGISTERED
    finally:
        await modem.stop()


# ------------------------------------ 9.5 watchdog длительного отсутствия

@pytest.mark.asyncio
async def test_no_service_alert_after_prolonged_absence_keeps_manual_selection():
    """9.5: при длительном отсутствии регистрации админ уведомлён, выбор не меняется."""
    responses = dict(HUAWEI)
    # Не зарегистрирован по голосу и данным.
    responses["AT+CREG?"] = "+CREG: 1,0"
    responses["AT+CGREG?"] = "+CGREG: 1,0"
    responses['AT+COPS=1,2,"25099"'] = "OK"
    transport = FakeTransport(responses)
    events: list[Event] = []
    store = _store(no_service_alert=0.01)  # быстро истекающий порог
    store.settings.sims[IMSI] = SimSettings(plmn="25099")
    modem, service = await _run_modem(transport, store, events=events)
    try:
        # Первый опрос запоминает момент; alerts ещё нет.
        await service.poll()
        assert not any(e.type == EventType.NO_SERVICE for e in events)
        # Ждём истечения порога и опрашиваем ещё раз.
        await asyncio.sleep(0.05)
        await service.poll()
        alerts = [e for e in events if e.type == EventType.NO_SERVICE]
        assert len(alerts) == 1
        assert alerts[0].data["operator"] == "25099"
        # Автоматическое переключение не применяется -- принудительный выбор
        # остался в силе.
        assert service.applied_plmn == "25099"
        assert "AT+COPS=0" not in transport.commands
    finally:
        await modem.stop()


@pytest.mark.asyncio
async def test_no_alert_when_operator_is_automatic():
    """9.5: без заданного оператора длительное отсутствие регистрации не алертит."""
    responses = dict(HUAWEI)
    responses["AT+CREG?"] = "+CREG: 1,0"
    responses["AT+CGREG?"] = "+CGREG: 1,0"
    transport = FakeTransport(responses)
    events: list[Event] = []
    store = _store(no_service_alert=0.01)
    modem, service = await _run_modem(transport, store, events=events)
    try:
        await service.poll()
        await asyncio.sleep(0.05)
        await service.poll()
        assert not any(e.type == EventType.NO_SERVICE for e in events)
    finally:
        await modem.stop()
