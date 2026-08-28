"""Жизненный цикл приложения: порядок запуска, остановка по сигналу, проверка настроек."""

from __future__ import annotations

import asyncio
import json
import os
import signal

import pytest

from modemmanager.__main__ import main, parse_args
from modemmanager.app import Application
from modemmanager.config import ConfigError
from modemmanager.events import Event, EventType


class RecordingComponent:
    def __init__(self, name: str, journal: list[str]):
        self.name = name
        self.journal = journal
        self.running = False

    async def start(self) -> None:
        self.running = True
        self.journal.append("start:" + self.name)

    async def stop(self) -> None:
        self.running = False
        self.journal.append("stop:" + self.name)

    async def on_event(self, event: Event) -> None:
        self.journal.append("event:" + self.name)


class StubApplication(Application):
    """Приложение с заглушками вместо портов, Telegram и HTTP-сервера."""

    def __init__(self, *args, **kwargs):
        self.journal: list[str] = []
        super().__init__(*args, **kwargs)

    def build_components(self) -> None:
        self.registry = RecordingComponent("registry", self.journal)
        self.metrics = RecordingComponent("metrics", self.journal)
        self.notifier = RecordingComponent("notifier", self.journal)
        self.reconciler = RecordingComponent("reconciler", self.journal)
        self.web = RecordingComponent("web", self.journal)
        self.bus.subscribe(self.registry.on_event, priority=10, name="registry")
        self.bus.subscribe(self.metrics.on_event, priority=20, name="metrics")
        self.bus.subscribe(self.notifier.on_event, priority=30, name="notifier")


def _write_valid_settings(tmp_path, **discovery):
    path = tmp_path / "settings.json"
    payload = {
        "web": {"password": "secret"},
        "telegram": {"token": "T", "admin_chat_id": "42"},
    }
    if discovery:
        payload["discovery"] = discovery
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


async def _await_started(app: StubApplication) -> None:
    for _ in range(1000):
        if getattr(app, "web", None) is not None and app.web.running:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"приложение не поднялось: {app.journal}")


async def test_application_starts_and_stops_in_reverse_order(tmp_path):
    config = _write_valid_settings(tmp_path)
    app = StubApplication(config, tmp_path / "events.jsonl")

    task = asyncio.create_task(app.run())
    await _await_started(app)
    assert app.journal == ["start:notifier", "start:reconciler", "start:web"]

    app.request_stop()
    await asyncio.wait_for(task, timeout=5)

    assert app.journal == [
        "start:notifier",
        "start:reconciler",
        "start:web",
        "stop:web",
        "stop:reconciler",
        "stop:notifier",
    ]


async def test_termination_signal_stops_the_application(tmp_path):
    """SIGTERM должен приводить к штатной остановке, а не к обрыву."""
    config = _write_valid_settings(tmp_path)
    app = StubApplication(config, tmp_path / "events.jsonl")
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, app.request_stop)
    try:
        task = asyncio.create_task(app.run())
        await _await_started(app)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, timeout=5)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)

    assert app.journal[-3:] == ["stop:web", "stop:reconciler", "stop:notifier"]
    assert not app.web.running


async def test_failure_of_one_component_does_not_block_shutdown(tmp_path):
    config = _write_valid_settings(tmp_path)
    app = StubApplication(config, tmp_path / "events.jsonl")

    async def broken_stop() -> None:
        raise RuntimeError("порт уже отвалился")

    task = asyncio.create_task(app.run())
    await _await_started(app)
    app.web.stop = broken_stop

    app.request_stop()
    await asyncio.wait_for(task, timeout=5)

    assert "stop:reconciler" in app.journal
    assert "stop:notifier" in app.journal


async def test_events_reach_journal_on_disk(tmp_path):
    config = _write_valid_settings(tmp_path)
    app = StubApplication(config, tmp_path / "events.jsonl")
    app.build()

    await app.bus.publish(Event(type=EventType.SMS, at=1.0, imsi="8970", data={"text": "x"}))

    records = await app.event_log.tail(10)
    assert records[0]["text"] == "x"
    # Журнал впереди всех потребителей.
    assert app.journal == ["event:registry", "event:metrics", "event:notifier"]


