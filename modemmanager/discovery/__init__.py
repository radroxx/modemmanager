"""Обнаружение модемов: перечисление портов, проба, цикл сверки."""

from .sysfs import UsbDevice, enumerate_devices

__all__ = ["UsbDevice", "enumerate_devices"]
