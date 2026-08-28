"""Шина событий: порядок доставки и изоляция сбоев подписчиков."""

from __future__ import annotations

import asyncio

import pytest

from modemmanager.events import Event, EventBus, EventType


async def test_subscribers_are_called_in_priority_order():
    calls: list[str] = []
    bus = EventBus()
    bus.subscribe(lambda event: calls.append("notifier"), priority=30, name="notifier")
    bus.subscribe(lambda event: calls.append("metrics"), priority=20, name="metrics")
    bus.subscribe(lambda event: calls.append("event-log"), priority=0, name="event-log")

    await bus.publish(Event(type=EventType.SMS))

    assert calls == ["event-log", "metrics", "notifier"]


async def test_journal_receives_event_before_notifier():
    """Событие должно попасть на диск раньше попытки его доставить."""
    order: list[str] = []
    bus = EventBus()

    async def journal(event: Event) -> None:
        await asyncio.sleep(0)
        order.append("journal")

    async def notifier(event: Event) -> None:
        order.append("notifier")

    bus.subscribe(journal, priority=0, name="event-log")
    bus.subscribe(notifier, priority=30, name="notifier")

    await bus.publish(Event(type=EventType.SMS))

    assert order == ["journal", "notifier"]


async def test_failing_subscriber_does_not_stop_the_others():
    reached: list[str] = []
    bus = EventBus()

    async def broken(event: Event) -> None:
        raise RuntimeError("телеграм недоступен")

    bus.subscribe(broken, priority=10, name="broken")
    bus.subscribe(lambda event: reached.append("after"), priority=20, name="after")

    await bus.publish(Event(type=EventType.SMS))

    assert reached == ["after"]


async def test_sync_and_async_subscribers_both_supported():
    seen: list[str] = []
    bus = EventBus()

    async def async_handler(event: Event) -> None:
        seen.append("async:" + event.type)

    bus.subscribe(lambda event: seen.append("sync:" + event.type))
    bus.subscribe(async_handler)

    await bus.publish(Event(type=EventType.CALL))

    assert seen == ["sync:call", "async:call"]


async def test_cancellation_is_not_swallowed():
    bus = EventBus()

    async def cancelling(event: Event) -> None:
        raise asyncio.CancelledError

    bus.subscribe(cancelling)

    with pytest.raises(asyncio.CancelledError):
        await bus.publish(Event(type=EventType.SMS))


def test_dedup_key_prefers_hardware_identity():
    assert Event(type=EventType.MODEM_FAULT, imei="123", usb_path="3-1").dedup_key == (
        "modem_fault",
        "123",
    )
    # До чтения IMEI единственный доступный признак -- путь USB.
    assert Event(type=EventType.MODEM_UP, usb_path="3-1").dedup_key == ("modem_up", "3-1")
    assert Event(type=EventType.NO_SERVICE, imsi="8970").dedup_key == ("no_service", "8970")


def test_record_puts_mandatory_fields_first():
    record = Event(type=EventType.SMS, at=0.0, data={"text": "x"}).to_record()

    assert list(record)[:3] == ["at", "ts", "type"]


def test_data_cannot_shadow_identity_fields():
    """Запись журнала не должна врать о том, к какому модему она относится."""
    record = Event(
        type=EventType.SMS,
        at=0.0,
        imsi="real",
        data={"imsi": "spoofed", "type": "call", "text": "x"},
    ).to_record()

    assert record["imsi"] == "real"
    assert record["type"] == EventType.SMS
    assert record["text"] == "x"
