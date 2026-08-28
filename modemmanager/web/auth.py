"""HTTP Basic Auth: один пароль на все страницы, кроме ``/metrics``.

Смысл именно в единственном пароле: не нужны роли и учётные записи, интерфейс
диагностический. Basic Auth выбран потому, что и браузер, и ``curl`` его
понимают без вспомогательного эндпоинта авторизации, а метрики можно оставить
без защиты одной строкой правил в middleware.
"""

from __future__ import annotations

import base64
import hmac
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

#: Пути, доступные без авторизации. Метрики -- по спецификации; корневая
#: проверка живучести -- удобно для скрейперов.
_PUBLIC_PATHS = frozenset({"/metrics", "/healthz"})


def compare(provided: str, expected: str) -> bool:
    """Сравнивает пароли постоянным по времени способом."""
    if not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def parse_basic_auth(value: str) -> str | None:
    """Возвращает пароль из заголовка ``Authorization: Basic ...`` или ``None``."""
    if not value.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(value[6:], validate=True).decode("utf-8", "replace")
    except (ValueError, base64.binascii.Error):
        return None
    _, _, password = decoded.partition(":")
    return password


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Middleware, требующий один пароль на все страницы, кроме публичных."""

    def __init__(self, app, *, password_getter) -> None:
        super().__init__(app)
        self._password_getter = password_getter

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)
        password = self._password_getter()
        provided = parse_basic_auth(request.headers.get("Authorization", ""))
        if provided is None or not compare(provided, password):
            return Response(
                "authentication required\n",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="modemmanager"'},
            )
        return await call_next(request)
