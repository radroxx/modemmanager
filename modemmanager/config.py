"""Настройки приложения: загрузка, проверка и атомарная перезапись.

Единственный файл настроек -- намерения пользователя. Состояние рантайма здесь
не хранится и на диск не попадает вообще (см. design.md, D12).
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SETTINGS_MODE = 0o600

DEFAULT_DRIVERS = ["option1", "ch341-uart", "cp210x", "ftdi_sio"]
DEFAULT_PROBE_BAUDRATES = [115200, 9600]


class ConfigError(Exception):
    """Настройки непригодны для запуска."""


def _coerce(cls: type, raw: Any) -> Any:
    """Строит dataclass из словаря, игнорируя неизвестные поля."""
    if not isinstance(raw, dict):
        return cls()
    known = {f.name: f for f in fields(cls)}
    kwargs: dict[str, Any] = {}
    for name, value in raw.items():
        target = known.get(name)
        if target is None:
            continue  # неизвестное поле -- игнорируем, не падаем
        kwargs[name] = value
    return cls(**kwargs)


@dataclass
class SimSettings:
    """Пользовательские настройки одной SIM-карты, ключ -- IMSI."""

    label: str = ""
    msisdn: str = ""
    pin: str = ""
    plmn: str = ""
    chat_id: str = ""

    def public_dict(self) -> dict[str, Any]:
        """Представление для API: PIN только для записи (см. web-interface)."""
        return {
            "label": self.label,
            "msisdn": self.msisdn,
            "plmn": self.plmn,
            "chat_id": self.chat_id,
            "pin_set": bool(self.pin),
        }


@dataclass
class WebSettings:
    host: str = "127.0.0.1"
    port: int = 8080
    password: str = ""


@dataclass
class TelegramSettings:
    token: str = ""
    admin_chat_id: str = ""
    api_base: str = "https://api.telegram.org"
    max_retry_delay: float = 300.0


@dataclass
class DiscoverySettings:
    drivers: list[str] = field(default_factory=lambda: list(DEFAULT_DRIVERS))
    sysfs_root: str = "/sys"
    dev_root: str = "/dev"
    scan_interval: float = 2.0
    gone_debounce: float = 5.0
    probe_baudrates: list[int] = field(
        default_factory=lambda: list(DEFAULT_PROBE_BAUDRATES)
    )
    probe_timeout: float = 5.0
    # Сколько молчаливых проб подряд накопить, прежде чем поставить крестик
    # «не модем». Модем после появления портов может задерживаться с ответом.
    probe_max_attempts: int = 5
    # База экспоненциальной задержки между попытками (секунды): 1-я задержка
    # равна ей, каждая следующая -- вдвое больше.
    probe_retry_backoff: float = 2.0
    # Закрепление скорости за конкретным путём USB-интерфейса, если автоопределение
    # нежелательно: {"1-2:1.0": 115200}
    port_baudrate: dict[str, int] = field(default_factory=dict)
    fault_retry_interval: float = 60.0
    # Построчная диагностика AT-обмена в журнал уровня DEBUG. Включается вручную:
    # объём вывода при штатной работе велик, а автоматика по уровню логгера
    # не различает «интересно всё» и «только этот участок».
    at_trace: bool = False


@dataclass
class IntervalSettings:
    signal: float = 30.0
    registration: float = 30.0
    storage: float = 300.0
    # Сколько модем может быть без регистрации при заданном операторе до алерта
    no_service_alert: float = 900.0


@dataclass
class SmsSettings:
    assembly_timeout: float = 600.0


@dataclass
class CallSettings:
    clip_wait: float = 2.0
    ring_dedup: float = 15.0


#: Текущая версия формата ``settings.json``. Инкрементируется при
#: несовместимых изменениях, требующих одноразовой миграции содержимого файла.
#: Версия 2 -- переход с ICCID на IMSI как ключ раздела ``sims``.
SCHEMA_VERSION = 2


@dataclass
class Settings:
    web: WebSettings = field(default_factory=WebSettings)
    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    discovery: DiscoverySettings = field(default_factory=DiscoverySettings)
    intervals: IntervalSettings = field(default_factory=IntervalSettings)
    sms: SmsSettings = field(default_factory=SmsSettings)
    calls: CallSettings = field(default_factory=CallSettings)
    sims: dict[str, SimSettings] = field(default_factory=dict)
    #: Версия формата, под который записан файл на диске. Значение ``0`` --
    #: файл ещё не мигрирован (записан старой версией приложения).
    schema_version: int = SCHEMA_VERSION

    # ------------------------------------------------------------------ загрузка

    @classmethod
    def from_dict(cls, raw: Any) -> "Settings":
        if not isinstance(raw, dict):
            raw = {}
        version = 0
        raw_version = raw.get("schema_version")
        if isinstance(raw_version, int) and raw_version > 0:
            version = raw_version
        # Раздел ``sims`` до версии 2 хранился по ICCID; после -- по IMSI.
        # Ключи одного к другому не сматчатся, поэтому при старом файле
        # секцию сбрасываем в пустой словарь (см. design.md, D4). После
        # первого сохранения версия в файле обновляется, и повторного
        # сброса не будет.
        sims: dict[str, SimSettings] = {}
        sims_raw = raw.get("sims")
        if isinstance(sims_raw, dict):
            if version < SCHEMA_VERSION:
                if sims_raw:
                    log.warning(
                        "настройки SIM сброшены при переходе на IMSI "
                        "(было записей: %d)",
                        len(sims_raw),
                    )
            else:
                for imsi, value in sims_raw.items():
                    sims[str(imsi)] = _coerce(SimSettings, value)
        return cls(
            web=_coerce(WebSettings, raw.get("web")),
            telegram=_coerce(TelegramSettings, raw.get("telegram")),
            discovery=_coerce(DiscoverySettings, raw.get("discovery")),
            intervals=_coerce(IntervalSettings, raw.get("intervals")),
            sms=_coerce(SmsSettings, raw.get("sms")),
            calls=_coerce(CallSettings, raw.get("calls")),
            sims=sims,
            schema_version=SCHEMA_VERSION,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # ------------------------------------------------------------------ проверка

    def missing_required(self) -> list[str]:
        """Обязательные значения, без которых запускаться нельзя."""
        missing = []
        if not self.web.password:
            missing.append("web.password")
        if not self.telegram.token:
            missing.append("telegram.token")
        if not self.telegram.admin_chat_id:
            missing.append("telegram.admin_chat_id")
        return missing

    def sim(self, imsi: str) -> SimSettings:
        """Настройки SIM; отсутствующая карта даёт пустые настройки."""
        return self.sims.get(imsi, SimSettings())

    def is_configured(self, imsi: str) -> bool:
        return imsi in self.sims


class SettingsStore:
    """Владеет файлом настроек: читает, отдаёт, атомарно перезаписывает."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.settings = Settings()

    def load(self) -> Settings:
        """Читает файл; отсутствующий или битый файл не мешает запуску."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.settings = Settings()
            self.save()
            return self.settings
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{self.path}: некорректный JSON: {exc}") from exc
        # Файл, записанный старой версией, не имеет поля ``schema_version`` --
        # после ``from_dict`` версия обновится до текущей, и его нужно
        # переписать на диск, чтобы миграция раздела ``sims`` не повторялась
        # на следующем запуске (см. design.md, D4).
        raw_version = raw.get("schema_version") if isinstance(raw, dict) else None
        self.settings = Settings.from_dict(raw)
        if raw_version != SCHEMA_VERSION:
            self.save()
        return self.settings

    def save(self) -> None:
        """Перезапись через временный файл: на диске всегда целая версия."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(
            self.settings.to_dict(), ensure_ascii=False, indent=2, sort_keys=False
        )
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SETTINGS_MODE)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.chmod(tmp, SETTINGS_MODE)
        os.replace(tmp, self.path)
        self._fsync_dir()

    def _fsync_dir(self) -> None:
        try:
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)

    # ------------------------------------------------------------ правка настроек

    def update_sim(self, imsi: str, patch: dict[str, Any]) -> SimSettings:
        """Обновляет настройки SIM.

        PIN обрабатывается особо: отсутствие ключа сохраняет прежнее значение,
        ``None`` удаляет, строка задаёт новое (см. web-interface, write-only PIN).
        """
        current = self.settings.sims.get(imsi)
        if current is None:
            current = SimSettings()
            self.settings.sims[imsi] = current
        for key in ("label", "msisdn", "plmn", "chat_id"):
            if key in patch:
                value = patch[key]
                setattr(current, key, "" if value is None else str(value))
        if "pin" in patch:
            value = patch["pin"]
            current.pin = "" if value is None else str(value)
        self.save()
        return current

    def mode(self) -> int:
        """Права файла настроек, для проверки в тестах."""
        return stat.S_IMODE(self.path.stat().st_mode)
