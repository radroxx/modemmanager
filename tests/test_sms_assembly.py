"""Сборка многочастных сообщений."""

from __future__ import annotations

from pdu_builder import (
    build_deliver,
    build_udh,
    concat_8bit,
    concat_16bit,
)

from modemmanager.sms import Encoding, Assembler, GroupKey, parse_deliver
from modemmanager.sms import gsm7


IMSI = "89701020123456789042"
SENDER = "79990001122"


class Clock:
    """Управляемое время: позволяет прогонять таймауты явно."""

    def __init__(self, start: float = 0.0):
        self.value = start

    def advance(self, seconds: float) -> None:
        self.value += seconds

    def __call__(self) -> float:
        return self.value


def _assembler(timeout: float = 600.0) -> tuple[Assembler, Clock, Clock]:
    clock = Clock()
    wall = Clock(1_700_000_000.0)
    return (Assembler(timeout=timeout, clock=clock, wall_clock=wall), clock, wall)


def _part(seq: int, total: int, text: str, ref: int = 42, *, encoding: str = "gsm7"):
    """Собирает часть многочастного сообщения с 8-битной ссылкой."""
    udh = build_udh(concat_8bit(reference=ref, total=total, seq=seq))
    hex_pdu = build_deliver(sender=SENDER, text=text, encoding=encoding, udh=udh)
    return parse_deliver(hex_pdu)


# ---------------------------------------------------- 6.5 сборка по seq

