"""Журнал событий: дополнение, чтение с конца, устойчивость к повреждениям."""

from __future__ import annotations

import json

from modemmanager.eventlog import TAIL_BLOCK, EventLog
from modemmanager.events import Event, EventType


async def test_append_does_not_touch_previous_lines(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    await log.append(Event(type=EventType.SMS, at=1.0, imsi="a", data={"text": "one"}))
    first_snapshot = (tmp_path / "events.jsonl").read_text(encoding="utf-8")

    await log.append(Event(type=EventType.SMS, at=2.0, imsi="b", data={"text": "two"}))
    content = (tmp_path / "events.jsonl").read_text(encoding="utf-8")

    assert content.startswith(first_snapshot)
    assert len(content.splitlines()) == 2


async def test_missing_file_is_created(tmp_path):
    path = tmp_path / "sub" / "events.jsonl"
    log = EventLog(path)
    log.ensure_file()

    assert path.exists()
    assert await log.tail(10) == []


async def test_record_carries_mandatory_fields(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    await log.append(
        Event(
            type=EventType.SMS,
            at=1_700_000_000.0,
            imsi="8970123",
            sim_label="Роуминг",
            imei="123456789012345",
            data={"from": "+79990001122", "text": "привет"},
        )
    )

    record = json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8"))

    assert record["type"] == EventType.SMS
    assert record["ts"] == 1_700_000_000.0
    assert record["at"].endswith("Z")
    assert record["imsi"] == "8970123"
    assert record["sim_label"] == "Роуминг"
    assert record["imei"] == "123456789012345"
    assert record["text"] == "привет"


async def test_record_without_sim_omits_imsi(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    await log.append(
        Event(type=EventType.MODEM_GONE, usb_path="3-1", data={"reason": "unplugged"})
    )

    record = json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8"))

    assert "imsi" not in record
    assert record["usb_path"] == "3-1"
    assert record["type"] == EventType.MODEM_GONE


async def test_tail_returns_newest_first(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    for index in range(10):
        await log.append(Event(type=EventType.SMS, at=float(index), data={"n": index}))

    records = await log.tail(3)

    assert [record["n"] for record in records] == [9, 8, 7]


async def test_tail_reads_only_end_of_large_file(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    # Файл заметно больше одного блока чтения.
    with open(path, "w", encoding="utf-8") as handle:
        for index in range(200_000):
            handle.write(json.dumps({"type": "sms", "ts": index, "n": index}) + "\n")
    size = path.stat().st_size
    assert size > 20 * TAIL_BLOCK

    records = await log.tail(5)

    assert [record["n"] for record in records] == [199_999, 199_998, 199_997, 199_996, 199_995]
    assert log.last_tail_bytes <= TAIL_BLOCK
    assert log.last_tail_bytes < size / 10


async def test_tail_skips_corrupted_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    await log.append(Event(type=EventType.SMS, at=1.0, data={"n": 1}))
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{это не json\n")
        handle.write("\n")
    await log.append(Event(type=EventType.SMS, at=2.0, data={"n": 2}))

    records = await log.tail(10)

    assert [record["n"] for record in records] == [2, 1]


async def test_tail_filters_by_imsi(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    for index in range(20):
        await log.append(
            Event(
                type=EventType.SMS,
                at=float(index),
                imsi="a" if index % 2 == 0 else "b",
                data={"n": index},
            )
        )

    records = await log.tail(3, imsi="b")

    assert [record["n"] for record in records] == [19, 17, 15]
    assert all(record["imsi"] == "b" for record in records)


async def test_tail_filters_by_type(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    await log.append(Event(type=EventType.SMS, at=1.0, data={"n": 1}))
    await log.append(Event(type=EventType.CALL, at=2.0, data={"n": 2}))
    await log.append(Event(type=EventType.MODEM_GONE, at=3.0, data={"n": 3}))

    records = await log.tail(10, types=[EventType.SMS, EventType.CALL])

    assert [record["n"] for record in records] == [2, 1]


async def test_tail_returns_everything_when_fewer_than_requested(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    await log.append(Event(type=EventType.SMS, at=1.0, data={"n": 1}))
    await log.append(Event(type=EventType.SMS, at=2.0, data={"n": 2}))

    records = await log.tail(50)

    assert len(records) == 2


async def test_tail_crossing_block_boundary(tmp_path):
    """Запись, разрезанная границей блока чтения, собирается корректно."""
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    filler = "x" * 500
    with open(path, "w", encoding="utf-8") as handle:
        for index in range(1000):
            handle.write(json.dumps({"type": "sms", "n": index, "pad": filler}) + "\n")
    assert path.stat().st_size > 2 * TAIL_BLOCK

    records = await log.tail(400)

    assert len(records) == 400
    assert [record["n"] for record in records] == list(range(999, 599, -1))
    assert all(record["pad"] == filler for record in records)


async def test_tail_limit_zero(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    await log.append(Event(type=EventType.SMS, at=1.0, data={"n": 1}))

    assert await log.tail(0) == []
