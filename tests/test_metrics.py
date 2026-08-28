"""Метрики Prometheus."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fake_answers import HUAWEI
from fake_modem import FakeTransport
from starlette.testclient import TestClient

from modemmanager.behaviors import HuaweiBehavior
from modemmanager.config import IntervalSettings, SettingsStore, SimSettings
from modemmanager.discovery.sysfs import UsbDevice
from modemmanager.events import Event, EventType
from modemmanager.metrics import Metrics
from modemmanager.modem import Modem, ModemStatus
from modemmanager.modem_registry import ModemRegistry
from modemmanager.values import (
    Identity,
    PinAttempts,
    Registration,
    RegistrationState,
    Signal,
    Storage,
)
from modemmanager.web.server import WebServer


IMSI = "89701020123456789042"


def _store(tmp_path) -> SettingsStore:
    store = SettingsStore(tmp_path / "settings.json")
    store.settings.web.password = "secret"
    store.settings.telegram.token = "token-xyz"
    store.settings.telegram.admin_chat_id = "admin"
    return store


def _make_modem(*, usb_path="3-1", imsi=IMSI, imei="861234567890123", sim_label="Работа") -> Modem:
    transport = FakeTransport(dict(HUAWEI))
    modem = Modem(
        device=UsbDevice(usb_path=usb_path, ports=[transport.port], drivers={"option1"}),
        behavior=HuaweiBehavior(),
        transport=transport,
        bus=None,  # шина метрикам не нужна
        identity=Identity(manufacturer="huawei", model="E3372", imei=imei),
        intervals=IntervalSettings(),
    )
    modem.state.imsi = imsi
    modem.state.sim_label = sim_label
    modem.state.status = ModemStatus.ONLINE
    modem.state.signal = Signal(dbm=-79, raw=17)
    modem.state.registration = Registration(
        voice=RegistrationState.ROAMING, data=RegistrationState.REGISTERED
    )
    modem.state.storage = Storage(used=3, total=20, name="SM")
    modem.state.pin_attempts = PinAttempts(pin=3, source="AT^CPIN?")
    modem.state.reconnects = 2
    modem.state.last_poll = 1_700_000_000.0
    modem.state.last_sms = 1_700_000_100.0
    return modem


# ------------------------------------- 11.1 эндпоинт метрик в формате Prometheus

def test_metrics_endpoint_returns_prometheus_format(tmp_path):
    store = _store(tmp_path)
    registry = ModemRegistry()
    registry.add(_make_modem())
    metrics = Metrics(registry, store)

    web = WebServer(store=store, registry=registry, event_log=None, metrics=metrics)  # type: ignore[arg-type]
    with TestClient(web.app) as client:
        response = client.get("/metrics")
        assert response.status_code == 200
        text = response.text
    # Формат Prometheus: строки ``HELP``, ``TYPE`` и значения.
    assert "# HELP mm_modem_present" in text
    assert "# TYPE mm_modem_present gauge" in text
    assert 'mm_modem_present{imei="861234567890123"} 1.0' in text
    # Content-Type начинается с ``text/plain`` -- скрейпер это ждёт.
    assert response.headers["content-type"].startswith("text/plain")


# --------- 11.2 состояния, сигнал, регистрация с оператором и роумингом, память, PIN

class TestMeasurementMetrics:
    def test_signal_and_storage_and_pin_attempts(self, tmp_path):
        store = _store(tmp_path)
        registry = ModemRegistry()
        registry.add(_make_modem())
        metrics = Metrics(registry, store)
        text = metrics.render()

        assert f'mm_signal_dbm{{imsi="{IMSI}"}} -79.0' in text
        assert f'mm_storage_used{{imsi="{IMSI}"}} 3.0' in text
        assert f'mm_storage_total{{imsi="{IMSI}"}} 20.0' in text
        assert f'mm_pin_attempts_left{{imsi="{IMSI}"}} 3.0' in text

    def test_registration_split_and_roaming_flag(self, tmp_path):
        store = _store(tmp_path)
        registry = ModemRegistry()
        registry.add(_make_modem())
        metrics = Metrics(registry, store)
        text = metrics.render()

        # Голос в роуминге (5), данные зарегистрированы (2).
        assert f'mm_registration_state{{domain="voice",imsi="{IMSI}"}} 5.0' in text
        assert f'mm_registration_state{{domain="data",imsi="{IMSI}"}} 2.0' in text
        assert f'mm_roaming{{imsi="{IMSI}"}} 1.0' in text

    def test_modem_status_and_presence(self, tmp_path):
        store = _store(tmp_path)
        registry = ModemRegistry()
        modem = _make_modem()
        registry.add(modem)
        metrics = Metrics(registry, store)
        text = metrics.render()

        assert 'mm_modem_present{imei="861234567890123"} 1.0' in text
        assert 'mm_modem_status{imei="861234567890123",status="online"} 1.0' in text
        # Другие возможные состояния должны быть выставлены в 0.
        assert 'mm_modem_status{imei="861234567890123",status="fault"} 0.0' in text

    def test_unknown_signal_is_not_emitted_as_zero(self, tmp_path):
        """Неизвестный уровень сигнала не выставляется как 0."""
        store = _store(tmp_path)
        registry = ModemRegistry()
        modem = _make_modem()
        modem.state.signal = Signal.unknown()
        registry.add(modem)
        metrics = Metrics(registry, store)
        text = metrics.render()

        # ``mm_signal_dbm`` для этого imsi не должен появиться со значением 0.
        assert f'mm_signal_dbm{{imsi="{IMSI}"}}' not in text


# ------------------------ 11.3 счётчики сообщений, вызовов, ошибок, переподключений

class TestCounters:
    def test_sms_and_call_counters_grow(self, tmp_path):
        store = _store(tmp_path)
        registry = ModemRegistry()
        registry.add(_make_modem())
        metrics = Metrics(registry, store)

        async def push():
            await metrics.on_event(Event(type=EventType.SMS, imsi=IMSI, data={"text": "a"}))
            await metrics.on_event(Event(type=EventType.SMS, imsi=IMSI, data={"text": "b"}))
            await metrics.on_event(
                Event(type=EventType.CALL, imsi=IMSI, data={"outcome": "rejected"})
            )

        asyncio.run(push())
        text = metrics.render()
        assert f'mm_sms_received_total{{imsi="{IMSI}"}} 2.0' in text
        assert f'mm_calls_rejected_total{{imsi="{IMSI}"}} 1.0' in text

    def test_at_errors_and_reconnects_come_from_state(self, tmp_path):
        store = _store(tmp_path)
        registry = ModemRegistry()
        modem = _make_modem()
        modem.state.error_count = 5
        registry.add(modem)
        metrics = Metrics(registry, store)
        text = metrics.render()

        assert 'mm_at_errors_total{imei="861234567890123"} 5.0' in text
        assert 'mm_reconnects_total{imei="861234567890123"} 2.0' in text

    def test_notification_counters_via_hooks(self, tmp_path):
        store = _store(tmp_path)
        metrics = Metrics(ModemRegistry(), store)
        metrics.on_notification_failed("sms")
        metrics.on_notification_failed("sms")
        metrics.on_notification_sent("modem_gone")
        text = metrics.render()

        assert 'mm_notifications_failed_total{event_type="sms"} 2.0' in text
        assert 'mm_notifications_sent_total{event_type="modem_gone"} 1.0' in text


# ------------------------ 11.4 время последнего сообщения и последнего опроса

class TestTimestamps:
    def test_last_sms_and_last_poll_are_exposed(self, tmp_path):
        store = _store(tmp_path)
        registry = ModemRegistry()
        registry.add(_make_modem())
        metrics = Metrics(registry, store)
        text = metrics.render()

        # ``prometheus_client`` печатает большие числа в экспоненциальной форме;
        # проверяем наличие меток и то, что модем и SIM опознаются, а сами
        # значения -- через второй прогон после обновления.
        assert f'mm_last_sms_timestamp_seconds{{imsi="{IMSI}"}}' in text
        assert 'mm_last_poll_timestamp_seconds{imei="861234567890123"}' in text

        # Изменяем значения и убеждаемся, что рендер отражает новые числа.
        modem = next(iter(registry))
        modem.state.last_sms = 2_000_000_000.0
        modem.state.last_poll = 2_100_000_000.0
        updated = metrics.render()
        assert "2e+09" in updated
        assert "2.1e+09" in updated


# ------------------- 11.5 инфо-метрика с изменяемыми полями; метки значений неизменны

class TestSimInfoIsSeparate:
    def test_renaming_sim_does_not_change_value_metrics(self, tmp_path):
        store = _store(tmp_path)
        store.settings.sims[IMSI] = SimSettings(label="Старое имя", msisdn="+79999")
        registry = ModemRegistry()
        registry.add(_make_modem(sim_label="Старое имя"))
        metrics = Metrics(registry, store)
        first = metrics.render()

        # Пользователь переименовал SIM.
        store.settings.sims[IMSI].label = "Роуминг-МТС"
        # Модем ещё не обновил своё представление -- это нормально: изменяемые
        # поля живут в настройках.
        second = metrics.render()

        # Значение сигнала не изменилось и метка идентификатора та же.
        assert f'mm_signal_dbm{{imsi="{IMSI}"}} -79.0' in first
        assert f'mm_signal_dbm{{imsi="{IMSI}"}} -79.0' in second
        # А инфо-метрика теперь несёт новое имя.
        assert 'label="Роуминг-МТС"' in second

    def test_measurement_metrics_carry_only_imsi_no_label(self, tmp_path):
        store = _store(tmp_path)
        store.settings.sims[IMSI] = SimSettings(label="Работа")
        registry = ModemRegistry()
        registry.add(_make_modem())
        metrics = Metrics(registry, store)
        text = metrics.render()
        # Значения не имеют метки ``label`` -- только ``imsi``.
        for line in text.splitlines():
            if line.startswith("mm_signal_dbm") or line.startswith("mm_storage_used"):
                assert "label=" not in line, line


# ------------------------------------------------- 11.6 отсутствие секретов

class TestSecretsAreNotEmitted:
    def test_metrics_output_contains_no_pin_password_token_or_sms_text(self, tmp_path):
        store = _store(tmp_path)
        # Пусть в настройках лежат секреты -- посмотрим, что они не попадут в /metrics.
        store.settings.web.password = "web-password-shhh"
        store.settings.telegram.token = "bot-token-shhh"
        store.settings.sims[IMSI] = SimSettings(pin="8642", label="Работа", msisdn="+79999")
        registry = ModemRegistry()
        registry.add(_make_modem())
        metrics = Metrics(registry, store)

        secret_sms_text = "TOTALLY-SECRET-SMS-BODY-XYZ"

        async def push_sms():
            await metrics.on_event(
                Event(
                    type=EventType.SMS,
                    imsi=IMSI,
                    data={"text": secret_sms_text, "from": "+79999"},
                )
            )

        asyncio.run(push_sms())
        text = metrics.render()

        assert "web-password-shhh" not in text
        assert "bot-token-shhh" not in text
        assert "8642" not in text
        assert secret_sms_text not in text
