"""Перечисление портов-кандидатов по списку драйверов.

Фильтр по драйверу -- первый и самый дешёвый шаг: он отсекает посторонние
USB-UART устройства до того, как в порт будет записан хотя бы один байт. Порты
группируются по родительскому USB-устройству, потому что одно устройство может
отдавать несколько портов, и модемом является устройство, а не порт.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: Где ядро перечисляет драйверы последовательных USB-портов.
DRIVERS_SUBPATH = "bus/usb-serial/drivers"

#: Признак каталога USB-устройства (а не интерфейса или порта).
USB_DEVICE_MARKER = "idVendor"


@dataclass
class UsbDevice:
    """Одно физическое USB-устройство с его последовательными портами."""

    #: Путь устройства на шине, например ``1-1.2``. Устойчив к перенумерации
    #: `ttyUSB*`, но меняется при переключении в другой порт хаба.
    usb_path: str
    sysfs_path: str = ""
    vendor_id: str = ""
    product_id: str = ""
    manufacturer: str = ""
    product: str = ""
    #: Серийный номер USB. У части модемов пуст -- полагаться на него нельзя.
    serial: str = ""
    #: Устройства портов в порядке, в котором их перечислило ядро.
    ports: list[str] = field(default_factory=list)
    #: Драйверы, через которые найдены порты этого устройства.
    drivers: set[str] = field(default_factory=set)

    @property
    def hint(self) -> str:
        """Пара идентификаторов USB. Только подсказка: за универсальным мостом
        идентификаторы принадлежат мосту, а не модему."""
        if self.vendor_id and self.product_id:
            return f"{self.vendor_id}:{self.product_id}"
        return ""

    @property
    def description(self) -> str:
        parts = [part for part in (self.manufacturer, self.product) if part]
        label = " ".join(parts) or self.hint or "неизвестное устройство"
        return f"{label} на {self.usb_path}"

    @property
    def single_port(self) -> bool:
        return len(self.ports) == 1


def enumerate_devices(
    drivers: list[str],
    *,
    sysfs_root: str = "/sys",
    dev_root: str = "/dev",
) -> list[UsbDevice]:
    """Возвращает USB-устройства, чьи порты привязаны к указанным драйверам.

    Список драйверов читается при каждом вызове, поэтому его изменение в
    настройках применяется на следующем цикле обнаружения без перезапуска.
    """
    devices: dict[str, UsbDevice] = {}
    for driver in drivers:
        driver_dir = Path(sysfs_root) / DRIVERS_SUBPATH / driver
        if not driver_dir.is_dir():
            log.debug("драйвер %s не загружен, пропускаю", driver)
            continue
        for entry in sorted(driver_dir.iterdir(), key=lambda item: _natural_key(item.name)):
            if not _looks_like_port(entry):
                continue
            device = _device_for_port(entry, driver=driver, dev_root=dev_root)
            if device is None:
                continue
            existing = devices.get(device.usb_path)
            if existing is None:
                devices[device.usb_path] = device
            else:
                existing.ports.extend(device.ports)
                existing.drivers |= device.drivers

    for device in devices.values():
        device.ports.sort(key=_natural_key)
    return sorted(devices.values(), key=lambda item: _natural_key(item.usb_path))


def _looks_like_port(entry: Path) -> bool:
    """Отличает запись о порте от служебных файлов каталога драйвера."""
    if entry.name in ("bind", "unbind", "uevent", "module", "new_id", "remove_id"):
        return False
    if not entry.is_symlink() and not entry.is_dir():
        return False
    return bool(re.match(r"^tty", entry.name))


def _device_for_port(entry: Path, *, driver: str, dev_root: str) -> UsbDevice | None:
    try:
        resolved = entry.resolve()
    except OSError as exc:  # устройство исчезло между перечислением и разбором
        log.debug("не удалось разобрать %s: %s", entry, exc)
        return None

    usb_dir = _find_usb_device_dir(resolved)
    if usb_dir is None:
        log.debug("для порта %s не найдено родительское USB-устройство", entry.name)
        return None

    return UsbDevice(
        usb_path=usb_dir.name,
        sysfs_path=str(usb_dir),
        vendor_id=_read(usb_dir / "idVendor"),
        product_id=_read(usb_dir / "idProduct"),
        manufacturer=_read(usb_dir / "manufacturer"),
        product=_read(usb_dir / "product"),
        serial=_read(usb_dir / "serial"),
        ports=[os.path.join(dev_root, entry.name)],
        drivers={driver},
    )


def _find_usb_device_dir(path: Path) -> Path | None:
    """Поднимается от порта к каталогу USB-устройства.

    Порт лежит внутри интерфейса (``1-1.2:1.0``), интерфейс -- внутри устройства
    (``1-1.2``). Устройство отличается наличием файла с идентификатором
    производителя.
    """
    for candidate in [path, *path.parents]:
        if (candidate / USB_DEVICE_MARKER).exists():
            return candidate
    return None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _natural_key(name: str) -> tuple:
    """Сортировка, при которой ``ttyUSB2`` идёт раньше ``ttyUSB10``."""
    return tuple(
        int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name) if part != ""
    )
