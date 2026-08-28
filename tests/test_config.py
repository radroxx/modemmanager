"""Настройки: загрузка, проверка обязательных значений, атомарная перезапись."""

from __future__ import annotations

import json
import os

import pytest

from modemmanager.config import ConfigError, Settings, SettingsStore


def test_missing_file_creates_defaults(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    settings = store.load()

    assert path.exists()
    assert settings.web.port == 8080
    assert "option1" in settings.discovery.drivers
    assert settings.discovery.at_trace is False
    assert settings.sims == {}


def test_at_trace_defaults_to_false_when_absent(tmp_path):
    """Настройка построчной трассировки AT-обмена -- необязательная, по умолчанию выключена."""
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"discovery": {"scan_interval": 3.0}}), encoding="utf-8"
    )

    settings = SettingsStore(path).load()

    assert settings.discovery.at_trace is False
    assert settings.discovery.scan_interval == 3.0


def test_at_trace_is_read_from_settings(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"discovery": {"at_trace": True}}), encoding="utf-8"
    )

    settings = SettingsStore(path).load()

    assert settings.discovery.at_trace is True


def test_partial_file_keeps_defaults_for_absent_sections(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"web": {"port": 9999}}), encoding="utf-8")

    settings = SettingsStore(path).load()

    assert settings.web.port == 9999
    assert settings.web.host == "127.0.0.1"  # значение по умолчанию сохранено
    assert settings.telegram.token == ""
    assert settings.discovery.scan_interval == 2.0


def test_unknown_fields_are_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "web": {"port": 8081, "colour": "red"},
                "unknown_section": {"anything": 1},
                "sims": {"8970": {"label": "a", "future_field": True}},
            }
        ),
        encoding="utf-8",
    )

    settings = SettingsStore(path).load()

    assert settings.web.port == 8081
    assert settings.sims["8970"].label == "a"


def test_load_wipes_sims_from_old_schema(tmp_path):
    """При загрузке файла без ``schema_version`` секция ``sims`` очищается.

    Ключи там хранились по ICCID; матчить их к новому ключу IMSI нельзя,
    поэтому раздел сбрасывается один раз при переходе на новую версию.
    """
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"sims": {"89701020123456789042": {"label": "рабочая"}}}),
        encoding="utf-8",
    )

    settings = SettingsStore(path).load()

    assert settings.sims == {}
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert reloaded.get("sims") == {}
    assert reloaded.get("schema_version") == 2

    # Повторная загрузка того же файла уже не выводит предупреждения и
    # оставляет `sims` в состоянии, записанном на предыдущем шаге.
    assert SettingsStore(path).load().sims == {}


def test_broken_json_is_reported(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ConfigError):
        SettingsStore(path).load()


def test_saved_file_mode_is_private(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.load()

    assert store.mode() == 0o600


def test_interrupted_save_keeps_previous_version(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.load()
    store.settings.web.password = "first"
    store.save()
    before = path.read_text(encoding="utf-8")

    real_replace = os.replace

    def failing_replace(src, dst):
        raise OSError("прервано на самом переименовании")

    monkeypatch.setattr(os, "replace", failing_replace)
    store.settings.web.password = "second"
    with pytest.raises(OSError):
        store.save()
    monkeypatch.setattr(os, "replace", real_replace)

    # На диске осталась целая прежняя версия, а не обрезанная новая.
    assert path.read_text(encoding="utf-8") == before
    assert json.loads(before)["web"]["password"] == "first"


def test_interrupted_serialisation_leaves_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.load()
    store.save()

    def failing_fsync(_fd):
        raise OSError("диск отвалился посреди записи")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(OSError):
        store.save()

    assert not (tmp_path / "settings.json.tmp").exists()
    json.loads(path.read_text(encoding="utf-8"))  # прежняя версия читается целиком


def test_missing_required_lists_all_gaps():
    settings = Settings()

    assert settings.missing_required() == [
        "web.password",
        "telegram.token",
        "telegram.admin_chat_id",
    ]

    settings.web.password = "x"
    settings.telegram.token = "y"
    settings.telegram.admin_chat_id = "z"
    assert settings.missing_required() == []


def test_sim_pin_is_write_only_in_public_dict(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.load()
    store.update_sim("8970123", {"label": "Роуминг", "pin": "1234"})

    public = store.settings.sims["8970123"].public_dict()

    assert public["pin_set"] is True
    assert "pin" not in public
    assert public["label"] == "Роуминг"


def test_update_sim_without_pin_key_keeps_previous_pin(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.load()
    store.update_sim("8970123", {"pin": "1234"})

    store.update_sim("8970123", {"label": "переименовали"})

    assert store.settings.sims["8970123"].pin == "1234"


def test_update_sim_with_null_pin_removes_it(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    store.load()
    store.update_sim("8970123", {"pin": "1234"})

    store.update_sim("8970123", {"pin": None})

    assert store.settings.sims["8970123"].pin == ""


def test_settings_survive_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.load()
    store.settings.web.password = "secret"
    store.settings.telegram.token = "token"
    store.settings.telegram.admin_chat_id = "42"
    store.update_sim("8970123", {"label": "Тест", "pin": "1234", "plmn": "25002"})

    reloaded = SettingsStore(path).load()

    assert reloaded.web.password == "secret"
    assert reloaded.sims["8970123"].label == "Тест"
    assert reloaded.sims["8970123"].pin == "1234"
    assert reloaded.sims["8970123"].plmn == "25002"
