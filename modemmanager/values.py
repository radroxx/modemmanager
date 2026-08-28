"""Значения, которыми обмениваются части системы.

Собраны отдельно, чтобы поведение семейства модемов возвращало одно и то же
независимо от того, какими командами оно это получило: потребители (метрики,
интерфейс, уведомления) не должны знать про семейства.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: Значение, которым модемы обозначают «уровень неизвестен» в `+CSQ`.
CSQ_UNKNOWN = 99
CSQ_MIN_DBM = -113
CSQ_MAX_DBM = -51

#: Число делений в грубой шкале. Совпадает с шкалой, в которой модемы сообщают
#: уровень в `+CIEV`, чтобы деление не менялось при переводе в дБм и обратно.
BARS_SCALE = 5


def _bars_to_dbm(value: int, scale: int = BARS_SCALE) -> int:
    """Середина участка шкалы, которому соответствует деление."""
    return round(CSQ_MIN_DBM + (CSQ_MAX_DBM - CSQ_MIN_DBM) * value / scale)


class SimState(str, Enum):
    """Состояние SIM-карты с точки зрения модема."""

    UNKNOWN = "unknown"
    ABSENT = "absent"
    PIN_REQUIRED = "pin_required"
    PUK_REQUIRED = "puk_required"
    READY = "ready"


class RegistrationState(str, Enum):
    """Состояние регистрации в сети (по `+CREG`/`+CGREG`)."""

    UNKNOWN = "unknown"
    NOT_REGISTERED = "not_registered"
    REGISTERED = "registered"
    SEARCHING = "searching"
    DENIED = "denied"
    ROAMING = "roaming"


#: Числовые коды `<stat>` из `+CREG`/`+CGREG`.
_REGISTRATION_CODES = {
    0: RegistrationState.NOT_REGISTERED,
    1: RegistrationState.REGISTERED,
    2: RegistrationState.SEARCHING,
    3: RegistrationState.DENIED,
    4: RegistrationState.UNKNOWN,
    5: RegistrationState.ROAMING,
}


def registration_state(code: int) -> RegistrationState:
    return _REGISTRATION_CODES.get(code, RegistrationState.UNKNOWN)


@dataclass(frozen=True)
class Signal:
    """Уровень сигнала в единой шкале.

    ``dbm is None`` означает «неизвестно» и не заменяется числом: подстановка
    нуля или минимума шкалы сделала бы отсутствие данных неотличимым от плохого
    приёма.
    """

    dbm: int | None = None
    #: Исходное значение `<rssi>`, как его сообщил модем (для диагностики).
    raw: int | None = None
    #: Качество канала `<ber>`, если модем его сообщил.
    quality: int | None = None

    @property
    def known(self) -> bool:
        return self.dbm is not None

    @classmethod
    def unknown(cls) -> Signal:
        return cls()

    @classmethod
    def from_csq(cls, rssi: int, quality: int | None = None) -> Signal:
        """Переводит `<rssi>` из `+CSQ` (0..31) в дБм."""
        if rssi == CSQ_UNKNOWN or not 0 <= rssi <= 31:
            return cls(dbm=None, raw=rssi, quality=quality)
        return cls(dbm=CSQ_MIN_DBM + 2 * rssi, raw=rssi, quality=quality)

    @classmethod
    def from_bars(cls, value: int, scale: int = BARS_SCALE) -> Signal:
        """Переводит грубую шкалу «палочек» (например, `+CIEV`) в дБм.

        Точность такого источника ниже, чем у `+CSQ`, поэтому значение
        приводится к середине соответствующего участка шкалы: лучше честная
        оценка, чем отсутствие данных между опросами.
        """
        if scale <= 0 or value < 0 or value > scale:
            return cls(dbm=None, raw=value)
        if value == 0:
            return cls(dbm=CSQ_MIN_DBM, raw=value)
        return cls(dbm=_bars_to_dbm(value, scale), raw=value)

    @property
    def bars(self) -> int | None:
        """Грубая шкала 0..5 для интерфейса.

        Границы совпадают с обратным переводом ``from_bars``: деление, которое
        сообщил модем, не меняется при показе.
        """
        if self.dbm is None:
            return None
        for value in range(BARS_SCALE, 0, -1):
            if self.dbm >= _bars_to_dbm(value):
                return value
        return 0


@dataclass(frozen=True)
class Registration:
    """Состояние регистрации отдельно для голоса с сообщениями и для данных."""

    voice: RegistrationState = RegistrationState.UNKNOWN
    data: RegistrationState = RegistrationState.UNKNOWN
    #: Код зоны и соты, если модем их сообщил (полезно при разборе роуминга).
    area: str = ""
    cell: str = ""

    @property
    def roaming(self) -> bool:
        return RegistrationState.ROAMING in (self.voice, self.data)

    @property
    def usable_for_sms(self) -> bool:
        """Достаточно ли регистрации, чтобы сообщение могло прийти."""
        return self.voice in (RegistrationState.REGISTERED, RegistrationState.ROAMING)


@dataclass(frozen=True)
class Operator:
    """Выбранный оператор."""

    #: Код сети в виде MCCMNC, например ``25002``.
    plmn: str = ""
    name: str = ""
    #: ``True`` -- выбор сети ручной, ``False`` -- автоматический.
    manual: bool = False
    #: Технология доступа, как её сообщил модем (``<AcT>``).
    technology: str = ""

    @property
    def known(self) -> bool:
        return bool(self.plmn or self.name)

    @property
    def label(self) -> str:
        if self.name and self.plmn:
            return f"{self.name} ({self.plmn})"
        return self.name or self.plmn or "неизвестен"


@dataclass(frozen=True)
class PinAttempts:
    """Остаток попыток ввода PIN-кода.

    ``pin is None`` -- остаток неизвестен. Это отдельный исход, а не ноль:
    неизвестный остаток запрещает попытку ввода так же, как исчерпанный, но по
    другой причине (см. sim-management).
    """

    pin: int | None = None
    puk: int | None = None
    #: Команда, которой удалось прочитать остаток (для журнала и диагностики).
    source: str = ""
    #: Почему остаток неизвестен, если он неизвестен.
    reason: str = ""

    @property
    def known(self) -> bool:
        return self.pin is not None


@dataclass(frozen=True)
class Storage:
    """Заполненность памяти сообщений."""

    used: int | None = None
    total: int | None = None
    #: Имя хранилища, как его называет модем (``SM``, ``ME``, ...).
    name: str = ""

    @property
    def known(self) -> bool:
        return self.used is not None and self.total is not None

    @property
    def full(self) -> bool:
        return self.known and self.used >= self.total

    @property
    def free(self) -> int | None:
        if not self.known:
            return None
        return max(0, self.total - self.used)


@dataclass(frozen=True)
class Identity:
    """Опознание модема по ответам на пробу."""

    manufacturer: str = ""
    model: str = ""
    revision: str = ""
    imei: str = ""
    #: Полный текст ответа на пробу -- для диагностики неопознанных модемов.
    raw: str = ""

    @property
    def description(self) -> str:
        parts = [part for part in (self.manufacturer, self.model) if part]
        return " ".join(parts) or "неизвестная модель"


@dataclass(frozen=True)
class NetworkCandidate:
    """Один результат поиска доступных сетей."""

    plmn: str
    name: str = ""
    #: ``0`` неизвестен, ``1`` доступна, ``2`` текущая, ``3`` запрещена.
    status: int = 0
    technology: str = ""

    @property
    def forbidden(self) -> bool:
        return self.status == 3
