"""Перечисление портов по драйверам и группировка по USB-устройству."""

from __future__ import annotations

import fake_sysfs

from modemmanager.discovery.sysfs import enumerate_devices


def test_multiport_device_gives_one_record(tmp_path):
    """Модем -- это устройство, а не порт: три порта дают одну запись."""
    root = fake_sysfs.make_root(tmp_path)
    fake_sysfs.add_device(
        root,
        usb_path="3-1",
        ports=["ttyUSB0", "ttyUSB1", "ttyUSB2"],
        driver="option1",
    )

    devices = enumerate_devices(["option1"], sysfs_root=str(root), dev_root="/dev")

    assert len(devices) == 1
    device = devices[0]
    assert device.usb_path == "3-1"
    assert device.ports == ["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2"]
    assert device.single_port is False
    assert device.hint == "12d1:1001"
    assert device.drivers == {"option1"}


def test_single_port_devices_are_separate_modems(tmp_path):
    root = fake_sysfs.make_root(tmp_path)
    for index, usb_path in enumerate(("1-1.1", "1-1.2")):
        fake_sysfs.add_device(
            root,
            usb_path=usb_path,
            ports=[f"ttyUSB{index}"],
            driver="ch341-uart",
            vendor_id="1a86",
            product_id="7523",
            manufacturer="",
            product="USB Serial",
        )

    devices = enumerate_devices(["ch341-uart"], sysfs_root=str(root), dev_root="/dev")

    assert [device.usb_path for device in devices] == ["1-1.1", "1-1.2"]
    assert all(device.single_port for device in devices)


def test_ports_on_other_drivers_are_not_listed(tmp_path):
    """Устройство на неразрешённом драйвере не попадает в кандидаты вообще."""
    root = fake_sysfs.make_root(tmp_path)
    fake_sysfs.add_device(root, usb_path="3-1", ports=["ttyUSB0"], driver="option1")
    fake_sysfs.add_device(
        root,
        usb_path="4-1",
        ports=["ttyUSB1"],
        driver="pl2303",
        vendor_id="067b",
        product_id="2303",
    )

    devices = enumerate_devices(["option1"], sysfs_root=str(root), dev_root="/dev")

    assert [device.usb_path for device in devices] == ["3-1"]


def test_driver_list_is_read_on_every_call(tmp_path):
    """Изменение списка драйверов применяется на следующем цикле обнаружения."""
    root = fake_sysfs.make_root(tmp_path)
    fake_sysfs.add_device(root, usb_path="3-1", ports=["ttyUSB0"], driver="option1")
    fake_sysfs.add_device(
        root,
        usb_path="4-1",
        ports=["ttyUSB1"],
        driver="ch341-uart",
        vendor_id="1a86",
        product_id="7523",
    )

    first = enumerate_devices(["option1"], sysfs_root=str(root), dev_root="/dev")
    second = enumerate_devices(
        ["option1", "ch341-uart"], sysfs_root=str(root), dev_root="/dev"
    )

    assert [device.usb_path for device in first] == ["3-1"]
    assert [device.usb_path for device in second] == ["3-1", "4-1"]


def test_missing_driver_is_not_an_error(tmp_path):
    """Драйвер может быть не загружен -- это обычное состояние, а не сбой."""
    root = fake_sysfs.make_root(tmp_path)
    fake_sysfs.add_device(root, usb_path="3-1", ports=["ttyUSB0"], driver="option1")

    devices = enumerate_devices(
        ["option1", "cp210x"], sysfs_root=str(root), dev_root="/dev"
    )

    assert [device.usb_path for device in devices] == ["3-1"]


def test_service_files_are_not_taken_for_ports(tmp_path):
    root = fake_sysfs.make_root(tmp_path)
    fake_sysfs.add_device(root, usb_path="3-1", ports=["ttyUSB0"], driver="option1")

    devices = enumerate_devices(["option1"], sysfs_root=str(root), dev_root="/dev")

    assert devices[0].ports == ["/dev/ttyUSB0"]


def test_ports_are_sorted_naturally(tmp_path):
    """`ttyUSB2` идёт раньше `ttyUSB10`, иначе порядок портов перепутан."""
    root = fake_sysfs.make_root(tmp_path)
    fake_sysfs.add_device(
        root,
        usb_path="3-1",
        ports=["ttyUSB10", "ttyUSB2", "ttyUSB9"],
        driver="option1",
    )

    devices = enumerate_devices(["option1"], sysfs_root=str(root), dev_root="/dev")

    assert devices[0].ports == ["/dev/ttyUSB2", "/dev/ttyUSB9", "/dev/ttyUSB10"]


def test_empty_serial_does_not_hide_the_device(tmp_path):
    """У части модемов серийный номер пуст -- опознание не должно на него опираться."""
    root = fake_sysfs.make_root(tmp_path)
    fake_sysfs.add_device(root, usb_path="3-1", ports=["ttyUSB0"], driver="option1")

    device = enumerate_devices(["option1"], sysfs_root=str(root), dev_root="/dev")[0]

    assert device.serial == ""
    assert "3-1" in device.description
