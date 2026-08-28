"""Запрет команд, приводящих к расходам, и маскирование секретов.

Проверка стоит на уровне записи в порт, а не в вызывающем коде: любая ошибка в
логике выше не должна приводить к платной команде. SIM-карты в роуминге, поэтому
отправка сообщений и исходящие вызовы запрещены навсегда, а не настройкой.
"""

from __future__ import annotations

import re

#: Причины запрета. Ключ -- регулярное выражение по нормализованной команде.
_FORBIDDEN: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^AT\+CMGS\b"), "отправка сообщения (тарифицируется)"),
    (re.compile(r"^AT\+CMSS\b"), "отправка сообщения из памяти (тарифицируется)"),
    (re.compile(r"^AT\+CMGC\b"), "отправка служебной команды сети (тарифицируется)"),
    (re.compile(r"^ATD"), "исходящий вызов (тарифицируется)"),
    (re.compile(r"^AT\+CMOD\b"), "смена режима вызова не используется"),
    (re.compile(r"^ATA\b"), "ответ на вызов (тарифицируется)"),
    (re.compile(r"^AT\+CHLD\b"), "управление удержанием вызова не используется"),
    (re.compile(r"^AT\+CGDATA\b"), "переход в режим передачи данных (тарифицируется)"),
    (re.compile(r"^AT\+CGACT\b"), "активация канала передачи данных (тарифицируется)"),
    (re.compile(r"^ATO\b"), "возврат в режим передачи данных (тарифицируется)"),
    (re.compile(r"^AT\+USSD\b|^AT\+CUSD\b"), "запрос USSD (тарифицируется)"),
)

#: Явно разрешённые команды, попадающие под шаблон запрета по форме.
#: ``ATH`` и ``AT+CHUP`` завершают вызов -- отклонение входящего вызова бесплатно.
_ALLOWED = (
    re.compile(r"^ATH\d?$"),
    re.compile(r"^AT\+CHUP$"),
)


def normalise(command: str) -> str:
    """Приводит команду к виду, по которому проверяется запрет."""
    stripped = command.strip().rstrip("\r\n")
    # Модемы одинаково принимают at, At и AT; проверка не должна это различать.
    if stripped[:2].lower() == "at":
        stripped = "AT" + stripped[2:]
    return stripped


def forbidden_reason(command: str) -> str | None:
    """Возвращает причину запрета или ``None``, если команда разрешена."""
    normalised = normalise(command)
    for allowed in _ALLOWED:
        if allowed.match(normalised):
            return None
    upper = normalised.upper()
    for pattern, reason in _FORBIDDEN:
        if pattern.match(upper):
            return reason
    return None


#: Команды, в которых значение является секретом и не должно попадать в вывод.
_SECRET_COMMANDS = re.compile(
    r"^(AT\+(CPIN|CPWD|CLCK|CPUC|CSIM)\s*=)(.*)$",
    re.IGNORECASE,
)

MASK = "***"


def mask(text: str) -> str:
    """Заменяет значения PIN-кодов и паролей маской.

    Применяется ко всему, что уходит в диагностику обмена: одной точки
    маскирования достаточно, чтобы секрет не утёк ни через отладку, ни через
    текст ошибки.
    """
    match = _SECRET_COMMANDS.match(text.strip())
    if match:
        return match.group(1) + MASK
    return text
