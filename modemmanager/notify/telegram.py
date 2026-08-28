"""Доставка уведомлений в Telegram.

Отделено от шины событий очередью с повторами и растущей задержкой: событие
приходит, оно попадает в журнал (у журнала приоритет ниже), маршрутизатор
подбирает адресата, формируется текст и отправка ставится в очередь. Реальная
доставка идёт в отдельной задаче: недоступность Telegram не должна ни задержать
освобождение памяти SIM, ни блокировать шину.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from ..config import SettingsStore
from ..events import Event, EventType
from .format import format_event
from .router import RouterDecision, is_recovery, is_stateful, route_event

log = logging.getLogger(__name__)


class Delivery(Protocol):
    """Тонкий интерфейс для отправки текста в Telegram.

    Отдельный тип нужен, чтобы тесты могли поставить свою реализацию, не
    поднимая настоящий HTTP-клиент.
    """

    async def send(self, chat_id: str, text: str) -> None: ...


class DeliveryError(Exception):
    """Отправка сорвалась: временный сбой сети или отказ Telegram."""


@dataclass
class _Item:
    chat_id: str
    text: str
    attempts: int = 0
    next_attempt: float = 0.0
    #: Тип события -- только для метрик и журнала диагностики.
    event_type: str = ""


class TelegramNotifier:
    """Публикует уведомления, не мешая приёму сообщений."""

    def __init__(
        self,
        store: SettingsStore,
        metrics: Any = None,
        *,
        delivery: Delivery | None = None,
        clock: Callable[[], float] = time.monotonic,
        initial_backoff: float = 5.0,
    ):
        self.store = store
        self.metrics = metrics
        self.delivery: Delivery = delivery or _make_default_delivery(store)
        self._clock = clock
        self._initial_backoff = initial_backoff
        self._queue: deque[_Item] = deque()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping = False
        #: Ключи стойких состояний, о которых уже уведомили. Сюда попадают только
        #: события из ``STATEFUL_EVENT_TYPES``; после события восстановления
        #: соответствующие записи снимаются.
        self._seen: dict[tuple[str, str], float] = {}
        #: Счётчики для метрик (пока метрик нет, используются только тестами).
        self.failed_sends = 0
        self.sent = 0

    # ------------------------------------------------------------- жизненный цикл

    async def start(self) -> None:
        self._stopping = False
        if self._task is None:
            self._task = asyncio.create_task(self._deliver_loop(), name="telegram-deliver")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------ приём

    async def on_event(self, event: Event) -> None:
        """Подписчик шины. Не выполняет отправку -- только очередь."""
        # Событие уже в журнале: у записи журнала приоритет 0, у нас 30.
        # Проверять недоставку смысла нет, пометка «в очереди» ничему не поможет.

        if is_recovery(event):
            self._clear_seen_for(event)

        if event.type == EventType.SIM_STATE:
            # SIM_STATE -- служебный сигнал для дедупа. Само по себе уведомление
            # о нём не отправляется: уведомляют «входы» в конкретные состояния
            # (PIN_REQUIRED, PIN_GUARD, PUK_LOCKED, SIM_ABSENT).
            return

        if is_stateful(event):
            key = event.dedup_key
            if key in self._seen:
                # Уже уведомили о входе в это состояние; повторять не нужно.
                return
            self._seen[key] = event.at

        decision = route_event(event, self.store.settings)
        if not decision.routed:
            log.warning("уведомление о %s некуда отправить: адресат не задан", event.type)
            return

        footer = decision.reason if decision.fallback_to_admin else ""
        text = format_event(event, admin_footer=footer)
        item = _Item(chat_id=decision.chat_id, text=text, event_type=event.type)
        self._queue.append(item)
        self._wake.set()

    # ------------------------------------------------------------ восстановление

    def _clear_seen_for(self, event: Event) -> None:
        """Событие возврата в норму снимает дедуп по субъекту.

        Собственное уведомление о восстановлении посылать не нужно: событие
        ``MODEM_RECOVERED`` / ``MODEM_UP`` дальше пройдёт стандартным путём и
        будет отправлено обычной маршрутизацией.
        """
        subject = event.imei or event.usb_path or event.imsi
        if not subject:
            return
        cleared = [key for key in self._seen if key[1] == subject]
        for key in cleared:
            del self._seen[key]

    # --------------------------------------------------------------- доставка

    async def _deliver_loop(self) -> None:
        max_delay = self.store.settings.telegram.max_retry_delay
        while not self._stopping:
            item = self._next_ready()
            if item is None:
                # Либо очередь пуста, либо ближайшая попытка ещё не подошла.
                await self._sleep_until_next()
                continue
            try:
                await self.delivery.send(item.chat_id, item.text)
                self.sent += 1
                self._on_metric("sent", item.event_type)
            except DeliveryError as exc:
                self._on_delivery_failure(item, exc, max_delay=max_delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- защита от неожиданной ошибки
                self._on_delivery_failure(item, DeliveryError(str(exc)), max_delay=max_delay)

    def _next_ready(self) -> _Item | None:
        if not self._queue:
            return None
        now = self._clock()
        # Быстрый путь: голова очереди готова.
        head = self._queue[0]
        if head.next_attempt <= now:
            return self._queue.popleft()
        return None

    async def _sleep_until_next(self) -> None:
        if not self._queue:
            self._wake.clear()
            try:
                await self._wake.wait()
            except asyncio.CancelledError:
                raise
            return
        # У головы очереди назначено время следующей попытки: спим до него.
        head = self._queue[0]
        delay = max(0.0, head.next_attempt - self._clock())
        try:
            self._wake.clear()
            await asyncio.wait_for(self._wake.wait(), timeout=delay or 0.01)
        except asyncio.TimeoutError:
            return
        except asyncio.CancelledError:
            raise

    def _on_delivery_failure(
        self, item: _Item, exc: DeliveryError, *, max_delay: float
    ) -> None:
        item.attempts += 1
        self.failed_sends += 1
        self._on_metric("failed", item.event_type)
        delay = min(max_delay, self._initial_backoff * (2 ** (item.attempts - 1)))
        item.next_attempt = self._clock() + delay
        # Возвращаем в конец очереди, чтобы новые уведомления не ждали за спиной
        # хронически битого сообщения.
        self._queue.append(item)
        log.info(
            "не удалось отправить %s (попытка %d): %s; следующая через %.1f с",
            item.event_type or "уведомление",
            item.attempts,
            exc,
            delay,
        )

    def _on_metric(self, name: str, event_type: str) -> None:
        if self.metrics is None:
            return
        handler = getattr(self.metrics, f"on_notification_{name}", None)
        if handler is None:
            return
        try:
            handler(event_type)
        except Exception:  # pragma: no cover -- защита от опечаток в метриках
            log.exception("сбой метрики уведомлений")


# ---------------------------------------------------------------- вспомогательное


class _RecoveryProxy:
    """Прокси события с подменённым типом -- для маршрутизации recovery.

    Модификация исходного события испортила бы запись в журнале, поэтому
    подставляется лёгкая обёртка, у которой ровно те же поля.
    """

    __slots__ = ("_wrapped", "_type")

    def __init__(self, wrapped: Event, event_type: str):
        self._wrapped = wrapped
        self._type = event_type

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    @property
    def type(self) -> str:
        return self._type

    @property
    def data(self) -> dict[str, Any]:
        return self._wrapped.data


def _make_default_delivery(store: SettingsStore) -> Delivery:
    """Ленивая фабрика: реальный HTTP-клиент подключаем только если понадобится."""
    return _LazyHttpDelivery(store)


class _LazyHttpDelivery:
    """Отложенно инициализируемый настоящий отправщик через ``httpx``.

    Поддерживает контракт ``Delivery``. Отдельный класс нужен, чтобы модуль
    ``modemmanager.notify`` можно было импортировать в тестовой среде, где
    ``httpx`` может отсутствовать.
    """

    def __init__(self, store: SettingsStore):
        self.store = store
        self._client: Any = None

    async def send(self, chat_id: str, text: str) -> None:
        settings = self.store.settings.telegram
        if not settings.token:
            raise DeliveryError("маркер Telegram не задан")
        if self._client is None:
            import httpx  # локальный импорт: тесты могут не иметь httpx

            self._client = httpx.AsyncClient(timeout=10.0)
        url = f"{settings.api_base.rstrip('/')}/bot{settings.token}/sendMessage"
        try:
            response = await self._client.post(
                url,
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            )
        except Exception as exc:  # noqa: BLE001 -- любая сетевая ошибка -> retry
            raise DeliveryError(str(exc)) from exc
        if response.status_code >= 500:
            raise DeliveryError(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            # 4xx -- проблема с запросом: повтор ничего не даст, но пометим как
            # ошибку для метрик и пусть выпадет из очереди.
            log.warning("Telegram отказал: %s %s", response.status_code, response.text[:200])
            return


# HttpxDelivery -- публичный алиас, полезный для тестов и внешних потребителей,
# которые хотят подставить свою реализацию доставки.
HttpxDelivery = _LazyHttpDelivery
