"""Разбор двоичного представления входящего сообщения (SMS-DELIVER)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pdu_builder import (
    build_deliver,
    build_udh,
    concat_8bit,
    concat_16bit,
    encode_scts,
)

from modemmanager.sms import Encoding, PduError, parse_deliver
from modemmanager.sms.pdu import decode_payload


class TestGsm7Deliver:
    def test_short_message_from_international_number(self):
        hex_pdu = build_deliver(
            sender="79990001122",
            text="Hello",
            scts=encode_scts(
                year=25, month=1, day=6, hour=14, minute=30, second=0, tz_quarters=12
            ),
        )
        deliver = parse_deliver(hex_pdu)
        assert deliver.encoding is Encoding.GSM7
        assert deliver.sender.number == "79990001122"
        assert deliver.sender.display == "+79990001122"
        assert deliver.decode_payload() == "Hello"
        assert deliver.timestamp == datetime(
            2025, 1, 6, 14, 30, 0, tzinfo=timezone(timedelta(hours=3))
        )
        assert deliver.udh is None
        assert deliver.is_concatenated is False
        assert deliver.raw == hex_pdu

    def test_extension_character_survives_round_trip(self):
        hex_pdu = build_deliver(sender="79990001122", text="€uro")
        deliver = parse_deliver(hex_pdu)
        assert deliver.decode_payload() == "€uro"

    def test_negative_timezone_is_recognised(self):
        hex_pdu = build_deliver(
            sender="19990001122",
            text="hi",
            scts=encode_scts(year=25, tz_quarters=20, tz_negative=True),
        )
        deliver = parse_deliver(hex_pdu)
        assert deliver.timestamp.utcoffset() == -timedelta(minutes=15 * 20)


class TestUcs2Deliver:
    def test_cyrillic_short_message(self):
        hex_pdu = build_deliver(sender="79990001122", text="Привет", encoding="ucs2")
        deliver = parse_deliver(hex_pdu)
        assert deliver.encoding is Encoding.UCS2
        assert deliver.decode_payload() == "Привет"

    def test_odd_length_payload_is_preserved_as_octets(self):
        """UCS2 с нечётным TP-UDL сохраняется как поток октетов до объединения."""
        payload = bytes.fromhex("041F")  # половина символа 'Пр' -- один октет
        hex_pdu = build_deliver(
            sender="79990001122", payload=payload, encoding="ucs2"
        )
        deliver = parse_deliver(hex_pdu)
        assert deliver.payload == payload


class TestConcatUdh:
    def test_8bit_concat_reference(self):
        udh = build_udh(concat_8bit(reference=0x2A, total=3, seq=2))
        hex_pdu = build_deliver(sender="79990001122", text="TWO", udh=udh)
        deliver = parse_deliver(hex_pdu)
        assert deliver.udh is not None
        concat = deliver.udh.concat
        assert concat is not None
        assert concat.reference == 0x2A
        assert concat.total == 3
        assert concat.seq == 2
        assert concat.reference_16bit is False

    def test_16bit_concat_reference(self):
        udh = build_udh(concat_16bit(reference=0xABCD, total=2, seq=1))
        hex_pdu = build_deliver(
            sender="79990001122", text="X", encoding="ucs2", udh=udh
        )
        deliver = parse_deliver(hex_pdu)
        concat = deliver.udh.concat if deliver.udh else None
        assert concat is not None
        assert concat.reference == 0xABCD
        assert concat.reference_16bit is True

    def test_gsm7_payload_skips_udh_alignment(self):
        """С UDH из шести октетов первый септет начинается с бита 49."""
        udh = build_udh(concat_8bit(reference=1, total=2, seq=1))
        hex_pdu = build_deliver(sender="79990001122", text="ABC", udh=udh)
        deliver = parse_deliver(hex_pdu)
        # payload -- уже распакованные септеты, без UDH и битов выравнивания.
        assert decode_payload(deliver.payload, Encoding.GSM7) == "ABC"


class TestErrors:
    def test_truncated_pdu_raises(self):
        with pytest.raises(PduError):
            parse_deliver("00")

    def test_odd_hex_length_raises(self):
        with pytest.raises(PduError):
            parse_deliver("0")

    def test_non_hex_raises(self):
        with pytest.raises(PduError):
            parse_deliver("XY")

    def test_broken_udh_length_raises(self):
        udh = build_udh(bytes([0x00, 0x03, 0x01, 0x02]))  # обещает 3 октета, а их два
        hex_pdu = build_deliver(sender="79990001122", text="A", udh=udh)
        # Сломаем UDHL руками: подменим первый байт UD.
        broken = list(bytes.fromhex(hex_pdu))
        # Найдём позицию UDHL: после SMSC(1) + flags(1) + addr_length(1) + toa(1) +
        # (addr_octets = ceil(11/2) = 6) + pid(1) + dcs(1) + scts(7) + udl(1) = 20.
        broken[20] = 0xFF  # заведомо неверная длина UDHL
        with pytest.raises(PduError):
            parse_deliver(bytes(broken).hex())
