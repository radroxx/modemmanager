"""JSON-API веб-интерфейса.

Все API-эндпоинты возвращают ``application/json``. Схема простая: списки в
``/api/state`` и ``/api/history``, объект в ``/api/sims/<imsi>``. Никаких
курсоров -- история читается «сверху» N последних записей, а состояние подаётся
целиком (см. spec, «Данные страницы обновляются»).
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ..config import SettingsStore
from ..eventlog import EventLog
from ..modem_registry import ModemRegistry
from ..network import NetworkService, ScanBusy

log = logging.getLogger(__name__)


class ApiHandlers:
    """Общий контейнер зависимостей: обработчики -- методы для удобства."""

    def __init__(
        self,
        *,
        store: SettingsStore,
        registry: ModemRegistry,
        event_log: EventLog,
    ):
        self.store = store
        self.registry = registry
        self.event_log = event_log

    # ---------------------------------------------------------------- состояние

    async def state(self, request: Request) -> JSONResponse:
        payload = {
            "modems": self.registry.snapshot(),
            "counts": self.registry.counts(),
        }
        return JSONResponse(payload)

    # ------------------------------------------------------------- SIM settings

    async def sim(self, request: Request) -> JSONResponse:
        imsi = request.path_params["imsi"]
        sim = self.store.settings.sim(imsi)
        return JSONResponse(self._sim_payload(imsi, sim))

    async def sim_update(self, request: Request) -> JSONResponse:
        imsi = request.path_params["imsi"]
        patch = await _parse_body(request)
        # PIN и остальные поля обрабатываются в SettingsStore.update_sim.
        # Из формы отдельно приходит флаг ``pin_clear`` -- он означает удалить,
        # а не оставить как есть: превращаем в ``pin: null``.
        if patch.pop("pin_clear", None) in ("1", "true", True):
            patch["pin"] = None
        # Пустая строка ``pin`` из формы означает «не менять» -- удаляем ключ.
        if patch.get("pin", None) == "":
            patch.pop("pin", None)
        sim = self.store.update_sim(imsi, patch)
        # Применение нового выбора оператора -- задача NetworkService модема.
        # Здесь только применение к возможно присутствующему модему:
        modem = self.registry.by_imsi(imsi)
        if modem is not None:
            network = modem.find_component(NetworkService)
            if network is not None:
                try:
                    await network.apply_operator()
                except Exception:
                    log.exception("не удалось применить новые настройки к модему %s", imsi)
        return JSONResponse(self._sim_payload(imsi, sim))

    def _sim_payload(self, imsi: str, sim) -> dict[str, Any]:
        modem = self.registry.by_imsi(imsi)
        pin_attempts_left: int | None = None
        if modem is not None:
            pin_attempts_left = modem.state.pin_attempts.pin
        return {
            "imsi": imsi,
            **sim.public_dict(),
            "pin_attempts_left": pin_attempts_left,
        }

    # ---------------------------------------------------------------- история

    async def history(self, request: Request) -> JSONResponse:
        params = request.query_params
        try:
            limit = int(params.get("limit", "100"))
        except ValueError:
            limit = 100
        limit = max(1, min(limit, 1000))
        imsi = params.get("imsi") or None
        types = params.getlist("type") or None
        items = await self.event_log.tail(limit=limit, imsi=imsi, types=types)
        return JSONResponse(items)

    # ---------------------------------------------------- поиск операторов

    async def scan(self, request: Request) -> JSONResponse:
        usb_path = request.path_params["usb_path"]
        modem = self.registry.get(usb_path)
        if modem is None:
            return JSONResponse({"error": "модем не найден"}, status_code=404)
        network = modem.find_component(NetworkService)
        if network is None:
            return JSONResponse({"error": "модем не поддерживает поиск"}, status_code=409)
        try:
            result = await network.scan()
        except ScanBusy:
            return JSONResponse({"error": "поиск уже идёт"}, status_code=409)
        return JSONResponse(result.public_dict())


async def _parse_body(request: Request) -> dict[str, Any]:
    """Читает тело как JSON или как form-encoded в зависимости от типа контента."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type == "application/json":
        try:
            data = await request.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    if content_type in ("application/x-www-form-urlencoded", "multipart/form-data"):
        form = await request.form()
        return {key: form.get(key) for key in form}
    return {}
