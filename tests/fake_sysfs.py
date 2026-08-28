"""Подготовленное дерево sysfs для проверки перечисления портов.

Настоящее дерево ядра воспроизводится ровно в той части, на которую опирается
перечисление: каталог драйвера со ссылками на порты, интерфейс USB и каталог
устройства с идентификаторами.
"""

from __future__ import annotations

from pathlib import Path

DRIVER_SERVICE_FILES = ("bind", "unbind", "uevent", "new_id", "remove_id")


def make_root(tmp_path: Path) -> Path:
    """Создаёт пустой корень sysfs."""
    root = tmp_path / "sys"
    (root / "bus" / "usb-serial" / "drivers").mkdir(parents=True)
    return root


def add_driver(root: Path, driver: str) -> Path:
    """Создаёт каталог драйвера вместе со служебными файлами."""
    driver_dir = root / "bus" / "usb-serial" / "drivers" / driver
    driver_dir.mkdir(parents=True, exist_ok=True)
    for name in DRIVER_SERVICE_FILES:
        (driver_dir / name).write_text("")
    return driver_dir


def add_device(
    root: Path,
    *,
    usb_path: str,
    ports: list[str],
    driver: str,
    vendor_id: str = "12d1",
    product_id: str = "1001",
    manufacturer: str = "HUAWEI Technology",
    product: str = "HUAWEI Mobile",
    serial: str = "",
) -> Path:
    """Добавляет USB-устройство с портами, привязанными к драйверу.

    Порты лежат внутри интерфейсов устройства, как в настоящем sysfs, а в каталоге
    драйвера появляются символические ссылки на них.
    """
    device_dir = root / "devices" / "usb1" / usb_path
    device_dir.mkdir(parents=True, exist_ok=True)
    (device_dir / "idVendor").write_text(vendor_id + "\n")
    (device_dir / "idProduct").write_text(product_id + "\n")
    (device_dir / "manufacturer").write_text(manufacturer + "\n")
    (device_dir / "product").write_text(product + "\n")
    if serial:
        (device_dir / "serial").write_text(serial + "\n")

    driver_dir = add_driver(root, driver)
    for index, port in enumerate(ports):
        interface = device_dir / f"{usb_path}:1.{index}"
        interface.mkdir(parents=True, exist_ok=True)
        port_dir = interface / port
        port_dir.mkdir(parents=True, exist_ok=True)
        (driver_dir / port).symlink_to(port_dir, target_is_directory=True)
    return device_dir