class TestConcatenation:
    def test_single_part_message_is_delivered_immediately(self):
        assembler, _clock, _wall = _assembler()
        hex_pdu = build_deliver(sender=SENDER, text="hello")
        result = assembler.add(IMSI, parse_deliver(hex_pdu))
        assert result is not None
        assert result.complete
        assert result.text == "hello"
        assert result.key.total == 1

    def test_parts_arriving_in_order_assemble_to_full_text(self):
        assembler, _, _ = _assembler()
        assert assembler.add(IMSI, _part(1, 2, "PART ONE")) is None
        result = assembler.add(IMSI, _part(2, 2, "PART TWO"))
        assert result is not None
        assert result.complete
        assert result.text == "PART ONEPART TWO"

    def test_parts_arriving_out_of_order_assemble_in_seq_order(self):
        assembler, _, _ = _assembler()
        assert assembler.add(IMSI, _part(2, 2, "PART TWO")) is None
        result = assembler.add(IMSI, _part(1, 2, "PART ONE"))
        assert result is not None
        assert result.text == "PART ONEPART TWO"

    def test_extension_character_split_across_boundary_reassembles(self):
        """Символ ``€`` (ESC 0x65) разрезан границей: сборка возвращает символ."""
        assembler, _, _ = _assembler()
        # Часть 1: "A" плюс висячий ESC. Строим сырьё вручную, чтобы контролировать
        # что именно попадёт в payload после распаковки.
        part1_septets = gsm7.encode("A") + bytes([gsm7.ESCAPE])
        part2_septets = bytes([0x65]) + gsm7.encode("B")

        # Собираем PDU каждой части, но задаём payload напрямую в hex:
        # для этого пакуем септеты с учётом сдвига UDH.
        def _build(seq: int, septets: bytes) -> str:
            udh = build_udh(concat_8bit(reference=7, total=2, seq=seq))
            fill = gsm7.fill_bits_after_udh(len(udh) - 1)
            packed = gsm7.pack_septets(septets, skip_bits=fill)
            udh_septets = gsm7.septets_for_udh(len(udh) - 1)
            udl = udh_septets + len(septets)
            # Собираем PDU через build_deliver с сырым payload:
            return build_deliver(
                sender=SENDER,
                payload=septets,
                encoding="gsm7",
                udh=udh,
            )

        part1 = parse_deliver(_build(1, part1_septets))
        part2 = parse_deliver(_build(2, part2_septets))
        assert assembler.add(IMSI, part1) is None
        result = assembler.add(IMSI, part2)
        assert result is not None
        assert result.text == "A€B"

    def test_ucs2_bmp_character_split_across_boundary(self):
        """Символ UCS2 разрезан по одному октету между частями."""
        assembler, _, _ = _assembler()
        # 'П' -> 04 1F, 'р' -> 04 40. Часть 1: 04, часть 2: 1F 04 40.
        part1 = parse_deliver(
            build_deliver(
                sender=SENDER,
                payload=bytes.fromhex("04"),
                encoding="ucs2",
                udh=build_udh(concat_8bit(reference=8, total=2, seq=1)),
            )
        )
        part2 = parse_deliver(
            build_deliver(
                sender=SENDER,
                payload=bytes.fromhex("1F0440"),
                encoding="ucs2",
                udh=build_udh(concat_8bit(reference=8, total=2, seq=2)),
            )
        )
        assert assembler.add(IMSI, part1) is None
        result = assembler.add(IMSI, part2)
        assert result is not None
        assert result.text == "Пр"

    def test_ucs2_surrogate_pair_split_across_boundary(self):
        """UTF-16 суррогатная пара с разрывом между октетами кода."""
        assembler, _, _ = _assembler()
        emoji = "😀"  # D83D DE00 (4 октета в UTF-16 BE)
        raw = emoji.encode("utf-16-be")
        # Разрываем ровно между двумя половинами суррогата: 2 + 2.
        part1_bytes = raw[:2]
        part2_bytes = raw[2:]
        part1 = parse_deliver(
            build_deliver(
                sender=SENDER,
                payload=part1_bytes,
                encoding="ucs2",
                udh=build_udh(concat_8bit(reference=9, total=2, seq=1)),
            )
        )
        part2 = parse_deliver(
            build_deliver(
                sender=SENDER,
                payload=part2_bytes,
                encoding="ucs2",
                udh=build_udh(concat_8bit(reference=9, total=2, seq=2)),
            )
        )
        assert assembler.add(IMSI, part1) is None
        result = assembler.add(IMSI, part2)
        assert result is not None
        assert result.text == emoji

    def test_duplicate_part_is_ignored(self):
        assembler, _, _ = _assembler()
        assert assembler.add(IMSI, _part(1, 2, "hello ")) is None
        # Повтор пришёл: не добавляется. Второй экземпляр должен быть проигнорирован.
        assert assembler.add(IMSI, _part(1, 2, "hello ", ref=42)) is None
        # Всё ещё ждём часть 2:
        result = assembler.add(IMSI, _part(2, 2, "world"))
        assert result is not None
        assert result.text == "hello world"


# ------------------------------------------- 6.6 ключ группы и переиспользование

class TestGroupKey:
    def test_reference_reuse_after_timeout_starts_new_group(self):
        assembler, clock, _ = _assembler(timeout=10.0)
        assembler.add(IMSI, _part(1, 2, "AAA"))  # первая часть первого сообщения

        clock.advance(11.0)  # прошло больше времени ожидания

        # Прошёл срок; должно появиться в expire.
        expired = assembler.expire()
        assert len(expired) == 1
        assert expired[0].incomplete
        assert expired[0].parts_missing == (2,)

        # Теперь тот же ref, тот же отправитель, тот же total -- это другое
        # сообщение и не смешивается с прошлым.
        assembler.add(IMSI, _part(1, 2, "BBB"))
        result = assembler.add(IMSI, _part(2, 2, "CCC"))
        assert result is not None
        assert result.text == "BBBCCC"

    def test_different_total_makes_different_group(self):
        assembler, _, _ = _assembler()
        assembler.add(IMSI, _part(1, 2, "AAA", ref=1))
        # Другой total -> другая группа: 2/3 не соберётся с 1/2.
        assert assembler.add(IMSI, _part(2, 3, "BBB", ref=1)) is None
        # Плюс третья часть тоже отдельная группа.
        assert assembler.add(IMSI, _part(3, 3, "CCC", ref=1)) is None
        assert len(assembler.pending_keys()) == 2

    def test_16bit_and_8bit_references_are_kept_separate(self):
        assembler, _, _ = _assembler()
        udh_8 = build_udh(concat_8bit(reference=5, total=2, seq=1))
        udh_16 = build_udh(concat_16bit(reference=5, total=2, seq=1))
        assembler.add(
            IMSI, parse_deliver(build_deliver(sender=SENDER, text="A", udh=udh_8))
        )
        assembler.add(
            IMSI,
            parse_deliver(build_deliver(sender=SENDER, text="B", encoding="ucs2", udh=udh_16)),
        )
        keys = assembler.pending_keys()
        assert len(keys) == 2
        assert any(key.reference_16bit for key in keys)
        assert any(not key.reference_16bit for key in keys)


