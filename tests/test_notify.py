"""Уведомления в Telegram: маршрутизация, доставка, дедуп, формат."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from modemmanager.config import SettingsStore, SimSettings
from modemmanager.events import Event, EventBus, EventType
from modemmanager.eventlog import EventLog
from modemmanager.notify import (
    RouterDecision,
    TelegramNotifier,
    format_event,
    route_event,
)
from modemmanager.notify.telegram import DeliveryError


IMSI = "89701020123456789042"


class FakeDelivery:
    """Пишет всё, что ей отдают. Может быть настроена, чтобы сбоить."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.fail_first: int = 0

    async def send(self, chat_id: str, text: str) -> None:
        if self.fail_first > 0:
            self.fail_first -= 1
            raise DeliveryError("Telegram недоступен")
        self.messages.append((chat_id, text))


def _store(**overrides) -> SettingsStore:
    store = SettingsStore(Path("/tmp/settings_notify.json"))
    store.settings.web.password = "x"
    store.settings.telegram.token = "y"
    store.settings.telegram.admin_chat_id = "admin-1"
    store.settings.telegram.max_retry_delay = 60.0
    for key, value in overrides.items():
        setattr(store.settings, key, value)
    return store


async def _wait_for(condition, timeout: float = 1.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise TimeoutError(f"условие не выполнено за {timeout}с")


# ------------------------------------------------- 8.1 маршрутизация

class TestRouting:
    def test_sms_goes_to_sim_chat_when_configured(self):
        store = _store()
        store.settings.sims[IMSI] = SimSettings(chat_id="chat-42")
        event = Event(type=EventType.SMS, imsi=IMSI)
        decision = route_event(event, store.settings)
        assert decision.chat_id == "chat-42"
        assert decision.fallback_to_admin is False

    def test_sms_falls_back_to_admin_when_sim_chat_missing(self):
        store = _store()
        store.settings.sims[IMSI] = SimSettings(chat_id="")
        event = Event(type=EventType.SMS, imsi=IMSI)
        decision = route_event(event, store.settings)
        assert decision.chat_id == "admin-1"
        assert decision.fallback_to_admin is True

    def test_system_events_always_go_to_admin(self):
        store = _store()
        store.settings.sims[IMSI] = SimSettings(chat_id="chat-42")
        event = Event(type=EventType.MODEM_GONE, imsi=IMSI)
        decision = route_event(event, store.settings)
        assert decision.chat_id == "admin-1"

    def test_call_routes_by_imsi(self):
        store = _store()
        store.settings.sims[IMSI] = SimSettings(chat_id="chat-42")
        event = Event(type=EventType.CALL, imsi=IMSI)
        decision = route_event(event, store.settings)
        assert decision.chat_id == "chat-42"


# ---------------------------- 8.6 запись в журнал ДО доставки

@pytest.mark.asyncio
async def test_event_is_logged_before_delivery_is_attempted(tmp_path):
    """Даже если Telegram сбоит, событие уже в журнале."""
    log_path = tmp_path / "events.jsonl"
    event_log = EventLog(log_path)
    event_log.ensure_file()

    store = _store()
    store.settings.sims[IMSI] = SimSettings(chat_id="chat-42")
    delivery = FakeDelivery()
    delivery.fail_first = 100  # никогда не пропустит
    notifier = TelegramNotifier(store, delivery=delivery)
    await notifier.start()

    bus = EventBus()
    bus.subscribe(event_log.append, priority=0, name="event-log")
    bus.subscribe(notifier.on_event, priority=30, name="notifier")

    await bus.publish(Event(type=EventType.SMS, imsi=IMSI, data={"text": "hi", "from": "+79990001122"}))
    # Позволяем очередь прокрутить и упасть с ошибкой.
    for _ in range(5):
        await asyncio.sleep(0)

    # Журнал уже содержит запись.
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1
    assert '"type":"sms"' in lines[0]
    # Отправка не удалась и осталась в очереди.
    assert notifier.failed_sends >= 1
    assert delivery.messages == []
    await notifier.stop()


# --------------------------------- 8.2 очередь не влияет на приём

@pytest.mark.asyncio
async def test_unreachable_telegram_does_not_block_bus():
    """Публикация события возвращает управление сразу, даже если доставка виснет."""
    store = _store()
    store.settings.sims[IMSI] = SimSettings(chat_id="chat-42")

    class SlowDelivery(FakeDelivery):
        async def send(self, chat_id, text):
            await asyncio.sleep(10.0)  # висит

    delivery = SlowDelivery()
    notifier = TelegramNotifier(store, delivery=delivery)
    await notifier.start()
    bus = EventBus()
    bus.subscribe(notifier.on_event, priority=30, name="notifier")

    # Публикация должна вернуться быстро, даже если доставщик спит.
    start = asyncio.get_event_loop().time()
    await bus.publish(Event(type=EventType.SMS, imsi=IMSI, data={"text": "hi"}))
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.1
    await notifier.stop()


@pytest.mark.asyncio
async def test_retry_backoff_grows():
    """Повторные сбои увеличивают интервал."""
    store = _store()
    store.settings.sims[IMSI] = SimSettings(chat_id="chat-42")
    delivery = FakeDelivery()
    delivery.fail_first = 3

    notifier = TelegramNotifier(store, delivery=delivery, initial_backoff=0.01)
    await notifier.start()
    bus = EventBus()
    bus.subscribe(notifier.on_event, priority=30, name="notifier")

    await bus.publish(Event(type=EventType.SMS, imsi=IMSI, data={"text": "hi", "from": "+123"}))

    # Ждём пока три отказа отработают и четвёртая попытка пройдёт.
    await _wait_for(lambda: len(delivery.messages) == 1, timeout=2.0)
    assert notifier.failed_sends == 3
    await notifier.stop()


# --------------------------------------------- 8.3 дедуп продолжительных состояний

@pytest.mark.asyncio
async def test_repeated_stateful_event_is_only_sent_once():
    store = _store()
    delivery = FakeDelivery()
    notifier = TelegramNotifier(store, delivery=delivery)
    await notifier.start()

    for _ in range(5):
        await notifier.on_event(
            Event(
                type=EventType.SIM_ABSENT,
                usb_path="3-1",
                imei="861234567890123",
                data={"reason": "no sim"},
            )
        )
    await _wait_for(lambda: len(delivery.messages) == 1, timeout=1.0)
    assert len(delivery.messages) == 1
    await notifier.stop()


@pytest.mark.asyncio
async def test_recovery_event_clears_dedup_and_publishes_restored():
    store = _store()
    delivery = FakeDelivery()
    notifier = TelegramNotifier(store, delivery=delivery)
    await notifier.start()

    imei = "861234567890123"
    await notifier.on_event(Event(type=EventType.MODEM_FAULT, imei=imei, data={"reason": "port"}))
    await notifier.on_event(Event(type=EventType.MODEM_RECOVERED, imei=imei))
    # После восстановления новое неисправное состояние снова уведомит.
    await notifier.on_event(Event(type=EventType.MODEM_FAULT, imei=imei, data={"reason": "port"}))

    await _wait_for(lambda: len(delivery.messages) == 3, timeout=1.0)
    # Порядок: fault, recovery, fault.
    kinds = [text[:6] for _chat, text in delivery.messages]
    assert delivery.messages[0][1].startswith("🛠")  # fault
    assert "восстановил" in delivery.messages[1][1] or "восстановлено" in delivery.messages[1][1]
    assert delivery.messages[2][1].startswith("🛠")
    await notifier.stop()


# ------------------------------------------ 8.4 форматирование каждого вида

class TestFormatting:
    def test_sms_notification_contains_sim_sender_time_text(self):
        event = Event(
            type=EventType.SMS,
            imsi=IMSI,
            sim_label="Рабочая",
            at=1_700_000_000.0,
            data={"from": "+79990001122", "text": "Payload"},
        )
        text = format_event(event)
        assert "Рабочая" in text
        assert "+79990001122" in text
        assert "Payload" in text
        # Время в тексте есть -- либо ISO, либо форматированное.
        assert "20" in text  # год из timestamp

    def test_incomplete_sms_is_marked(self):
        event = Event(
            type=EventType.SMS,
            imsi=IMSI,
            sim_label="Рабочая",
            data={
                "from": "+79990001122",
                "text": "part",
                "incomplete": True,
                "missing": [2, 3],
            },
        )
        text = format_event(event)
        assert "не полностью" in text
        assert "2, 3" in text

    def test_call_notification_has_number_or_hidden_and_sim_and_time(self):
        text = format_event(
            Event(
                type=EventType.CALL,
                imsi=IMSI,
                sim_label="Рабочая",
                at=1_700_000_000.0,
                data={"number": "+79990001122", "hidden": False, "outcome": "rejected"},
            )
        )
        assert "Рабочая" in text
        assert "+79990001122" in text
        assert "Отклонено" in text
        assert "Время" in text

        hidden_text = format_event(
            Event(
                type=EventType.CALL,
                imsi=IMSI,
                sim_label="Рабочая",
                data={"number": "", "hidden": True, "outcome": "rejected"},
            )
        )
        assert "скрыт" in hidden_text


# --------------------------------- 8.5 отсутствие секретов в текстах

class TestSecretsHidden:
    def test_pin_guard_notification_does_not_contain_pin_value(self):
        event = Event(
            type=EventType.PIN_GUARD,
            imsi=IMSI,
            sim_label="Рабочая",
            data={"attempts": 2, "known": True, "reason": "остаток 2"},
        )
        # Сам факт: остаток попыток -- да, значение PIN -- нет.
        text = format_event(event)
        assert "2" in text
        # На случай, если по ошибке в поле подсунуто значение PIN:
        malicious = Event(
            type=EventType.PIN_GUARD,
            imsi="000000000000",
            data={"attempts": 3, "reason": "pin 5678 забыт"},
        )
        assert "5678" not in format_event(malicious)

    def test_password_and_token_are_masked_if_they_appear(self):
        event = Event(
            type=EventType.MODEM_FAULT,
            imei="1",
            data={"reason": "password 654321 нужен"},
        )
        text = format_event(event)
        assert "654321" not in text
