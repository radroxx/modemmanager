"""Точка входа: разбор аргументов, запуск event loop, корректное завершение."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from .app import Application
from .config import ConfigError

DEFAULT_CONFIG = "settings.json"
DEFAULT_EVENTS = "events.jsonl"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="modemmanager",
        description="Наблюдение за USB-модемами, приём SMS и входящих вызовов",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="путь к settings.json")
    parser.add_argument("--events", default=DEFAULT_EVENTS, help="путь к events.jsonl")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="уровень журналирования",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="проверить настройки и выйти, не запуская обслуживание модемов",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    app = Application(args.config, args.events)
    if args.check_config:
        app.store.load()
        missing = app.store.settings.missing_required()
        if missing:
            print("не заданы обязательные настройки: " + ", ".join(missing), file=sys.stderr)
            return 1
        print("настройки в порядке")
        return 0

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.request_stop)
        except NotImplementedError:  # pragma: no cover -- не Linux
            pass
    try:
        await app.run()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:  # pragma: no cover -- обработано сигналом
        return 0


if __name__ == "__main__":
    sys.exit(main())
