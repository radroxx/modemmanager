"""Кодировка GSM7: упаковка септетов, декодирование, таблица расширения."""

from __future__ import annotations

import pytest

from modemmanager.sms import gsm7


class TestPackAndUnpack:
    def test_hello_roundtrip_without_offset(self):
        septets = gsm7.encode("Hello")
        packed = gsm7.pack_septets(septets)
        assert gsm7.unpack_septets(packed, len(septets)) == septets

    def test_unpack_ignores_leading_bits(self):
        septets = gsm7.encode("Hi")
        packed = gsm7.pack_septets(septets, skip_bits=1)
        assert gsm7.unpack_septets(packed, len(septets), skip_bits=1) == septets

    def test_short_input_is_padded_with_zero_septets(self):
        """Обрезанный поток даёт нули на месте недостающих септетов."""
        packed = gsm7.pack_septets(gsm7.encode("A"))
        assert gsm7.unpack_septets(packed, 5)[:1] == gsm7.encode("A")

    def test_negative_count_is_rejected(self):
        with pytest.raises(ValueError):
            gsm7.unpack_septets(b"", -1)


class TestDecode:
    def test_hello(self):
        assert gsm7.decode(gsm7.encode("Hello")) == "Hello"

    def test_special_characters_from_base_table(self):
        for char in ("@", "£", "Ω", "Ñ"):
            assert gsm7.decode(gsm7.encode(char)) == char

    def test_extension_table_euro(self):
        """`€` кодируется парой ``ESC 0x65``."""
        assert gsm7.encode("€") == bytes([gsm7.ESCAPE, 0x65])
        assert gsm7.decode(gsm7.encode("€")) == "€"

    def test_extension_table_brackets(self):
        text = "[]{}\\^~|"
        assert gsm7.decode(gsm7.encode(text)) == text

    def test_dangling_escape_is_tolerated(self):
        """Одиночный ``ESC`` в конце потока не должен ломать декодирование."""
        assert gsm7.decode(bytes([gsm7.ESCAPE])) == "\x1b"


class TestUdhAlignment:
    def test_fill_bits_for_concat_8bit(self):
        """UDH из шести октетов даёт один бит выравнивания."""
        assert gsm7.fill_bits_after_udh(5) == 1
        assert gsm7.septets_for_udh(5) == 7

    def test_fill_bits_for_concat_16bit(self):
        """UDH из семи октетов делится на семь без остатка -- выравнивание не нужно."""
        assert gsm7.fill_bits_after_udh(6) == 0
        assert gsm7.septets_for_udh(6) == 8

    def test_decoding_with_offset_gives_correct_text(self):
        """Полная симуляция пути: пакуем со сдвигом -- распаковываем -- декодируем."""
        septets = gsm7.encode("PART TWO")
        packed = gsm7.pack_septets(septets, skip_bits=1)
        unpacked = gsm7.unpack_septets(packed, len(septets), skip_bits=1)
        assert gsm7.decode(unpacked) == "PART TWO"

    def test_extension_split_across_boundary_reassembles(self):
        """Разделение ``€`` границей: сборка септетов возвращает символ.

        В первой части остаётся ``ESC``, во второй -- код ``0x65``. Декодирование
        каждой части по отдельности даст сломанный результат; декодирование
        объединённых септетов даёт ``€``.
        """
        part1_septets = gsm7.encode("A") + bytes([gsm7.ESCAPE])
        part2_septets = bytes([0x65]) + gsm7.encode("B")
        combined = part1_septets + part2_septets
        assert gsm7.decode(combined) == "A€B"
        # Отдельное декодирование даёт другой результат -- ради контраста.
        assert gsm7.decode(part1_septets) != "A€"
