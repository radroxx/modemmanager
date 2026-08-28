"""Реестр поведений семейств модемов.

Выбор делается по ответу модема на пробу. Пара идентификаторов USB передаётся
только как подсказка: модуль SIM800 подключён через универсальный мост, и
идентификаторы принадлежат мосту, а не модулю, поэтому решение по ним было бы
неверным.
"""

from __future__ import annotations

import logging

from ..values import Identity
from .base import Kind, ModemBehavior, Unsolicited
from .generic import GenericAtBehavior
from .huawei import HuaweiBehavior
from .sim800 import Sim800Behavior

log = logging.getLogger(__name__)

#: Порядок важен: первое подошедшее семейство и выбирается.
_REGISTRY: list[type[ModemBehavior]] = []

#: Запасной вариант для модема, который отвечает на AT, но не опознан.
FALLBACK: type[ModemBehavior] = GenericAtBehavior


def register(behavior: type[ModemBehavior]) -> type[ModemBehavior]:
    """Добавляет семейство в реестр. Годится как декоратор."""
    if behavior not in _REGISTRY:
        _REGISTRY.append(behavior)
    return behavior


def registered() -> tuple[type[ModemBehavior], ...]:
    return tuple(_REGISTRY)


def families() -> tuple[str, ...]:
    return tuple(behavior.family for behavior in _REGISTRY) + (FALLBACK.family,)


def select(identity: Identity, *, hint: str = "") -> ModemBehavior:
    """Возвращает поведение для опознанного модема.

    Неопознанный модем получает универсальное поведение и обслуживается с
    ограниченным набором возможностей, а не отбрасывается.
    """
    for behavior in _REGISTRY:
        try:
            if behavior.matches(identity, hint=hint):
                return behavior()
        except Exception:  # pragma: no cover -- защита от ошибки в семействе
            log.exception("сбой опознания семейством %s", behavior.family)
    log.info(
        "модем не опознан (%s), назначено универсальное поведение",
        identity.description,
    )
    return FALLBACK()


def by_family(name: str) -> type[ModemBehavior] | None:
    """Ищет семейство по имени -- нужно для восстановления состояния и тестов."""
    for behavior in (*_REGISTRY, FALLBACK):
        if behavior.family == name:
            return behavior
    return None


register(HuaweiBehavior)
register(Sim800Behavior)

__all__ = [
    "FALLBACK",
    "GenericAtBehavior",
    "HuaweiBehavior",
    "Kind",
    "ModemBehavior",
    "Sim800Behavior",
    "Unsolicited",
    "by_family",
    "families",
    "register",
    "registered",
    "select",
]
