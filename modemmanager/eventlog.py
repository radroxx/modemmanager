"""Журнал событий в формате JSON Lines.

Файл только дополняется, ротация не выполняется (см. event-log spec). Чтение
истории идёт от конца файла с ограничением объёма прочитанного: файл за годы
вырастает до гигабайтов, и полного прохода по нему не должно быть нигде.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .events import Event

log = logging.getLogger(__name__)

#: Размер блока при чтении с конца файла.
TAIL_BLOCK = 64 * 1024
#: Верхняя граница прочитанного за один запрос истории.
TAIL_MAX_BYTES = 8 * 1024 * 1024


class EventLog:
    """Дописывает события в файл и читает последние записи с конца."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = asyncio.Lock()
        #: Сколько байт прочитано последним вызовом ``tail`` -- для тестов.
        self.last_tail_bytes = 0

    def ensure_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    # ------------------------------------------------------------------- запись

    async def append(self, event: Event) -> None:
        """Дописывает одну строку. Ранее записанные строки не изменяются."""
        line = json.dumps(event.to_record(), ensure_ascii=False, separators=(",", ":"))
        async with self._lock:
            await asyncio.to_thread(self._append_line, line)

    def _append_line(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")

    # ------------------------------------------------------------------- чтение

    async def tail(
        self,
        limit: int = 100,
        *,
        imsi: str | None = None,
        types: Iterable[str] | None = None,
        max_bytes: int = TAIL_MAX_BYTES,
    ) -> list[dict[str, Any]]:
        """Возвращает последние записи в порядке от новых к старым."""
        return await asyncio.to_thread(
            self._tail_sync, limit, imsi, set(types) if types else None, max_bytes
        )

    def _tail_sync(
        self,
        limit: int,
        imsi: str | None,
        types: set[str] | None,
        max_bytes: int,
    ) -> list[dict[str, Any]]:
        self.last_tail_bytes = 0
        if limit <= 0:
            return []
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return []
        if size == 0:
            return []

        found: list[dict[str, Any]] = []
        pending = b""  # незавершённое начало строки из предыдущего блока
        position = size
        read_total = 0

        with open(self.path, "rb") as handle:
            while position > 0 and len(found) < limit and read_total < max_bytes:
                block = min(TAIL_BLOCK, position, max_bytes - read_total)
                position -= block
                handle.seek(position)
                chunk = handle.read(block)
                read_total += len(chunk)
                self.last_tail_bytes = read_total

                buffer = chunk + pending
                lines = buffer.split(b"\n")
                # Первый элемент может быть обрезан слева -- дочитаем следующим блоком.
                pending = lines[0] if position > 0 else b""
                complete = lines[1:] if position > 0 else lines

                for raw in reversed(complete):
                    if not raw.strip():
                        continue
                    record = self._parse(raw)
                    if record is None:
                        continue
                    if not self._matches(record, imsi, types):
                        continue
                    found.append(record)
                    if len(found) >= limit:
                        break
        return found

    @staticmethod
    def _parse(raw: bytes) -> dict[str, Any] | None:
        """Повреждённая строка пропускается, чтение продолжается."""
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(record, dict):
            return None
        return record

    @staticmethod
    def _matches(
        record: dict[str, Any], imsi: str | None, types: set[str] | None
    ) -> bool:
        if imsi is not None and record.get("imsi") != imsi:
            return False
        if types is not None and record.get("type") not in types:
            return False
        return True
