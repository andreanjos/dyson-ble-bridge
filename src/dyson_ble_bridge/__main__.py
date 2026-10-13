"""Command line entry point: ``dyson-ble-bridge --config /path/config.json``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import TextIO

from .bridge import Bridge, acquire_single_instance_lock
from .config import Config, ConfigError

DEFAULT_CONFIG = Path(
    os.environ.get("DYSON_BRIDGE_CONFIG", "~/dyson-bridge/config.json")
).expanduser()
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dyson-ble-bridge",
        description="Bridge Dyson BLE lamps to Home Assistant over MQTT discovery.",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="configuration file")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="log verbosity (default INFO)",
    )
    parser.add_argument("--check", action="store_true", help="validate the configuration and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format=LOG_FORMAT, datefmt="%H:%M:%S")

    try:
        config = Config.load(args.config)
    except ConfigError as err:
        print(f"configuration error: {err}", file=sys.stderr)
        return 2

    if args.check:
        lamps = ", ".join(f"{lamp.serial} ({lamp.name})" for lamp in config.lamps)
        print(f"configuration OK — {len(config.lamps)} lamp(s): {lamps}")
        return 0

    lock: TextIO
    try:
        lock = acquire_single_instance_lock()  # type: ignore[assignment]
    except RuntimeError as err:
        print(err, file=sys.stderr)
        return 1
    try:
        return asyncio.run(Bridge(config).run())
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
