"""Опознание SIM-карты и модема по IMSI.

Читает IMSI (``AT+CIMI``) и IMEI, формирует человекочитаемое имя карты.
На используемом парке модемов альтернативные команды чтения идентификатора
SIM возвращают ``ERROR``, а ``AT+CIMI`` отдаёт IMSI ещё до ввода PIN-кода --
именно на этом строится опознание. Альтернативной команды нет: если модем
не отдал IMSI, карта считается неопознанной и обслуживание требует
вмешательства администратора.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ..at.errors import AtError, CommandError
from ..at.session import AtSession
from ..behaviors.base import ModemBehavior

log = logging.getLogger(__name__)


#: MCC (первые три цифры IMSI) → двухбуквенный код страны.
#: Список короткий: сюда попадают только те страны, чьи SIM реально могут
#: оказаться в этих модемах. Незнакомый MCC даёт числовой код -- это
#: честнее, чем ошибиться в двухбуквенном обозначении.
_MCC_TO_COUNTRY: dict[str, str] = {
    "250": "RU",
    "255": "UA",
    "257": "BY",
    "401": "KZ",
    "283": "AM",
    "400": "AZ",
    "437": "KG",
    "434": "UZ",
    "436": "TJ",
    "438": "TM",
    "259": "MD",
    "282": "GE",
    "260": "PL",
    "262": "DE",
    "234": "GB",
    "235": "GB",
    "208": "FR",
    "222": "IT",
    "214": "ES",
    "204": "NL",
    "247": "LV",
    "248": "EE",
    "246": "LT",
    "286": "TR",
    "310": "US",
    "311": "US",
    "312": "US",
    "313": "US",
    "314": "US",
    "315": "US",
    "316": "US",
    "460": "CN",
    "461": "CN",
}


@dataclass(frozen=True)
class SimIdentity:
    """Что известно о SIM-карте по её IMSI."""

    imsi: str
    country: str = ""
    country_code: str = ""
    tail: str = ""

    @property
    def auto_label(self) -> str:
        return format_auto_label(self)


# --------------------------------------------------------------------- нормализация

_IMSI_LINE = re.compile(r"([0-9]{6,15})")


def normalise(raw: str) -> str:
    """Оставляет от строки только цифры и возвращает их, если длина 6..15.

    IMSI по 3GPP TS 23.003 -- строка из 6..15 цифр (обычно 15). Заполнителей
    у IMSI нет, но модем может вернуть значение в кавычках или с прицепленным
    ``OK``; берём первую подстроку из 6..15 подряд идущих цифр.
    """
    if not raw:
        return ""
    for candidate in raw.strip().splitlines():
        line = candidate.strip()
        if not line or line.upper() == "OK":
            continue
        match = _IMSI_LINE.search(line)
        if match:
            return match.group(1)
    return ""


# --------------------------------------------------------------------- страна

def _country_from_imsi(imsi: str) -> tuple[str, str]:
    """Возвращает (двухбуквенный код или ``""``, MCC или ``""``)."""
    if len(imsi) < 3:
        return ("", "")
    mcc = imsi[:3]
    return (_MCC_TO_COUNTRY.get(mcc, ""), mcc)


def _tail(imsi: str, length: int = 5) -> str:
    """Хвост MSIN -- цифры после MCC, максимум ``length`` последних."""
    if not imsi or len(imsi) <= 3:
        return ""
    msin = imsi[3:]
    return msin[-length:]


def format_auto_label(identity: SimIdentity) -> str:
    """Автоимя SIM из IMSI.

    ``RU-...12345`` -- код страны и последние цифры MSIN. Если страну определить
    не удалось, вместо неё стоит числовой MCC или ``SIM`` -- это по-прежнему
    связывает имя с идентификатором, а не остаётся пустой строкой.
    """
    country = identity.country or identity.country_code or "SIM"
    tail = identity.tail or _tail(identity.imsi)
    if not tail:
        return f"{country}-?"
    return f"{country}-...{tail}"


def auto_label(imsi: str) -> str:
    """Автоимя SIM для короткого пути без разбора: ``auto_label(imsi)``."""
    if not imsi:
        return ""
    country, code = _country_from_imsi(imsi)
    return format_auto_label(
        SimIdentity(
            imsi=imsi,
            country=country,
            country_code=code,
            tail=_tail(imsi),
        )
    )


def from_imsi(imsi: str) -> SimIdentity:
    """Собирает опознание карты из её IMSI без обращения к модему."""
    imsi = normalise(imsi)
    country, code = _country_from_imsi(imsi)
    return SimIdentity(
        imsi=imsi,
        country=country,
        country_code=code,
        tail=_tail(imsi),
    )


# --------------------------------------------------------------------- чтение


async def read_imsi(session: AtSession) -> str:
    """Читает IMSI командой ``AT+CIMI``.

    На эксплуатируемом парке модемов ``AT+CIMI`` отдаёт IMSI ещё до ввода
    PIN-кода. Альтернативной команды не пробуем: она либо не поддержана, либо
    даёт ``ERROR`` -- see design.md, D1.
    """
    try:
        response = await session.execute("AT+CIMI")
    except CommandError as exc:
        log.debug("%s: AT+CIMI отклонена (%s)", session.port, exc.final)
        return ""
    except AtError as exc:
        log.debug("%s: AT+CIMI не выполнена (%s)", session.port, exc)
        return ""
    return normalise(response.text)


_IMEI = re.compile(r"^(\d{14,17})$")


async def read_imei(session: AtSession) -> str:
    """Читает IMEI модема, если он ещё не был получен на пробе."""
    try:
        response = await session.execute("AT+CGSN")
    except AtError as exc:
        log.debug("%s: AT+CGSN не выполнена (%s)", session.port, exc)
        return ""
    for raw in response.text.splitlines():
        line = raw.strip()
        match = _IMEI.match(line)
        if match:
            return match.group(1)
    return ""


async def identify(
    session: AtSession, behavior: ModemBehavior | None = None, *, imei: str = ""
) -> SimIdentity:
    """Возвращает опознание SIM: читает IMSI и собирает вспомогательные поля.

    ``behavior`` пока не влияет на чтение (все семейства понимают ``AT+CIMI``),
    но принимается в аргументах, чтобы будущее семейство со своей командой
    подключилось без правок в вызывающем коде.
    """
    del behavior  # семейство пока не участвует в чтении IMSI
    del imei  # IMEI берётся у модема, не у карты
    imsi = await read_imsi(session)
    return from_imsi(imsi)
