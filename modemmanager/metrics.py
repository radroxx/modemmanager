"""Метрики Prometheus.

Собирается два вида метрик:

- измеряемые значения -- меткой всегда идёт неизменяемый идентификатор
  SIM-карты (``imsi``) или модема (``imei``). Пользовательское имя SIM,
  номер и прочие изменяемые поля попадают в отдельную информационную
  метрику ``mm_sim_info`` -- переименование не создаёт новый time series
  (см. web-interface spec, «Имя SIM-карты не является меткой»).
- счётчики событий -- увеличиваются подписчиком шины.

Тексты сообщений, PIN-коды, пароль интерфейса и маркер Telegram нигде не
попадают в вывод: метки заранее ограничены, тело метрик -- только числа.
"""

from __future__ import annotations

import logging
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Info, generate_latest

from .config import SettingsStore
from .events import Event, EventType
from .modem_registry import ModemRegistry
from .values import RegistrationState


log = logging.getLogger(__name__)


#: Числовое отображение состояния регистрации для gauge.
_REGISTRATION_CODES = {
    RegistrationState.UNKNOWN: 0,
    RegistrationState.NOT_REGISTERED: 1,
    RegistrationState.REGISTERED: 2,
    RegistrationState.SEARCHING: 3,
    RegistrationState.DENIED: 4,
    RegistrationState.ROAMING: 5,
}


