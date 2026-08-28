"""События и внутренняя шина.

Шина разделяет формирование события и его потребителей (журнал, уведомления,
метрики, интерфейс). Журнал -- приоритетный потребитель: событие записывается на
диск до попытки отправки уведомления (см. event-log spec).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


class EventType:
    SMS = "sms"
    CALL = "call"
    MODEM_UP = "modem_up"
    MODEM_GONE = "modem_gone"
    MODEM_FAULT = "modem_fault"
    MODEM_RECOVERED = "modem_recovered"
    SIM_STATE = "sim_state"
    SIM_ABSENT = "sim_absent"
    SIM_UNKNOWN = "sim_unknown"
    PIN_GUARD = "pin_guard"
    PIN_REJECTED = "pin_rejected"
    PIN_REQUIRED = "pin_required"
    PUK_LOCKED = "puk_locked"
    NO_SERVICE = "no_service"
    SCAN_RESULT = "scan_result"
    FORBIDDEN_COMMAND = "forbidden_command"


#: События, которые всегда идут администратору: часть из них возникает до того,
#: как удалось прочитать IMSI, поэтому маршрутизировать их по SIM невозможно.
SYSTEM_EVENT_TYPES = frozenset(
    {
        EventType.MODEM_UP,
        EventType.MODEM_GONE,
        EventType.MODEM_FAULT,
        EventType.MODEM_RECOVERED,
        EventType.SIM_ABSENT,
        EventType.SIM_UNKNOWN,
        EventType.PIN_GUARD,
        EventType.PIN_REJECTED,
        EventType.PIN_REQUIRED,
        EventType.PUK_LOCKED,
        EventType.NO_SERVICE,
    }
)

#: События, о которых уведомляют однократно при входе в состояние.
STATEFUL_EVENT_TYPES = frozenset(
    {
        EventType.MODEM_GONE,
        EventType.MODEM_FAULT,
        EventType.SIM_ABSENT,
        EventType.PIN_GUARD,
        EventType.PIN_REQUIRED,
        EventType.PUK_LOCKED,
        EventType.NO_SERVICE,
    }
)


#: Признаки, по которым запись журнала связывается с железом и SIM.
IDENTITY_FIELDS = ("imsi", "sim_label", "imei", "usb_path", "tty")

#: Ключи, которые полезная нагрузка события не имеет права занимать.
RESERVED_FIELDS = frozenset({"at", "ts", "type", *IDENTITY_FIELDS})


@dataclass
class Event:
    """Одно наблюдаемое событие системы."""

    type: str
    at: float = field(default_factory=time.time)
    imsi: str | None = None
    sim_label: str | None = None
    imei: str | None = None
    usb_path: str | None = None
    tty: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        """Плоская запись для журнала: обязательные поля впереди.

        Признаки события (тип, время, IMSI, IMEI...) главнее полезной нагрузки:
        одноимённый ключ из ``data`` не подменяет их, иначе запись в журнале
        может соврать о том, к какому модему она относится.
        """
        record: dict[str, Any] = {
            "at": _isoformat(self.at),
            "ts": round(self.at, 3),
            "type": self.type,
        }
        for key in IDENTITY_FIELDS:
            value = getattr(self, key)
            if value:
                record[key] = value
        for key, value in self.data.items():
            if key in record or key in RESERVED_FIELDS:
                log.warning("поле %r в данных события %s перекрыто признаком", key, self.type)
                continue
            record[key] = value
        return record

    @property
    def dedup_key(self) -> tuple[str, str]:
        """Ключ состояния для однократных уведомлений."""
        subject = self.imei or self.usb_path or self.imsi or ""
        return (self.type, subject)


def _isoformat(timestamp: float) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


Subscriber = Callable[[Event], Awaitable[None] | None]


class EventBus:
    """Доставляет события подписчикам в порядке приоритета, изолируя сбои."""

    def __init__(self) -> None:
        self._subscribers: list[tuple[int, str, Subscriber]] = []

    def subscribe(self, handler: Subscriber, *, priority: int = 50, name: str = "") -> None:
        """Меньший приоритет -- раньше. Журнал подписывается с приоритетом 0."""
        label = name or getattr(handler, "__qualname__", repr(handler))
        self._subscribers.append((priority, label, handler))
        self._subscribers.sort(key=lambda item: item[0])

    async def publish(self, event: Event) -> None:
        """Последовательно вызывает подписчиков; исключение одного не мешает остальным."""
        for _priority, label, handler in list(self._subscribers):
            try:
                result = handler(event)
                if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("подписчик %s не смог обработать событие %s", label, event.type)
