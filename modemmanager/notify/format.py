"""Форматирование текста уведомлений.

Уведомления читают люди, поэтому здесь никаких сериализаций и никаких секретов
(PIN-код, пароль, маркер Telegram). Каждый тип события знает свой шаблон, а
подстановка идёт по имени поля, чтобы отсутствующие данные не ломали формат.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..events import Event, EventType

#: Что заведомо не должно попадать в тексты уведомлений. Формат берёт только
#: явные поля события, но на всякий случай можно спрятать текст, если поле
#: было названо как секрет по ошибке.
_SECRET_KEYS = frozenset({"pin", "password", "token", "bot_token"})


def _sim_line(event: Event) -> str:
    label = event.sim_label or event.imsi or "SIM неизвестна"
    return f"SIM: {label}"


def _timestamp(value: float | None) -> str:
    ts = value or 0.0
    if ts <= 0:
        return "время неизвестно"
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _hardware_line(event: Event) -> str:
    parts = []
    if event.imei:
        parts.append(f"IMEI: {event.imei}")
    if event.usb_path:
        parts.append(f"порт: {event.usb_path}")
    return "\n".join(parts)


def _mask_secrets(text: str) -> str:
    """Последняя линия защиты: если PIN случайно оказался в тексте, скрыть его."""
    for word in ("pin", "password", "token"):
        if word in text.lower():
            # Не сжигаем поле целиком: люди путают названия. Скрываем только
            # число длины 4..8 подряд идущих цифр рядом с этим словом.
            import re

            text = re.sub(
                rf"({word}[\s:=]+)\d{{4,8}}",
                r"\1***",
                text,
                flags=re.IGNORECASE,
            )
    return text


# ------------------------------------------------------------- шаблоны событий

def _format_sms(event: Event) -> str:
    incomplete = bool(event.data.get("incomplete"))
    sender = event.data.get("from") or "номер неизвестен"
    text = event.data.get("text") or ""
    header = "📨 SMS (получено не полностью)" if incomplete else "📨 SMS"
    lines = [
        header,
        _sim_line(event),
        f"От: {sender}",
        f"Время: {_timestamp(event.at)}",
    ]
    if incomplete:
        missing = event.data.get("missing") or []
        if missing:
            missed = ", ".join(str(n) for n in missing)
            lines.append(f"Не пришли части: {missed}")
    lines.append("")
    lines.append(text or "(пусто)")
    return "\n".join(lines)


def _format_call(event: Event) -> str:
    number = event.data.get("number") or ""
    hidden = bool(event.data.get("hidden"))
    outcome = event.data.get("outcome") or ""
    lines = [
        "📞 Входящий вызов",
        _sim_line(event),
    ]
    if hidden or not number:
        lines.append("Номер: скрыт")
    else:
        lines.append(f"Номер: {number}")
    lines.append(f"Время: {_timestamp(event.at)}")
    outcome_labels = {
        "rejected": "Отклонено системой.",
        "reject_failed": "Отклонение не удалось.",
        "ended_by_caller": "Вызывающий сам положил трубку.",
    }
    lines.append(outcome_labels.get(outcome, ""))
    return "\n".join(line for line in lines if line)


def _format_modem_gone(event: Event) -> str:
    return "\n".join(
        line
        for line in (
            "⚠️ Модем пропал",
            _sim_line(event),
            _hardware_line(event),
            f"Время: {_timestamp(event.at)}",
        )
        if line
    )


def _format_modem_up(event: Event) -> str:
    family = event.data.get("family") or "неизвестное семейство"
    return "\n".join(
        line
        for line in (
            "✅ Модем на связи",
            f"Семейство: {family}",
            _hardware_line(event),
        )
        if line
    )


def _format_modem_fault(event: Event) -> str:
    reason = event.data.get("reason") or "причина не указана"
    return "\n".join(
        line
        for line in (
            "🛠 Модем неисправен",
            _sim_line(event),
            _hardware_line(event),
            f"Причина: {reason}",
        )
        if line
    )


def _format_modem_recovered(event: Event) -> str:
    return "\n".join(
        line
        for line in (
            "✅ Модем восстановился",
            _sim_line(event),
            _hardware_line(event),
        )
        if line
    )


def _format_sim_absent(event: Event) -> str:
    return "\n".join(
        line
        for line in (
            "⚠️ SIM-карта отсутствует",
            _hardware_line(event),
            f"Время: {_timestamp(event.at)}",
        )
        if line
    )


def _format_sim_unknown(event: Event) -> str:
    label = event.data.get("auto_label") or event.imsi or "новая SIM"
    return "\n".join(
        line
        for line in (
            "❓ Обнаружена SIM без настроек",
            f"Имя: {label}",
            "Адресат уведомлений для неё не назначен.",
            _hardware_line(event),
        )
        if line
    )


def _format_pin_guard(event: Event) -> str:
    attempts = event.data.get("attempts")
    known = event.data.get("known")
    if known is False or attempts is None:
        remaining = "неизвестен"
    else:
        remaining = str(attempts)
    return "\n".join(
        line
        for line in (
            "🔒 Защита PIN сработала",
            _sim_line(event),
            f"Осталось попыток: {remaining}",
            "PIN на карте не отправлялся -- риск блокировки по PUK.",
            _hardware_line(event),
        )
        if line
    )


def _format_pin_required(event: Event) -> str:
    return "\n".join(
        line
        for line in (
            "🔑 SIM требует PIN-код, а его нет в настройках",
            _sim_line(event),
            _hardware_line(event),
        )
        if line
    )


def _format_pin_rejected(event: Event) -> str:
    attempts = event.data.get("attempts")
    remaining = str(attempts) if attempts is not None else "неизвестно"
    return "\n".join(
        line
        for line in (
            "🚫 PIN-код отклонён модемом",
            _sim_line(event),
            f"Осталось попыток: {remaining}",
            _hardware_line(event),
        )
        if line
    )


def _format_puk_locked(event: Event) -> str:
    return "\n".join(
        line
        for line in (
            "🚨 SIM заблокирована по PUK",
            _sim_line(event),
            "Разблокировка -- только вручную, вставив карту в телефон.",
            _hardware_line(event),
        )
        if line
    )


def _format_no_service(event: Event) -> str:
    operator = event.data.get("operator") or "не указан"
    return "\n".join(
        line
        for line in (
            "📶 Нет регистрации в сети",
            _sim_line(event),
            f"Заданный оператор: {operator}",
            _hardware_line(event),
        )
        if line
    )


def _format_default(event: Event) -> str:
    """Резервный шаблон для событий, которым не полагается ручной шаблон."""
    lines = [f"Событие: {event.type}"]
    if event.sim_label or event.imsi:
        lines.append(_sim_line(event))
    hw = _hardware_line(event)
    if hw:
        lines.append(hw)
    lines.append(f"Время: {_timestamp(event.at)}")
    return "\n".join(lines)


_FORMATTERS = {
    EventType.SMS: _format_sms,
    EventType.CALL: _format_call,
    EventType.MODEM_UP: _format_modem_up,
    EventType.MODEM_GONE: _format_modem_gone,
    EventType.MODEM_FAULT: _format_modem_fault,
    EventType.MODEM_RECOVERED: _format_modem_recovered,
    EventType.SIM_ABSENT: _format_sim_absent,
    EventType.SIM_UNKNOWN: _format_sim_unknown,
    EventType.PIN_GUARD: _format_pin_guard,
    EventType.PIN_REQUIRED: _format_pin_required,
    EventType.PIN_REJECTED: _format_pin_rejected,
    EventType.PUK_LOCKED: _format_puk_locked,
    EventType.NO_SERVICE: _format_no_service,
}


def format_event(event: Event, *, admin_footer: str = "") -> str:
    """Собирает текст уведомления по событию.

    ``admin_footer`` добавляется в конце и используется, когда событие попало
    в администраторский чат по маршрутизации-фолбэку (см. router).
    """
    formatter = _FORMATTERS.get(event.type, _format_default)
    text = formatter(event)
    if admin_footer:
        text = f"{text}\n\n({admin_footer})"
    return _mask_secrets(text)
