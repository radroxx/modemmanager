"""HTTP-сервер приложения в том же процессе, что и опрос модемов.

Собирает вместе:

- маршруты страниц (``/``, ``/history``, ``/sims``, ``/sims/<imsi>``);
- JSON-API (``/api/state``, ``/api/history``, ``/api/sims/<imsi>``, ``/api/modems/<usb>/scan``);
- эндпоинт метрик (``/metrics``) -- без авторизации;
- middleware Basic-Auth для всего остального.

Собранное приложение можно передать в ``starlette.testclient.TestClient`` для
тестов без поднятия сокета; в проде ``start`` запускает ``uvicorn.Server``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, Response
from starlette.routing import Route

from ..config import SettingsStore
from ..eventlog import EventLog
from ..modem_registry import ModemRegistry
from .api import ApiHandlers
from .auth import BasicAuthMiddleware
from . import pages

log = logging.getLogger(__name__)


class WebServer:
    """Владеет ASGI-приложением и (в проде) сервером ``uvicorn``."""

    def __init__(
        self,
        *,
        store: SettingsStore,
        registry: ModemRegistry,
        event_log: EventLog,
        metrics: Any = None,
        reconciler: Any = None,
    ):
        self.store = store
        self.registry = registry
        self.event_log = event_log
        self.metrics = metrics
        self.reconciler = reconciler
        self.api = ApiHandlers(store=store, registry=registry, event_log=event_log)
        self.app = self._build_app()
        self._server: Any = None
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------ жизненный цикл

    async def start(self) -> None:
        import uvicorn

        settings = self.store.settings.web
        config = uvicorn.Config(
            self.app,
            host=settings.host,
            port=settings.port,
            log_level="info",
            lifespan="off",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve(), name="web-server")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        task, self._task = self._task, None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()

    # -------------------------------------------------- сборка ASGI приложения

    def _build_app(self) -> Starlette:
        routes = [
            Route("/", self._index, methods=["GET"]),
            Route("/history", self._history_page, methods=["GET"]),
            Route("/sims", self._sims_index, methods=["GET"]),
            Route("/sims/{imsi}", self._sim_page, methods=["GET"]),
            Route("/api/state", self.api.state, methods=["GET"]),
            Route("/api/history", self.api.history, methods=["GET"]),
            Route("/api/sims/{imsi}", self.api.sim, methods=["GET"]),
            Route("/api/sims/{imsi}", self.api.sim_update, methods=["POST", "PUT"]),
            Route("/api/modems/{usb_path:path}/scan", self.api.scan, methods=["POST"]),
            Route("/metrics", self._metrics, methods=["GET"]),
            Route("/healthz", self._health, methods=["GET"]),
        ]

        app = Starlette(routes=routes)
        app.add_middleware(
            BasicAuthMiddleware, password_getter=self._password
        )
        return app

    # ------------------------------------------------------------------ страницы

    async def _index(self, request: Request) -> HTMLResponse:
        return HTMLResponse(pages.status_page())

    async def _history_page(self, request: Request) -> HTMLResponse:
        return HTMLResponse(pages.history_page())

    async def _sims_index(self, request: Request) -> HTMLResponse:
        imsis = sorted(self.store.settings.sims.keys())
        return HTMLResponse(pages.sims_index_page(imsis))

    async def _sim_page(self, request: Request) -> HTMLResponse:
        imsi = request.path_params["imsi"]
        sim = self.store.settings.sim(imsi)
        modem = self.registry.by_imsi(imsi)
        pin_attempts = modem.state.pin_attempts.pin if modem is not None else None
        return HTMLResponse(
            pages.sim_settings_page(
                imsi=imsi,
                label=sim.label,
                msisdn=sim.msisdn,
                plmn=sim.plmn,
                chat_id=sim.chat_id,
                pin_set=bool(sim.pin),
                pin_attempts=pin_attempts,
            )
        )

    # --------------------------------------------------------------- служебные

    async def _metrics(self, request: Request) -> Response:
        """Эндпоинт Prometheus. Реальные метрики -- в модуле ``metrics``."""
        if self.metrics is None:
            # Пустой ответ -- всё же в формате Prometheus, чтобы скрейпер не
            # ронять ошибками, пока метрики не подключены.
            return PlainTextResponse(
                "# no metrics registered\n",
                media_type="text/plain; version=0.0.4",
            )
        text = self.metrics.render()  # type: ignore[union-attr]
        return PlainTextResponse(text, media_type="text/plain; version=0.0.4")

    async def _health(self, request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok\n")

    def _password(self) -> str:
        return self.store.settings.web.password
