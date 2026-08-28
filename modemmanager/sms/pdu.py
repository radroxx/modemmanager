"""Разбор двоичного представления входящего сообщения (SMS-DELIVER).

Формат описан в 3GPP TS 23.040 (структура PDU) и TS 23.038 (кодировки). Здесь
только те поля, без которых нельзя собрать многочастное сообщение и правильно
показать его пользователю: отправитель, время получения, кодировка и служебный
заголовок пользовательских данных.

Разбор ошибок не проглатывает: если PDU не читается, ``PduError`` поднимается с
подробностями, а вызывающий пишет в журнал сырой ответ модема. Это лучше, чем
показать половину сообщения, притворяясь, что всё в порядке.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from . import gsm7


class PduError(ValueError):
    """PDU разобрать не удалось."""


class Encoding(str, Enum):
    """Кодировка полезной нагрузки, как её сообщил модем в TP-DCS."""

    GSM7 = "gsm7"
    UCS2 = "ucs2"
    EIGHT_BIT = "8bit"
    #: Прочие значения TP-DCS. Тексты извлечь нельзя, сырьё сохраняется как есть.
    RESERVED = "reserved"


#: Флаги октета TP-MTI/... в первом байте после SMSC.
_UDHI_FLAG = 0x40


@dataclass(frozen=True)
class Address:
    """Отправитель сообщения."""

    number: str
    ton: int = 0

    @property
    def international(self) -> bool:
        """Международный ли номер (TON = 001 -- international)."""
        return (self.ton >> 4) & 0x07 == 1

    @property
    def display(self) -> str:
        """Номер в удобном для человека виде."""
        if not self.number:
            return ""
        if self.international and not self.number.startswith("+"):
            return "+" + self.number
        return self.number


@dataclass(frozen=True)
class ConcatInfo:
    """Сведения о принадлежности сообщения к многочастному."""

    reference: int
    total: int
    seq: int
    #: ``True``, если ссылка передавалась 16-битной. Не влияет на разбор, но
    #: попадает в ключ группы сборки, чтобы 8- и 16-битные ссылки не пересекались.
    reference_16bit: bool = False


@dataclass(frozen=True)
class UserDataHeader:
    """Разобранный служебный заголовок пользовательских данных.

    ``length`` -- значение поля UDHL (без учёта самого байта UDHL); полная длина
    служебного заголовка в октетах равна ``length + 1``. ``concat`` присутствует,
    если сообщение является частью многочастного.
    """

    length: int
    concat: ConcatInfo | None = None
    #: Все ИЭ в исходном виде: код, длина, значение. Сохраняются на случай,
    #: если понадобится разбирать что-то ещё (например, порт приложения).
    elements: tuple[tuple[int, bytes], ...] = ()


@dataclass(frozen=True)
class Deliver:
    """Разобранное входящее сообщение."""

    sender: Address
    timestamp: datetime
    encoding: Encoding
    dcs: int
    #: Значение TP-UDL как есть -- сколько септетов (для GSM7) или октетов.
    tp_udl: int
    #: Служебный заголовок, если бит UDHI был выставлен.
    udh: UserDataHeader | None
    #: Канонические данные для сборки многочастного:
    #:
    #: * для GSM7 -- распакованные септеты (0..127) полезной части, без UDH и
    #:   выравнивающих битов;
    #: * для UCS2 и 8-bit -- октеты полезной части.
    #:
    #: Именно эти байты (в порядке ``seq``) склеиваются между частями и
    #: декодируются один раз (см. design.md, D7).
    payload: bytes
    #: Исходный текст PDU в hex -- то, что отдал модем. Сохраняется в журнале.
    raw: str

    @property
    def is_concatenated(self) -> bool:
        return self.udh is not None and self.udh.concat is not None

    @property
    def concat(self) -> ConcatInfo | None:
        return self.udh.concat if self.udh is not None else None

    def decode_payload(self) -> str:
        """Декодирует полезную часть, полагая её единственной частью сообщения."""
        return decode_payload(self.payload, self.encoding)


# --------------------------------------------------------------- декодирование текста

def decode_payload(payload: bytes, encoding: Encoding) -> str:
    """Переводит канонические данные в строку.

    Не пытается что-то додумать за протокол: если кодировка не поддерживается,
    возвращает пустую строку -- сырьё останется в журнале для повторного разбора.
    """
    if encoding is Encoding.GSM7:
        return gsm7.decode(payload)
    if encoding is Encoding.UCS2:
        try:
            return payload.decode("utf-16-be")
        except UnicodeDecodeError:
            return payload.decode("utf-16-be", errors="replace")
    if encoding is Encoding.EIGHT_BIT:
        try:
            return payload.decode("latin-1")
        except UnicodeDecodeError:  # pragma: no cover -- latin-1 никогда не бросает
            return ""
    return ""


# --------------------------------------------------------------- разбор

def parse_deliver(text: str) -> Deliver:
    """Разбирает hex-строку с PDU в структуру Deliver."""
    data = _hex_to_bytes(text)
    reader = _Reader(data)

    smsc_len = reader.byte()
    reader.skip(smsc_len)  # содержимое SMSC не влияет на текст сообщения

    flags = reader.byte()
    has_udh = bool(flags & _UDHI_FLAG)

    sender = _parse_address(reader)
    _pid = reader.byte()  # TP-PID не участвует в разборе текста, но должен быть прочитан
    dcs = reader.byte()
    scts = reader.take(7)
    timestamp = _parse_scts(scts)

    tp_udl = reader.byte()
    ud = reader.remainder()

    encoding = _encoding_from_dcs(dcs)
    udh, payload = _extract_payload(ud, tp_udl, encoding, has_udh)

    return Deliver(
        sender=sender,
        timestamp=timestamp,
        encoding=encoding,
        dcs=dcs,
        tp_udl=tp_udl,
        udh=udh,
        payload=payload,
        raw=text.strip().replace(" ", "").upper(),
    )


# --------------------------------------------------------------- вспомогательные

class _Reader:
    """Пошаговое чтение байтов PDU с явными границами."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def byte(self) -> int:
        if self._pos >= len(self._data):
            raise PduError("PDU обрывается в неожиданном месте")
        value = self._data[self._pos]
        self._pos += 1
        return value

    def take(self, count: int) -> bytes:
        if self._pos + count > len(self._data):
            raise PduError(f"PDU обрывается (требуется {count} октетов)")
        chunk = self._data[self._pos : self._pos + count]
        self._pos += count
        return chunk

    def skip(self, count: int) -> None:
        if count < 0:
            raise PduError("отрицательное поле длины в PDU")
        if self._pos + count > len(self._data):
            raise PduError("PDU обрывается в неожиданном месте")
        self._pos += count

    def remainder(self) -> bytes:
        chunk = self._data[self._pos :]
        self._pos = len(self._data)
        return chunk


