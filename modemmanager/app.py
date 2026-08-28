"""Сборка и жизненный цикл приложения.

Один процесс и один event loop: обнаружение и опрос модемов, доставка
уведомлений и HTTP-интерфейс работают рядом.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .config import ConfigError, SettingsStore
from .eventlog import EventLog
from .events import EventBus

log = logging.getLogger(__name__)


class Application:
    """Владеет составными частями и порядком их запуска и остановки."""

    def __init__(
        self,
        config_path: str | Path,
        events_path: str | Path,
        *,
        require_settings: bool = True,
    ):
        self.store = SettingsStore(config_path)
        self.event_log = EventLog(events_path)
        self.bus = EventBus()
        self.require_settings = require_settings
        self._stop = asyncio.Event()
        self._started: list[object] = []

    # ------------------------------------------------------------------ сборка

    def build(self) -> None:
        """Читает настройки и собирает части приложения."""
        settings = self.store.load()
        missing = settings.missing_required()
        if missing and self.require_settings:
            raise ConfigError(
                "не заданы обязательные настройки: "
                + ", ".join(missing)
                + f" (файл {self.store.path})"
            )
        if missing:
            log.warning("не заданы обязательные настройки: %s", ", ".join(missing))

        self.event_log.ensure_file()
        # Журнал -- приоритетный потребитель: событие попадает на диск до того,
        # как кто-либо попытается его доставить.
        self.bus.subscribe(self.event_log.append, priority=0, name="event-log")
        self._warn_if_at_trace_would_be_silent()
        self.build_components()

    def _warn_if_at_trace_would_be_silent(self) -> None:
        """Трассировка обмена пишется на уровне DEBUG. При строже -- записей не будет."""
        if not self.store.settings.discovery.at_trace:
            return
        effective = logging.getLogger().getEffectiveLevel()
        if effective <= logging.DEBUG:
            return
        log.warning(
            "трассировка AT-обмена включена в настройках, но уровень журнала %s: "
            "трассировочные записи не появятся",
            logging.getLevelName(effective),
        )

    def build_components(self) -> None:
        """Создаёт обслуживающие части и подписывает их на шину.

        Вынесено отдельно, чтобы жизненный цикл приложения можно было проверять
        на заглушках, не поднимая настоящие порты и HTTP-сервер.
        """
        from .calls import CallService
        from .metrics import Metrics
        from .modem import Component, Modem
        from .modem_registry import ModemRegistry
        from .network import NetworkService
        from .notify.telegram import TelegramNotifier
        from .discovery.reconciler import Reconciler
        from .sim import SimService
        from .sms.service import SmsService
        from .web.server import WebServer

        self.registry = ModemRegistry()
        self.metrics = Metrics(self.registry, self.store)
        self.notifier = TelegramNotifier(self.store, self.metrics)

        store = self.store

        def component_factory(_modem: Modem) -> list[Component]:
            # По одному экземпляру каждой части на модем: состояние (текущий
            # IMSI, разбираемая многочастная SMS, номер входящего вызова)
            # принадлежит модему, а не приложению.
            return [
                SimService(store),
                SmsService(store),
                CallService(store),
                NetworkService(store),
            ]

        # Метрики читают состояние из регистратора, поэтому цикл сверки о них
        # ничего не знает.
        self.reconciler = Reconciler(
            store=self.store,
            bus=self.bus,
            registry=self.registry,
            component_factory=component_factory,
        )
        self.web = WebServer(
            store=self.store,
            registry=self.registry,
            event_log=self.event_log,
            metrics=self.metrics,
            reconciler=self.reconciler,
        )

        self.bus.subscribe(self.registry.on_event, priority=10, name="registry")
        self.bus.subscribe(self.metrics.on_event, priority=20, name="metrics")
        self.bus.subscribe(self.notifier.on_event, priority=30, name="notifier")

    # ------------------------------------------------------------------- запуск

    async def run(self) -> None:
        self.build()
        await self.notifier.start()
        self._started.append(self.notifier)
        await self.reconciler.start()
        self._started.append(self.reconciler)
        await self.web.start()
        self._started.append(self.web)
        log.info("приложение запущено")
        try:
            await self._stop.wait()
        finally:
            await self.shutdown()

    def request_stop(self) -> None:
        self._stop.set()

    async def shutdown(self) -> None:
        log.info("остановка приложения")
        for component in reversed(self._started):
            try:
                await component.stop()  # type: ignore[attr-defined]
            except Exception:
                log.exception("сбой при остановке %s", type(component).__name__)
        self._started.clear()
        log.info("приложение остановлено")
