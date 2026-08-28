"""Набор обслуживаемых модемов.

Единственное место, где живёт состояние рантайма: страница статуса, метрики и
уведомления читают его отсюда и никогда не опрашивают порты сами. Ключ -- путь
устройства на шине USB: он известен ещё до того, как модем ответил, и остаётся
неизменным, пока устройство не переподключили в другой разъём.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from .events import Event, EventType
from .modem import Modem, ModemState, ModemStatus

log = logging.getLogger(__name__)


class ModemRegistry:
    """Живой список модемов и их состояний."""

    def __init__(self) -> None:
        self._modems: dict[str, Modem] = {}

    # ------------------------------------------------------------------- состав

    def add(self, modem: Modem) -> None:
        previous = self._modems.get(modem.usb_path)
        if previous is not None and previous is not modem:
            log.debug("%s: запись модема заменена", modem.usb_path)
        self._modems[modem.usb_path] = modem

    def remove(self, usb_path: str) -> Modem | None:
        return self._modems.pop(usb_path, None)

    def get(self, usb_path: str) -> Modem | None:
        return self._modems.get(usb_path)

    def by_imsi(self, imsi: str) -> Modem | None:
        """Модем с указанной SIM. Связь держится в памяти, файлов состояния нет."""
        for modem in self._modems.values():
            if modem.state.imsi == imsi:
                return modem
        return None

    def by_imei(self, imei: str) -> Modem | None:
        for modem in self._modems.values():
            if modem.state.imei == imei:
                return modem
        return None

    def __iter__(self) -> Iterator[Modem]:
        return iter(list(self._modems.values()))

    def __len__(self) -> int:
        return len(self._modems)

    def __contains__(self, usb_path: object) -> bool:
        return usb_path in self._modems

    # ------------------------------------------------------------- представления

    @property
    def paths(self) -> list[str]:
        return list(self._modems)

    def states(self) -> list[ModemState]:
        return [modem.state for modem in self]

    def snapshot(self) -> list[dict[str, Any]]:
        """Состояния для интерфейса, отсортированные по имени SIM."""
        return [
            state.public_dict()
            for state in sorted(self.states(), key=lambda item: (item.label, item.usb_path))
        ]

    def counts(self) -> dict[str, int]:
        """Сколько модемов в каком состоянии -- для метрик и сводки."""
        result = {status.value: 0 for status in ModemStatus}
        for state in self.states():
            result[state.status.value] += 1
        return result

    # -------------------------------------------------------------------- шина

    async def on_event(self, event: Event) -> None:
        """Отмечает в состоянии модема то, что видно только из события.

        Регистратор не занимается обработкой событий: он лишь запоминает время
        последнего сообщения, чтобы страница статуса показывала признаки жизни
        SIM-карты, по которой давно ничего не приходило.
        """
        if event.type != EventType.SMS or not event.usb_path:
            return
        modem = self._modems.get(event.usb_path)
        if modem is not None:
            modem.state.last_sms = event.at