def _hex_to_bytes(text: str) -> bytes:
    """Убирает пробелы, приводит к bytes; понимает и заглавные, и строчные."""
    cleaned = "".join(text.split())
    if not cleaned:
        raise PduError("пустой PDU")
    if len(cleaned) % 2:
        raise PduError(f"нечётное число hex-цифр ({len(cleaned)})")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise PduError(f"не hex: {exc}") from exc


def _parse_address(reader: _Reader) -> Address:
    """Разбирает поле TP-OA: длина в полу-октетах, тип, полу-октетные BCD."""
    length = reader.byte()
    if length == 0:
        return Address(number="", ton=0)
    ton = reader.byte()
    octets = (length + 1) // 2
    raw = reader.take(octets)
    if (ton >> 4) & 0x07 == 5:
        # Алфавитно-цифровой адрес: значение упаковано как GSM7 без UDH.
        septet_count = (len(raw) * 8) // 7
        septets = gsm7.unpack_septets(raw, septet_count)
        return Address(number=gsm7.decode(septets), ton=ton)
    digits = _semi_octet_digits(raw)
    return Address(number=digits[:length], ton=ton)


def _semi_octet_digits(data: bytes) -> str:
    """Разворачивает полу-октетные пары BCD в строку цифр.

    В PDU пары цифр хранятся с переставленными полу-октетами: ``72`` означает
    ``27``. Полу-октет ``0xF`` -- заполнитель на нечётной длине, отбрасывается.
    """
    out: list[str] = []
    for byte in data:
        low = byte & 0x0F
        high = (byte >> 4) & 0x0F
        for digit in (low, high):
            if digit == 0x0F:
                continue
            if digit > 9:
                # Нецифровой полу-октет: сохраняем в hex, чтобы был виден дефект.
                out.append(f"{digit:X}")
            else:
                out.append(str(digit))
    return "".join(out)


def _parse_scts(data: bytes) -> datetime:
    """Разбирает временную метку SCTS: 7 октетов, полу-октетные BCD.

    Часовой пояс -- знаковое число в четвертях часа. Знак хранится в 3-м бите
    старшего полу-октета последнего октета (после свапа -- в младшем полу-октете
    сырого байта). Восстанавливаем и знак, и величину.
    """
    if len(data) != 7:
        raise PduError(f"SCTS ожидает 7 октетов, получено {len(data)}")
    year = _bcd_pair(data[0])
    month = _bcd_pair(data[1])
    day = _bcd_pair(data[2])
    hour = _bcd_pair(data[3])
    minute = _bcd_pair(data[4])
    second = _bcd_pair(data[5])
    quarters, negative = _parse_timezone(data[6])
    offset = timedelta(minutes=15 * quarters)
    if negative:
        offset = -offset
    century = 2000 if year < 70 else 1900
    try:
        stamp = datetime(
            century + year,
            month,
            day,
            hour,
            minute,
            second,
            tzinfo=timezone(offset),
        )
    except ValueError as exc:
        raise PduError(f"метка времени SCTS некорректна: {exc}") from exc
    return stamp