class Metrics:
    """Собирает и отдаёт метрики Prometheus."""

    def __init__(self, registry: ModemRegistry, store: SettingsStore):
        self._modems = registry
        self._store = store
        self._registry = CollectorRegistry(auto_describe=False)

        # -------- измеряемые значения (метка -- только неизменяемый идентификатор)
        self._modem_present = Gauge(
            "mm_modem_present",
            "1, если модем сейчас обслуживается",
            ["imei"],
            registry=self._registry,
        )
        self._modem_status = Gauge(
            "mm_modem_status",
            "Текущее состояние модема (метка status выставлена на 1)",
            ["imei", "status"],
            registry=self._registry,
        )
        self._signal_dbm = Gauge(
            "mm_signal_dbm",
            "Уровень сигнала в дБм",
            ["imsi"],
            registry=self._registry,
        )
        self._registration = Gauge(
            "mm_registration_state",
            "Состояние регистрации: 0 unknown, 1 not_registered, "
            "2 registered, 3 searching, 4 denied, 5 roaming",
            ["imsi", "domain"],
            registry=self._registry,
        )
        self._roaming = Gauge(
            "mm_roaming", "1, если регистрация в роуминге", ["imsi"], registry=self._registry
        )
        self._storage_used = Gauge(
            "mm_storage_used",
            "Занятых мест в памяти сообщений",
            ["imsi"],
            registry=self._registry,
        )
        self._storage_total = Gauge(
            "mm_storage_total",
            "Всего мест в памяти сообщений",
            ["imsi"],
            registry=self._registry,
        )
        self._pin_attempts_left = Gauge(
            "mm_pin_attempts_left",
            "Остаток попыток ввода PIN-кода",
            ["imsi"],
            registry=self._registry,
        )
        self._last_sms = Gauge(
            "mm_last_sms_timestamp_seconds",
            "Время последнего полученного сообщения (unix timestamp)",
            ["imsi"],
            registry=self._registry,
        )
        self._last_poll = Gauge(
            "mm_last_poll_timestamp_seconds",
            "Время последнего успешного опроса модема (unix timestamp)",
            ["imei"],
            registry=self._registry,
        )
        self._sim_info = Info(
            "mm_sim",
            "Изменяемые пользователем сведения о SIM (метки для отображения)",
            ["imsi"],
            registry=self._registry,
        )
        self._reconnects_gauge = Gauge(
            "mm_reconnects_total",
            "Сколько раз модем переподключался",
            ["imei"],
            registry=self._registry,
        )
        self._at_errors_gauge = Gauge(
            "mm_at_errors_total",
            "Сколько ошибок обмена было на этом модеме",
            ["imei"],
            registry=self._registry,
        )

        # ------------------------------------------------ счётчики событий
        self._sms_counter = Counter(
            "mm_sms_received_total",
            "Сколько сообщений принято",
            ["imsi"],
            registry=self._registry,
        )
        self._calls_counter = Counter(
            "mm_calls_rejected_total",
            "Сколько вызовов отклонено",
            ["imsi"],
            registry=self._registry,
        )
        self._notify_failed = Counter(
            "mm_notifications_failed_total",
            "Сколько отправок уведомлений завершилось неудачей",
            ["event_type"],
            registry=self._registry,
        )
        self._notify_sent = Counter(
            "mm_notifications_sent_total",
            "Сколько уведомлений отправлено успешно",
            ["event_type"],
            registry=self._registry,
        )

    # ------------------------------------------------------------------ шина

    async def on_event(self, event: Event) -> None:
        """Приём события с шины. Увеличивает счётчики; секретных данных не пишет."""
        if event.type == EventType.SMS:
            imsi = event.imsi or "unknown"
            self._sms_counter.labels(imsi=imsi).inc()
            self._last_sms.labels(imsi=imsi).set(event.at)
        elif event.type == EventType.CALL:
            imsi = event.imsi or "unknown"
            if event.data.get("outcome") in (None, "rejected", "reject_failed"):
                self._calls_counter.labels(imsi=imsi).inc()

    # ---------------------- крючки для уведомителя (счётчики отправки)

    def on_notification_sent(self, event_type: str) -> None:
        self._notify_sent.labels(event_type=event_type or "unknown").inc()

    def on_notification_failed(self, event_type: str) -> None:
        self._notify_failed.labels(event_type=event_type or "unknown").inc()

    # ------------------------------------------------------------- рендер

    def render(self) -> str:
        """Собирает и отдаёт метрики в формате Prometheus."""
        self._refresh_from_registry()
        return generate_latest(self._registry).decode("utf-8")

    def _refresh_from_registry(self) -> None:
        """Синхронизирует значения gauge со снимком реестра модемов."""
        # Реестр -- источник истины, метрики отражают его текущее состояние.
        for state in self._modems.states():
            imei = state.imei or state.usb_path
            imsi = state.imsi
            self._modem_present.labels(imei=imei).set(1)
            self._reconnects_gauge.labels(imei=imei).set(state.reconnects)
            self._at_errors_gauge.labels(imei=imei).set(state.error_count)
            self._modem_status.labels(imei=imei, status=state.status.value).set(1)
            for status in ("online", "no_sim", "pin_required", "puk_locked", "fault", "gone"):
                if status != state.status.value:
                    self._modem_status.labels(imei=imei, status=status).set(0)
            if state.last_poll:
                self._last_poll.labels(imei=imei).set(state.last_poll)

            if imsi:
                if state.signal.dbm is not None:
                    self._signal_dbm.labels(imsi=imsi).set(state.signal.dbm)
                self._registration.labels(imsi=imsi, domain="voice").set(
                    _REGISTRATION_CODES.get(state.registration.voice, 0)
                )
                self._registration.labels(imsi=imsi, domain="data").set(
                    _REGISTRATION_CODES.get(state.registration.data, 0)
                )
                self._roaming.labels(imsi=imsi).set(1 if state.registration.roaming else 0)
                if state.storage.used is not None:
                    self._storage_used.labels(imsi=imsi).set(state.storage.used)
                if state.storage.total is not None:
                    self._storage_total.labels(imsi=imsi).set(state.storage.total)
                if state.pin_attempts.pin is not None:
                    self._pin_attempts_left.labels(imsi=imsi).set(state.pin_attempts.pin)
                if state.last_sms:
                    self._last_sms.labels(imsi=imsi).set(state.last_sms)
                sim = self._store.settings.sim(imsi)
                self._sim_info.labels(imsi=imsi).info(
                    {
                        "label": sim.label or state.sim_label or "",
                        "msisdn": sim.msisdn or "",
                        "operator": state.operator.plmn or "",
                    }
                )
