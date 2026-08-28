"""Помощник для сборки PDU в тестах.

Приложение отправлять сообщения не умеет и не будет; собирать PDU нужно в
тестах, чтобы проверить разбор и сборку многочастных без реального модема.
Функции этого модуля зеркалят разбор ``modemmanager.sms.pdu`` -- ошибки одного
не спрячутся ошибками другого.
"""

from __future__ import annotations

from modemmanager.sms import gsm7


UDHI_FLAG = 0x40
DELIVER_FLAGS = 0x04


def encode_address(number: str, ton: int = 0x91) -> bytes:
    """Формирует поле TP-OA."""
    digits = "".join(ch for ch in number if ch.isdigit())
    length = len(digits)
    padded = digits + ("F" if length % 2 else "")
    octets = bytearray()
    for i in range(0, len(padded), 2):
        # Полу-октетный свап: пара цифр AB хранится как байт BA.
        pair = padded[i : i + 2]
        octets.append((int(pair[1], 16) << 4) | int(pair[0], 16))
    return bytes([length, ton]) + bytes(octets)


def encode_scts(
    year: int = 2,
    month: int = 1,
    day: int = 1,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    tz_quarters: int = 0,
    tz_negative: bool = False,
) -> bytes:
    """Формирует поле TP-SCTS: 7 октетов, полу-октетные BCD."""

    def swapped(value: int) -> int:
        tens = value // 10
        ones = value % 10
        return (ones << 4) | tens

    tens = tz_quarters // 10
    ones = tz_quarters % 10
    if tz_negative:
        tens |= 0x08
    tz_byte = (ones << 4) | tens
    return bytes(
        [
            swapped(year % 100),
            swapped(month),
            swapped(day),
            swapped(hour),
            swapped(minute),
            swapped(second),
            tz_byte,
        ]
    )


def concat_8bit(reference: int, total: int, seq: int) -> bytes:
    """Служебный ИЭ конкатенации с 8-битной ссылкой (IEI=0x00)."""
    return bytes([0x00, 0x03, reference & 0xFF, total, seq])


def concat_16bit(reference: int, total: int, seq: int) -> bytes:
    """Служебный ИЭ конкатенации с 16-битной ссылкой (IEI=0x08)."""
    return bytes([0x08, 0x04, (reference >> 8) & 0xFF, reference & 0xFF, total, seq])


def build_udh(*elements: bytes) -> bytes:
    """Собирает UDH: сначала UDHL, потом ИЭ друг за другом."""
    body = b"".join(elements)
    return bytes([len(body)]) + body


def build_deliver(
    *,
    sender: str,
    text: str | None = None,
    payload: bytes | None = None,
    encoding: str = "gsm7",
    udh: bytes = b"",
    scts: bytes | None = None,
    ton: int = 0x91,
    smsc: bytes = b"\x00",
) -> str:
    """Собирает PDU SMS-DELIVER в виде hex-строки.

    ``text`` кодируется в выбранной кодировке; ``payload`` позволяет задать
    сырые данные напрямую (например, чтобы построить многочастную часть с
    заранее известным содержимым).
    """
    if scts is None:
        scts = encode_scts(year=25, month=1, day=1, hour=12, minute=0, second=0, tz_quarters=12)
    flags = DELIVER_FLAGS | (UDHI_FLAG if udh else 0)
    address = encode_address(sender, ton=ton)
    pid = 0x00

    if encoding == "gsm7":
        dcs = 0x00
        text_septets = gsm7.encode(text) if text is not None else (payload or b"")
        if udh:
            fill = gsm7.fill_bits_after_udh(len(udh) - 1)
            udh_septets = gsm7.septets_for_udh(len(udh) - 1)
            packed_body = gsm7.pack_septets(text_septets, skip_bits=fill)
            udl = udh_septets + len(text_septets)
            ud = udh + packed_body
        else:
            packed_body = gsm7.pack_septets(text_septets)
            udl = len(text_septets)
            ud = packed_body
    elif encoding == "ucs2":
        dcs = 0x08
        if text is not None:
            body = text.encode("utf-16-be")
        else:
            body = payload or b""
        udl = (len(udh) + len(body))
        ud = udh + body
    elif encoding == "8bit":
        dcs = 0x04
        body = payload or (text.encode("latin-1") if text is not None else b"")
        udl = (len(udh) + len(body))
        ud = udh + body
    else:
        raise ValueError(f"неизвестная кодировка теста: {encoding}")

    parts = [
        smsc,
        bytes([flags]),
        address,
        bytes([pid, dcs]),
        scts,
        bytes([udl]),
        ud,
    ]
    return b"".join(parts).hex().upper()