async def test_missing_required_settings_refuse_start(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}", encoding="utf-8")
    app = StubApplication(path, tmp_path / "events.jsonl")

    with pytest.raises(ConfigError) as excinfo:
        app.build()

    message = str(excinfo.value)
    assert "web.password" in message
    assert "telegram.token" in message


async def test_missing_required_settings_can_be_tolerated(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}", encoding="utf-8")
    app = StubApplication(path, tmp_path / "events.jsonl", require_settings=False)

    app.build()  # не падает: интерфейс нужен, чтобы настройки можно было ввести


def test_check_config_reports_gaps(tmp_path, capsys):
    path = tmp_path / "settings.json"
    code = main(["--config", str(path), "--events", str(tmp_path / "e.jsonl"), "--check-config"])

    assert code == 1
    assert "web.password" in capsys.readouterr().err


def test_check_config_accepts_complete_settings(tmp_path, capsys):
    config = _write_valid_settings(tmp_path)
    code = main(
        ["--config", str(config), "--events", str(tmp_path / "e.jsonl"), "--check-config"]
    )

    assert code == 0
    assert "порядке" in capsys.readouterr().out


async def test_build_wires_per_modem_services(tmp_path):
    """`Application.build_components` подключает к новому модему все части обслуживания."""
    from modemmanager.calls import CallService
    from modemmanager.network import NetworkService
    from modemmanager.sim import SimService
    from modemmanager.sms.service import SmsService

    path = _write_valid_settings(tmp_path)
    app = Application(path, tmp_path / "events.jsonl")
    app.build()

    factory = app.reconciler.component_factory
    assert factory is not None
    components = factory(object())
    kinds = {type(component) for component in components}
    assert kinds == {SimService, SmsService, CallService, NetworkService}


def test_at_trace_with_info_level_warns(tmp_path, caplog):
    """Трассировка включена, а уровень журнала строже DEBUG -- пользователя предупредить."""
    import logging

    path = _write_valid_settings(tmp_path, at_trace=True)
    app = StubApplication(path, tmp_path / "events.jsonl")

    root = logging.getLogger()
    saved = root.level
    root.setLevel(logging.INFO)
    try:
        with caplog.at_level(logging.WARNING, logger="modemmanager.app"):
            app.build()
    finally:
        root.setLevel(saved)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "трассировка" in r.getMessage()]
    assert len(warnings) == 1
    assert "INFO" in warnings[0].getMessage()


def test_at_trace_with_debug_level_stays_silent(tmp_path, caplog):
    """При at_trace=True и DEBUG предупреждения быть не должно."""
    import logging

    path = _write_valid_settings(tmp_path, at_trace=True)
    app = StubApplication(path, tmp_path / "events.jsonl")

    root = logging.getLogger()
    saved = root.level
    root.setLevel(logging.DEBUG)
    try:
        with caplog.at_level(logging.WARNING, logger="modemmanager.app"):
            app.build()
    finally:
        root.setLevel(saved)

    assert not any("трассировка" in r.getMessage() for r in caplog.records)


def test_at_trace_off_never_warns(tmp_path, caplog):
    """Если трассировка выключена, предупреждение не пишется независимо от уровня."""
    import logging

    path = _write_valid_settings(tmp_path)  # at_trace по умолчанию false
    app = StubApplication(path, tmp_path / "events.jsonl")

    root = logging.getLogger()
    saved = root.level
    root.setLevel(logging.INFO)
    try:
        with caplog.at_level(logging.WARNING, logger="modemmanager.app"):
            app.build()
    finally:
        root.setLevel(saved)

    assert not any("трассировка" in r.getMessage() for r in caplog.records)


def test_default_paths():
    args = parse_args([])

    assert args.config == "settings.json"
    assert args.events == "events.jsonl"
    assert args.log_level == "INFO"
