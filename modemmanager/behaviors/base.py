"""Контракт поведения семейства модемов.

Всё, что различается между семействами, собрано здесь как набор операций. Любая
другая часть системы работает только с этим контрактом, поэтому поддержка нового
семейства добавляется новой реализацией и её регистрацией -- без правок в
обнаружении, обмене с портом, приёме сообщений и интерфейсе.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..at.errors import AtError, CommandError
from ..at.session import AtSession
from ..values import (
    Identity,
    NetworkCandidate,
    Operator,
    PinAttempts,
    Registration,
    Signal,
    SimState,
    Storage,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Unsolicited:
    """Разобранное незапрошенное сообщение.

    ``kind`` -- что произошло, ``data`` -- разобранные подробности. Семейства
    приводят свои сообщения к общим видам, чтобы приём сообщений и обработка
    вызовов не зависели от модели модема.
    """

    kind: str
    data: dict[str, Any]
    raw: str = ""
    #: Сколько следующих строк принадлежат этому же сообщению.
    continuation: int = 0


class Kind:
    """Виды незапрошенных сообщений, общие для всех семейств."""

    RING = "ring"
    CALLER_ID = "caller_id"
    CALL_ENDED = "call_ended"
    SMS_STORED = "sms_stored"
    SMS_DIRECT = "sms_direct"
    STORAGE_FULL = "storage_full"
    SIGNAL = "signal"
    REGISTRATION = "registration"
    SIM_STATE = "sim_state"
    BOOT = "boot"
    NETWORK_MODE = "network_mode"
    POWER = "power"
    UNKNOWN = "unknown"


class ModemBehavior(ABC):
    """Операции, зависящие от семейства модема."""

    #: Имя семейства для журнала, интерфейса и метрик.
    family: str = "generic"
    #: Присылает ли модем изменения уровня сигнала и сети сам.
    pushes_signal: bool = False
    pushes_registration: bool = False
    #: Незапрошенные сообщения, специфичные для семейства.
    unsolicited_prefixes: tuple[str, ...] = ()
    #: Есть ли программный способ сброса.
    supports_reset: bool = False
    #: Скорости, с которыми имеет смысл пробовать порт.
    baudrates: tuple[int, ...] = (115200,)

    # ------------------------------------------------------------- опознание

    @classmethod
    @abstractmethod
    def matches(cls, identity: Identity, *, hint: str = "") -> bool:
        """Подходит ли семейство модему с таким опознанием.

        ``hint`` -- пара идентификаторов USB. Это только подсказка: за
        универсальным мостом стоит модем, чьи идентификаторы принадлежат мосту, а
        не модему, поэтому решение принимается по ответу модема.
        """

    # ------------------------------------------------------ порты и запуск

    def rank_ports(self, ports: Sequence[str]) -> list[str]:
        """Порядок проверки портов при поиске управляющего.

        По умолчанию -- в том порядке, в каком их перечислила система: угадывать
        по номеру нельзя, у Huawei управляющий порт не всегда первый.
        """
        return list(ports)

    async def initialise(self, session: AtSession) -> None:
        """Приводит модем в рабочее состояние.

        Базовая последовательность: без эха, числовые ошибки, полный набор
        функций, включённые уведомления способом семейства.
        """
        await session.initialise()
        await self._safe(session, "AT+CFUN=1", timeout=15.0)
        await self.enable_notifications(session)

    async def enable_notifications(self, session: AtSession) -> None:
        """Включает незапрошенные уведомления о сообщениях и о вызовах."""
        # Номер вызывающего: без этого о звонке известно только то, что он есть.
        await self._safe(session, "AT+CLIP=1")
        # Двоичный режим сообщений -- обязателен: только он даёт служебный
        # заголовок, без которого многочастные сообщения не собрать.
        await session.execute("AT+CMGF=0")
        # Уведомлять о поступлении, сообщение оставлять в памяти модема.
        await self._safe(session, "AT+CNMI=2,1,0,0,0")
        await self._safe(session, "AT+CREG=1")
        await self._safe(session, "AT+CGREG=1")

    # --------------------------------------------------- незапрошенные события

    def classify(self, line: str) -> Unsolicited:
        """Разбирает незапрошенное сообщение в общий вид."""
        return classify_standard(line)

    # ---------------------------------------------------------------- опрос

    @abstractmethod
    async def read_signal(self, session: AtSession) -> Signal: ...

    @abstractmethod
    async def read_registration(self, session: AtSession) -> Registration: ...

    @abstractmethod
    async def read_operator(self, session: AtSession) -> Operator: ...

    @abstractmethod
    async def read_pin_attempts(self, session: AtSession) -> PinAttempts: ...

    @abstractmethod
    async def read_storage(self, session: AtSession) -> Storage: ...

    @abstractmethod
    async def read_sim_state(self, session: AtSession) -> tuple[SimState, str]:
        """Состояние SIM и исходный ответ модема."""

    # ------------------------------------------------------------ управление

    async def reset(self, session: AtSession) -> None:
        """Программный сброс модема. Поднимает ``AtError``, если его нет."""
        raise AtError(f"{self.family}: программный сброс не поддерживается")

    async def scan_networks(
        self, session: AtSession, *, timeout: float = 120.0
    ) -> list[NetworkCandidate]:
        """Поиск доступных сетей. Занимает десятки секунд."""
        response = await session.execute("AT+COPS=?", timeout=timeout)
        return parse_cops_scan(response.text)

    # -------------------------------------------------------- вспомогательное

    async def _safe(self, session: AtSession, command: str, *, timeout: float = 5.0) -> None:
        """Выполняет команду, для которой отказ модема не является ошибкой.

        Часть команд поддерживается не всеми прошивками, и отказ на них не должен
        мешать обслуживанию: без `AT+CREG=1` останется регулярный опрос.
        """
        try:
            await session.execute(command, timeout=timeout)
        except CommandError as exc:
            log.debug("%s: %s отклонена модемом (%s)", session.port, command, exc.final)

    def __repr__(self) -> str:  # pragma: no cover -- диагностика
        return f"<{type(self).__name__} family={self.family}>"


# --------------------------------------------------------------------- разбор

_CLIP = re.compile(r'^\+CLIP:\s*"([^"]*)"(?:,(\d+))?', re.IGNORECASE)
_CMTI = re.compile(r'^\+CMTI:\s*"?([A-Za-z]*)"?\s*,\s*(\d+)', re.IGNORECASE)
_CMT = re.compile(r"^\+CMT:", re.IGNORECASE)
_CREG_EVENT = re.compile(r"^\+(CREG|CGREG|CEREG):\s*(\d+)(?:,(.*))?$", re.IGNORECASE)
_CPIN_EVENT = re.compile(r"^\+CPIN:\s*(.+)$", re.IGNORECASE)
_CSQ_EVENT = re.compile(r"^\+CSQN?:\s*(\d+)(?:,(\d+))?", re.IGNORECASE)


def classify_standard(line: str) -> Unsolicited:
    """Разбор незапрошенных сообщений, общих для всех семейств."""
    upper = line.upper()

    if upper.startswith("RING") or upper.startswith("+CRING:"):
        return Unsolicited(Kind.RING, {}, raw=line)

    match = _CLIP.match(line)
    if match:
        number = match.group(1)
        return Unsolicited(
            Kind.CALLER_ID,
            {"number": number, "hidden": not number},
            raw=line,
        )

    if upper.startswith("NO CARRIER") or upper.startswith("+CEND:"):
        return Unsolicited(Kind.CALL_ENDED, {}, raw=line)

    match = _CMTI.match(line)
    if match:
        return Unsolicited(
            Kind.SMS_STORED,
            {"storage": match.group(1) or "", "index": int(match.group(2))},
            raw=line,
        )

    if _CMT.match(line):
        # За строкой уведомления идёт строка с самим сообщением.
        return Unsolicited(Kind.SMS_DIRECT, {}, raw=line, continuation=1)

    match = _CREG_EVENT.match(line)
    if match:
        return Unsolicited(
            Kind.REGISTRATION,
            {"domain": match.group(1).upper(), "stat": int(match.group(2))},
            raw=line,
        )

    match = _CPIN_EVENT.match(line)
    if match:
        return Unsolicited(Kind.SIM_STATE, {"value": match.group(1).strip()}, raw=line)

    match = _CSQ_EVENT.match(line)
    if match:
        return Unsolicited(
            Kind.SIGNAL,
            {"rssi": int(match.group(1))},
            raw=line,
        )

    if upper in ("RDY", "CALL READY", "SMS READY", "+CFUN: 1"):
        return Unsolicited(Kind.BOOT, {"stage": upper}, raw=line)

    if "POWER DOWN" in upper or "VOLTAGE" in upper:
        return Unsolicited(Kind.POWER, {"detail": line}, raw=line)

    return Unsolicited(Kind.UNKNOWN, {}, raw=line)


_CSQ_RESPONSE = re.compile(r"^\+CSQ:\s*(\d+)\s*,\s*(\d+)", re.IGNORECASE)


def parse_csq(text: str) -> Signal:
    """Разбирает ответ на `AT+CSQ`."""
    for line in text.splitlines():
        match = _CSQ_RESPONSE.match(line.strip())
        if match:
            return Signal.from_csq(int(match.group(1)), int(match.group(2)))
    return Signal.unknown()


_CREG_RESPONSE = re.compile(
    r"^\+(CREG|CGREG|CEREG):\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*\"?([0-9A-Fa-f]*)\"?)?"
    r"(?:\s*,\s*\"?([0-9A-Fa-f]*)\"?)?",
    re.IGNORECASE,
)


def parse_creg(text: str) -> tuple[int | None, str, str]:
    """Разбирает ответ на `AT+CREG?`/`AT+CGREG?`: код состояния, зона, сота."""
    for line in text.splitlines():
        match = _CREG_RESPONSE.match(line.strip())
        if match:
            return (int(match.group(3)), match.group(4) or "", match.group(5) or "")
    return (None, "", "")


_COPS_RESPONSE = re.compile(
    r'^\+COPS:\s*(\d+)(?:\s*,\s*(\d+)\s*,\s*"?([^",]*)"?(?:\s*,\s*(\d+))?)?',
    re.IGNORECASE,
)


def parse_cops(text: str) -> tuple[int | None, int | None, str, str]:
    """Разбирает ответ на `AT+COPS?`: режим, формат, оператор, технология."""
    for line in text.splitlines():
        match = _COPS_RESPONSE.match(line.strip())
        if match:
            mode = int(match.group(1))
            fmt = int(match.group(2)) if match.group(2) else None
            return (mode, fmt, (match.group(3) or "").strip(), match.group(4) or "")
    return (None, None, "", "")


_SCAN_ENTRY = re.compile(r"\((\d+)\s*,\s*(.*?)\)")


def parse_cops_scan(text: str) -> list[NetworkCandidate]:
    """Разбирает результат поиска сетей `AT+COPS=?`.

    Формат: ``(<stat>,"<long>","<short>","<numeric>"[,<AcT>])``. Хвост ответа
    содержит списки поддерживаемых режимов вида ``(0-4)`` -- их нужно отбросить,
    иначе они попадут в список сетей как несуществующие операторы.
    """
    candidates: list[NetworkCandidate] = []
    for raw in _SCAN_ENTRY.findall(text):
        status = int(raw[0])
        fields = [field.strip().strip('"') for field in raw[1].split(",")]
        if not fields or not fields[0]:
            continue
        long_name = fields[0] if len(fields) > 0 else ""
        numeric = ""
        technology = ""
        # Числовой код -- первое поле, состоящее только из цифр и длиной 5-6.
        for field in fields:
            if field.isdigit() and 5 <= len(field) <= 6:
                numeric = field
                break
        if len(fields) >= 4 and fields[3].isdigit() and len(fields[3]) <= 2:
            technology = fields[3]
        elif len(fields) >= 5 and fields[4].isdigit():
            technology = fields[4]
        if not numeric:
            continue
        candidates.append(
            NetworkCandidate(
                plmn=numeric,
                name=long_name if not long_name.isdigit() else "",
                status=status,
                technology=technology,
            )
        )
    return candidates


_CPMS_RESPONSE = re.compile(
    r'^\+CPMS:\s*"?([A-Za-z]*)"?\s*,\s*(\d+)\s*,\s*(\d+)',
    re.IGNORECASE,
)


def parse_cpms(text: str) -> Storage:
    """Разбирает ответ на `AT+CPMS?`: имя хранилища, занято, всего."""
    for line in text.splitlines():
        match = _CPMS_RESPONSE.match(line.strip())
        if match:
            return Storage(
                name=match.group(1),
                used=int(match.group(2)),
                total=int(match.group(3)),
            )
    return Storage()


_CPIN_RESPONSE = re.compile(r"^\+CPIN:\s*(.+)$", re.IGNORECASE)


def parse_cpin(text: str) -> tuple[SimState, str]:
    """Разбирает ответ на `AT+CPIN?`."""
    for line in text.splitlines():
        match = _CPIN_RESPONSE.match(line.strip())
        if match:
            return (sim_state_from_cpin(match.group(1).strip()), match.group(1).strip())
    return (SimState.UNKNOWN, text.strip())


def sim_state_from_cpin(value: str) -> SimState:
    upper = value.upper()
    if "READY" in upper:
        return SimState.READY
    if "PUK" in upper:
        return SimState.PUK_REQUIRED
    if "PIN" in upper:
        return SimState.PIN_REQUIRED
    if "NOT INSERTED" in upper or "ABSENT" in upper:
        return SimState.ABSENT
    return SimState.UNKNOWN


#: Коды ошибок `+CME ERROR`, означающие отсутствие карты.
CME_SIM_ABSENT = frozenset({10, 13})
#: Коды, означающие, что карта требует PIN или PUK.
CME_SIM_PIN = frozenset({11, 12})


def sim_state_from_error(exc: CommandError) -> SimState:
    """Определяет состояние SIM по коду ошибки, когда `AT+CPIN?` отказал."""
    if exc.code in CME_SIM_ABSENT:
        return SimState.ABSENT
    if exc.code == 11:
        return SimState.PIN_REQUIRED
    if exc.code == 12:
        return SimState.PUK_REQUIRED
    return SimState.UNKNOWN


_ATI = re.compile(r"^(Manufacturer|Model|Revision|IMEI)\s*:\s*(.*)$", re.IGNORECASE)


def parse_identity(text: str, *, imei: str = "") -> Identity:
    """Собирает опознание модема из ответов на `ATI`, `AT+CGMI`, `AT+CGMM`.

    Huawei отвечает на `ATI` полями с подписями, SIM800 -- просто строками, поэтому
    разбираются оба вида.
    """
    fields: dict[str, str] = {}
    plain: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _ATI.match(line)
        if match:
            fields[match.group(1).lower()] = match.group(2).strip()
        else:
            plain.append(line)
    manufacturer = fields.get("manufacturer", "")
    model = fields.get("model", "")
    if not manufacturer and plain:
        manufacturer = plain[0]
    if not model and len(plain) > 1:
        model = plain[1]
    return Identity(
        manufacturer=manufacturer,
        model=model,
        revision=fields.get("revision", ""),
        imei=imei or fields.get("imei", ""),
        raw=text.strip(),
    )