def _bcd_pair(byte: int) -> int:
    low = byte & 0x0F
    high = (byte >> 4) & 0x0F
    if low > 9 or high > 9:
        raise PduError(f"полу-октет вне диапазона BCD: 0x{byte:02X}")
    return low * 10 + high


def _parse_timezone(byte: int) -> tuple[int, bool]:
    """Возвращает (число четвертей, отрицательный).

    Октет часового пояса хранит две BCD-цифры с полу-октетным свапом: низкий
    полу-октет -- десятки, высокий -- единицы. Знак -- бит 3 полу-октета
    десятков (после свапа он остаётся в младшем полу-октете) [3GPP TS 23.040
    §9.2.3.11].
    """
    tens_with_sign = byte & 0x0F
    ones = (byte >> 4) & 0x0F
    negative = bool(tens_with_sign & 0x08)
    tens = tens_with_sign & 0x07
    if ones > 9:
        raise PduError(f"часовой пояс не BCD: 0x{byte:02X}")
    return (tens * 10 + ones, negative)


# --------------------------------------------------------------- разбор UD

def _encoding_from_dcs(dcs: int) -> Encoding:
    """Отображение TP-DCS на кодировку, покрывающее случаи с реальных модемов."""
    if (dcs & 0xC0) == 0x00:
        group = (dcs >> 2) & 0x03
        if group == 0:
            return Encoding.GSM7
        if group == 1:
            return Encoding.EIGHT_BIT
        if group == 2:
            return Encoding.UCS2
        return Encoding.RESERVED
    if (dcs & 0xF0) == 0xF0:
        return Encoding.EIGHT_BIT if dcs & 0x04 else Encoding.GSM7
    return Encoding.RESERVED


def _extract_payload(
    ud: bytes, tp_udl: int, encoding: Encoding, has_udh: bool
) -> tuple[UserDataHeader | None, bytes]:
    """Отделяет служебный заголовок и подготавливает канонический payload."""
    udh: UserDataHeader | None = None
    header_octets = 0
    if has_udh:
        if not ud:
            raise PduError("флаг UDHI выставлен, но данных нет")
        udhl = ud[0]
        header_octets = udhl + 1
        if header_octets > len(ud):
            raise PduError(
                f"UDH выходит за пределы UD ({header_octets} октетов, доступно {len(ud)})"
            )
        udh = _parse_udh(udhl, ud[1:header_octets])

    tail = ud[header_octets:]
    if encoding is Encoding.GSM7:
        udh_septets = gsm7.septets_for_udh(udh.length) if udh else 0
        skip_bits = gsm7.fill_bits_after_udh(udh.length) if udh else 0
        text_septets = max(0, tp_udl - udh_septets)
        payload = gsm7.unpack_septets(tail, text_septets, skip_bits=skip_bits)
        return (udh, payload)

    # Для UCS2 и 8-bit TP-UDL считается в октетах и включает служебный заголовок.
    text_octets = max(0, tp_udl - header_octets)
    payload = bytes(tail[:text_octets])
    return (udh, payload)


def _parse_udh(length: int, data: bytes) -> UserDataHeader:
    """Разбирает поля служебного заголовка."""
    if length != len(data):
        raise PduError(f"UDHL={length} не соответствует длине данных {len(data)}")
    elements: list[tuple[int, bytes]] = []
    concat: ConcatInfo | None = None
    pos = 0
    while pos < len(data):
        if pos + 2 > len(data):
            raise PduError("незавершённый ИЭ в UDH")
        iei = data[pos]
        iedl = data[pos + 1]
        if pos + 2 + iedl > len(data):
            raise PduError(f"ИЭ 0x{iei:02X} обещает {iedl} октетов, а их нет")
        value = data[pos + 2 : pos + 2 + iedl]
        elements.append((iei, bytes(value)))
        if iei == 0x00 and iedl == 3:
            concat = ConcatInfo(
                reference=value[0],
                total=value[1],
                seq=value[2],
            )
        elif iei == 0x08 and iedl == 4:
            concat = ConcatInfo(
                reference=(value[0] << 8) | value[1],
                total=value[2],
                seq=value[3],
                reference_16bit=True,
            )
        pos += 2 + iedl
    return UserDataHeader(length=length, concat=concat, elements=tuple(elements))
