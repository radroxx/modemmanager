"""Сборка многочастных сообщений.

Каждая пришедшая часть удерживается в памяти до тех пор, пока не соберутся все
или пока не истечёт время ожидания. Декодирование делается один раз -- после
объединения канонических данных всех частей: если часть декодировать отдельно,
разрезанные границей символы получатся сломанными (см. design.md, D7).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .pdu import Address, Deliver, Encoding, decode_payload

log = logging.getLogger(__name__)


#: Ключ группы сборки. Восьмибитный ``ref`` переиспользуется, поэтому одного
#: только ``ref`` недостаточно, чтобы отличить два сообщения одного отправителя
#: (см. design.md, D9).
@dataclass(frozen=True)
class GroupKey:
    """Ключ группы сборки: одна SIM, один отправитель, одно многочастное."""

    imsi: str
    sender: str
    reference: int
    total: int
    reference_16bit: bool = False


@dataclass
class _Part:
    seq: int
    payload: bytes
    raw: str


@dataclass
class _Group:
    """Живая группа сборки."""

    key: GroupKey
    sender: Address
    encoding: Encoding
    #: Момент прихода первой части -- отсюда отсчитывается таймаут.
    started_at: float
    #: Момент прихода первой части в стенных часах -- нужен для записи журнала.
    first_wall_time: float
    parts: dict[int, _Part] = field(default_factory=dict)

    def is_full(self) -> bool:
        return len(self.parts) >= self.key.total

    def missing(self) -> list[int]:
        seen = set(self.parts)
        return [seq for seq in range(1, self.key.total + 1) if seq not in seen]

    def payloads_in_order(self) -> tuple[bytes, list[str]]:
        """Возвращает конкатенированные канонические данные и сырьё в порядке seq."""
        merged = bytearray()
        raw: list[str] = []
        for seq in sorted(self.parts):
            merged.extend(self.parts[seq].payload)
            raw.append(self.parts[seq].raw)
        return (bytes(merged), raw)


@dataclass(frozen=True)
class Assembled:
    """Результат прибытия части: сообщение либо собрано, либо ждёт остальных."""

    key: GroupKey
    sender: Address
    encoding: Encoding
    text: str
    #: Момент прихода первой части -- то, к чему привязана строка в журнале.
    timestamp: float
    complete: bool
    #: Номера пришедших частей в возрастающем порядке.
    parts_present: tuple[int, ...]
    #: Номера отсутствующих частей (для события неполноты).
    parts_missing: tuple[int, ...]
    #: Сырые PDU всех имеющихся частей в порядке ``seq`` (см. spec, 6.8).
    raw_pdus: tuple[str, ...]

    @property
    def incomplete(self) -> bool:
        return not self.complete

    @property
    def is_single(self) -> bool:
        return self.key.total <= 1


class Assembler:
    """Собирает многочастные сообщения по ключу группы с таймаутом."""

    def __init__(
        self,
        timeout: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ):
        self.timeout = timeout
        self._clock = clock
        self._wall = wall_clock
        self._groups: dict[GroupKey, _Group] = {}

    # ------------------------------------------------------------------ приём

    def add(self, imsi: str, deliver: Deliver, *, raw: str | None = None) -> Assembled | None:
        """Добавляет часть; возвращает ``Assembled``, когда сообщение готово.

        Однократное сообщение возвращает ``Assembled`` немедленно. Многочастное
        сообщение возвращает ``None``, пока не пришли все части (или пока не
        истечёт таймаут -- тогда за неполноту отвечает ``expire``).

        ``raw`` -- исходный текст PDU в hex; используется в журнале. Если не
        передан, берётся ``deliver.raw``.
        """
        pdu_raw = raw if raw is not None else deliver.raw
        concat = deliver.concat
        if concat is None or concat.total <= 1:
            text = decode_payload(deliver.payload, deliver.encoding)
            key = GroupKey(imsi=imsi, sender=deliver.sender.display, reference=0, total=1)
            return Assembled(
                key=key,
                sender=deliver.sender,
                encoding=deliver.encoding,
                text=text,
                timestamp=self._wall(),
                complete=True,
                parts_present=(1,),
                parts_missing=(),
                raw_pdus=(pdu_raw,),
            )

        key = GroupKey(
            imsi=imsi,
            sender=deliver.sender.display,
            reference=concat.reference,
            total=concat.total,
            reference_16bit=concat.reference_16bit,
        )
        group = self._groups.get(key)
        now = self._clock()
        if group is not None and now - group.started_at >= self.timeout:
            # Группа просрочена и ждёт снятия в ``expire``. Приходящая часть с
            # тем же ключом принадлежит уже другому сообщению -- ключ будет
            # переиспользован после снятия старой группы. Здесь просто не
            # смешиваем её со старыми частями.
            group = None

        if group is None:
            group = _Group(
                key=key,
                sender=deliver.sender,
                encoding=deliver.encoding,
                started_at=now,
                first_wall_time=self._wall(),
            )
            self._groups[key] = group

        if concat.seq in group.parts:
            # Повторная часть игнорируется: индексируем, а не дописываем.
            log.debug(
                "часть %d сообщения %s пришла повторно, игнорируем",
                concat.seq,
                key,
            )
            return None
        if not 1 <= concat.seq <= concat.total:
            log.warning(
                "часть %d вне диапазона 1..%d, игнорируем",
                concat.seq,
                concat.total,
            )
            return None
        group.parts[concat.seq] = _Part(
            seq=concat.seq,
            payload=deliver.payload,
            raw=pdu_raw,
        )

        if group.is_full():
            del self._groups[key]
            return self._assemble(group, complete=True)
        return None

    # ------------------------------------------------------------------ таймауты

    def expire(self, *, deadline: float | None = None) -> list[Assembled]:
        """Снимает группы, у которых истекло время ожидания.

        Возвращает результат в виде списка ``Assembled`` с ``complete=False`` --
        вызывающий формирует по ним события неполноты.
        """
        expired: list[Assembled] = []
        now = self._clock() if deadline is None else deadline
        for key in list(self._groups):
            group = self._groups[key]
            if now - group.started_at < self.timeout:
                continue
            del self._groups[key]
            expired.append(self._assemble(group, complete=False))
        return expired

    def next_deadline(self) -> float | None:
        """Когда ближайшая группа станет неполной, в единицах ``clock``."""
        if not self._groups:
            return None
        oldest = min(group.started_at for group in self._groups.values())
        return oldest + self.timeout

    # ------------------------------------------------------------------ утилиты

    def pending_keys(self) -> list[GroupKey]:
        return list(self._groups)

    def _assemble(self, group: _Group, *, complete: bool) -> Assembled:
        merged, raw_pdus = group.payloads_in_order()
        text = decode_payload(merged, group.encoding)
        parts_present = tuple(sorted(group.parts))
        parts_missing = tuple(group.missing())
        return Assembled(
            key=group.key,
            sender=group.sender,
            encoding=group.encoding,
            text=text,
            timestamp=group.first_wall_time,
            complete=complete,
            parts_present=parts_present,
            parts_missing=parts_missing,
            raw_pdus=tuple(raw_pdus),
        )