# ------------------------------------------------ 6.7 неполное по таймауту

class TestExpiry:
    def test_missing_parts_are_reported_after_timeout(self):
        assembler, clock, _ = _assembler(timeout=5.0)
        # Пришли только 1 и 3 из четырёх; 2 и 4 пропали.
        assembler.add(IMSI, _part(1, 4, "one", ref=99))
        assembler.add(IMSI, _part(3, 4, "three", ref=99))

        clock.advance(6.0)
        expired = assembler.expire()

        assert len(expired) == 1
        item = expired[0]
        assert item.incomplete
        assert item.parts_present == (1, 3)
        assert item.parts_missing == (2, 4)
        # Текст содержит только имеющиеся части, склеенные в порядке seq.
        assert item.text == "onethree"

    def test_expire_leaves_running_groups_untouched(self):
        assembler, clock, _ = _assembler(timeout=100.0)
        assembler.add(IMSI, _part(1, 2, "A"))
        clock.advance(50.0)
        assert assembler.expire() == []
        # Группа всё ещё в очереди.
        assert len(assembler.pending_keys()) == 1


# ------------------------------------------------ 6.8 сырьё частей в результате

class TestRawInAssembled:
    def test_multipart_result_contains_raw_pdu_of_every_part(self):
        assembler, _, _ = _assembler()
        p1 = _part(1, 2, "hello ")
        p2 = _part(2, 2, "world")
        assembler.add(IMSI, p1, raw=p1.raw)
        result = assembler.add(IMSI, p2, raw=p2.raw)
        assert result is not None
        assert result.raw_pdus == (p1.raw, p2.raw)

    def test_single_part_result_carries_raw_pdu(self):
        assembler, _, _ = _assembler()
        pdu = parse_deliver(build_deliver(sender=SENDER, text="one"))
        result = assembler.add(IMSI, pdu)
        assert result is not None
        assert result.raw_pdus == (pdu.raw,)

    def test_text_can_be_reparsed_from_raw_pdus_in_the_event(self):
        """Записав сырьё каждой части, текст можно получить повторно.

        Это то, что записывается в журнал (см. spec 6.8): даже если разбор в
        какой-то версии окажется неточен, по сохранённому сырью текст можно
        восстановить полностью.
        """
        assembler, _, _ = _assembler()
        assembler.add(IMSI, _part(1, 2, "before "))
        result = assembler.add(IMSI, _part(2, 2, "after"))
        assert result is not None
        # Реплей: разбираем каждое сырьё снова, собираем через новый Assembler.
        replay = Assembler(timeout=result.key.total * 60)
        for raw in result.raw_pdus:
            replay.add(IMSI, parse_deliver(raw))
        # Последний ``add`` вернул готовое сообщение с тем же текстом.
        replay_result = None
        replay2 = Assembler(timeout=60.0)
        for raw in result.raw_pdus:
            replay_result = replay2.add(IMSI, parse_deliver(raw))
        assert replay_result is not None
        assert replay_result.text == result.text
