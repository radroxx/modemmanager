"""Маршрутизация событий: кому уходит уведомление.

Правила простые: сообщения и вызовы -- в адресат SIM, всё остальное -- в
администраторский. SIM без назначенного адресата тоже уходит администратору, но
с пометкой о том, что настроить чат для карты никто не удосужился (см. spec
notifications, «Для SIM-карты адресат не задан»).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..events import STATEFUL_EVENT_TYPES, SYSTEM_EVENT_TYPES, Event, EventType


@dataclass(frozen=True)
class RouterDecision:
    """Куда уходит уведомление и что об этом надо сказать в тексте."""

    chat_id: str
    #: ``True``, если исходно уведомление предназначалось SIM, но чата у неё
    #: нет; сообщение уходит администратору со специальным подстрочником.
    fallback_to_admin: bool = False
    #: Причина, по которой пришлось перенаправить. Используется в форматере,
    #: чтобы админский чат получал ясную пометку.
    reason: str = ""

    @property
    def routed(self) -> bool:
        return bool(self.chat_id)


def route_event(event: Event, settings: Settings) -> RouterDecision:
    """Возвращает решение о маршруте для события."""
    admin = settings.telegram.admin_chat_id
    if event.type in SYSTEM_EVENT_TYPES:
        return RouterDecision(chat_id=admin)
    sim_chat = ""
    if event.imsi:
        sim_chat = settings.sim(event.imsi).chat_id
    if sim_chat:
        return RouterDecision(chat_id=sim_chat)
    if event.imsi:
        return RouterDecision(
            chat_id=admin,
            fallback_to_admin=True,
            reason="адресат для этой SIM не назначен",
        )
    return RouterDecision(chat_id=admin, fallback_to_admin=True, reason="IMSI неизвестен")


def is_stateful(event: Event) -> bool:
    """Признак того, что событие описывает длительное состояние."""
    return event.type in STATEFUL_EVENT_TYPES


#: События, обозначающие возврат в нормальное состояние. При их приходе
#: снимаются все «дедуп-запоминания» для этого субъекта -- следующий вход в
#: неисправное состояние должен снова уведомить.
RECOVERY_EVENT_TYPES = frozenset(
    {
        EventType.MODEM_UP,
        EventType.MODEM_RECOVERED,
    }
)


def is_recovery(event: Event) -> bool:
    """Признак события восстановления."""
    if event.type in RECOVERY_EVENT_TYPES:
        return True
    if event.type == EventType.SIM_STATE and event.data.get("state") == "ready":
        return True
    return False
