"""Runtime: supervise one BLE link per lamp and mirror it into Home Assistant via MQTT."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import random
import signal
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from . import ha
from .config import Config, LampConfig
from .ha import Command, CommandError, LampState, parse_command
from .link import LampLink, Transport

_LOGGER = logging.getLogger(__name__)

LOCK_PATH = Path(os.environ.get("DYSON_BRIDGE_LOCK", "/run/dyson-ble-bridge.lock"))
CONNECT_STAGGER = 3.0  # one adapter, one connect procedure at a time
MONITOR_INTERVAL = 5.0
FULL_REFRESH_INTERVAL = 60.0
BACKOFF_BASE, BACKOFF_FACTOR, BACKOFF_MAX = 3.0, 1.6, 60.0
WATCHDOG_INTERVAL = 20.0


def acquire_single_instance_lock(path: Path = LOCK_PATH) -> object:
    """One bridge per host: two instances fight over the same lamp links."""
    handle = path.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as err:
        handle.close()
        raise RuntimeError(f"another bridge instance holds {path}") from err
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def notify_systemd(state: str) -> None:
    """``sd_notify`` without a dependency (READY=1 / WATCHDOG=1)."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(state.encode())
    except OSError as err:
        _LOGGER.debug("sd_notify(%s) failed: %s", state, err)


@dataclass(slots=True)
class LampStats:
    """Per-lamp counters, surfaced by the status endpoint."""

    connects: int = 0
    failures: int = 0
    last_error: str | None = None
    last_change: float | None = None


class LampSupervisor:
    """Keeps one lamp connected, publishes its state, applies its commands."""

    def __init__(
        self,
        cfg: LampConfig,
        account_uuid: str,
        mqtt_client: mqtt.Client,
        *,
        index: int,
        loop: asyncio.AbstractEventLoop,
        transport_factory: Callable[..., Transport] | None = None,
    ) -> None:
        self.cfg = cfg
        self.mqtt = mqtt_client
        self.index = index
        self.loop = loop
        self.link = LampLink(
            cfg,
            account_uuid,
            transport_factory=transport_factory,
            on_state=self._state_changed,
            on_motion=self._motion_changed,
            on_link_lost=self._link_lost,
        )
        self.stats = LampStats()

    # ── MQTT ─────────────────────────────────────────────────────────────────

    def publish(
        self,
        topic: str,
        payload: str | bytes | int | float | dict[str, object] | list[object] | None,
        *,
        retain: bool = True,
    ) -> None:
        body = json.dumps(payload) if isinstance(payload, (dict, list)) else payload
        self.mqtt.publish(topic, body, qos=1, retain=retain)

    def announce(self) -> None:
        """(Re)publish discovery, availability and state — idempotent."""
        for topic, payload in (ha.light_discovery(self.cfg), ha.motion_discovery(self.cfg)):
            self.publish(topic, payload)
        self.publish(
            f"{self.cfg.base_topic}/availability",
            "online" if self.link.is_connected else "offline",
        )
        self._publish_state()

    def _publish_state(self) -> None:
        self.publish(f"{self.cfg.base_topic}/state", self.link.state.payload())

    def _state_changed(self, _state: LampState) -> None:
        self.stats.last_change = time.time()
        self._publish_state()

    def _motion_changed(self, motion: bool) -> None:
        self.publish(f"{self.cfg.base_topic}/motion", "ON" if motion else "OFF", retain=False)

    def _link_lost(self) -> None:
        self.stats.last_error = "link lost"
        self.publish(f"{self.cfg.base_topic}/availability", "offline")
        _LOGGER.warning("%s: link lost", self.cfg.serial)

    def on_message(self, payload: bytes) -> None:
        """Called from the paho network thread; hops onto the event loop."""
        try:
            command = parse_command(payload)
        except CommandError as err:
            _LOGGER.warning("%s: ignoring command: %s", self.cfg.serial, err)
            return
        asyncio.run_coroutine_threadsafe(self._apply(command), self.loop)

    async def _apply(self, command: Command) -> None:
        if not self.link.is_connected:
            _LOGGER.warning("%s: command while offline, dropped", self.cfg.serial)
            return
        try:
            changed = await self.link.apply(command)
        except Exception as err:
            self.stats.last_error = f"{type(err).__name__}: {err}"
            _LOGGER.warning("%s: command failed: %s", self.cfg.serial, err)
            return
        if changed:
            _LOGGER.info("%s: %s -> %s", self.cfg.serial, command, self.link.state)

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        await asyncio.sleep(CONNECT_STAGGER * self.index)
        self.announce()
        failures = 0
        while True:
            try:
                await self.link.connect()
                failures = 0
                self.stats.connects += 1
                self.stats.last_error = None
                self.publish(f"{self.cfg.base_topic}/availability", "online")
                self._publish_state()
                _LOGGER.info("%s: connected (%s)", self.cfg.serial, self.link.state)
                await self._monitor()
            except asyncio.CancelledError:
                await self.link.disconnect()
                raise
            except Exception as err:
                failures += 1
                self.stats.failures += 1
                self.stats.last_error = f"{type(err).__name__}: {err}"
                delay = min(BACKOFF_MAX, BACKOFF_BASE * BACKOFF_FACTOR ** min(failures, 6))
                delay *= random.uniform(0.85, 1.15)  # no synchronized retry storms
                _LOGGER.warning(
                    "%s: %s: %s — retrying in %.1fs",
                    self.cfg.serial,
                    type(err).__name__,
                    err,
                    delay,
                )
                await self.link.disconnect()
                await asyncio.sleep(delay)

    async def _monitor(self) -> None:
        """Hold the link open and reconcile state against the device."""
        last_full_refresh = time.time()
        while self.link.is_connected and not self.link.link_lost:
            await asyncio.sleep(MONITOR_INTERVAL)
            if not self.link.is_connected or self.link.link_lost:
                raise RuntimeError("link dropped")
            if time.time() - last_full_refresh >= FULL_REFRESH_INTERVAL:
                # Notifications can be missed: re-read the device once a minute.
                await self.link.refresh()
                last_full_refresh = time.time()

    def status(self) -> dict[str, object]:
        state = self.link.state
        return {
            "name": self.cfg.name,
            "connected": self.link.is_connected,
            "state": {"on": state.on, "lumens": state.lumens, "kelvin": state.kelvin},
            **asdict(self.stats),
        }


class Bridge:
    """Owns the MQTT client, the supervisors and the status endpoint."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.started = time.time()
        self.mqtt = mqtt.Client(CallbackAPIVersion.VERSION2, client_id="dyson-ble-bridge")
        self.mqtt.username_pw_set(cfg.mqtt.username, cfg.mqtt.password)
        self.mqtt.will_set(ha.BRIDGE_AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
        self.mqtt.reconnect_delay_set(min_delay=1, max_delay=60)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.supervisors: list[LampSupervisor] = []
        self.mqtt_connected = False
        self._tasks: list[asyncio.Task[None]] = []

    # ── public API ───────────────────────────────────────────────────────────

    async def run(self) -> int:
        self.loop = asyncio.get_running_loop()
        self.supervisors = [
            LampSupervisor(lamp, self.cfg.account_uuid, self.mqtt, index=index, loop=self.loop)
            for index, lamp in enumerate(self.cfg.lamps)
        ]
        self._register_topics()
        self.mqtt.on_connect = self._on_connect
        self.mqtt.connect(self.cfg.mqtt.host, self.cfg.mqtt.port, keepalive=30)
        self.mqtt.loop_start()
        start_status_server(self, self.cfg.status_port)

        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.loop.add_signal_handler(sig, stop.set)
        self._tasks = [asyncio.create_task(sup.run()) for sup in self.supervisors]
        self._tasks.append(asyncio.create_task(self._watchdog()))

        _LOGGER.info("bridge started for %d lamp(s)", len(self.supervisors))
        await stop.wait()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.mqtt.loop_stop()
        return 0

    def status(self) -> dict[str, object]:
        return {
            "mqtt_connected": self.mqtt_connected,
            "uptime_s": round(time.time() - self.started, 1),
            "lamps": {sup.cfg.serial: sup.status() for sup in self.supervisors},
        }

    # ── internals ────────────────────────────────────────────────────────────

    def _register_topics(self) -> None:
        for supervisor in self.supervisors:
            topic = f"{supervisor.cfg.base_topic}/set"
            self.mqtt.message_callback_add(topic, partial(self._route_command, supervisor))
            self.mqtt.subscribe(topic, qos=1)

    @staticmethod
    def _route_command(
        supervisor: LampSupervisor,
        _client: mqtt.Client,
        _userdata: object,
        message: mqtt.MQTTMessage,
    ) -> None:
        """paho runs callbacks on its network thread; the supervisor hops to the loop."""
        supervisor.on_message(message.payload)

    def _on_connect(
        self,
        _client: mqtt.Client,
        _userdata: object,
        _flags: object,
        reason_code: int,
        _properties: object = None,
    ) -> None:
        self.mqtt_connected = reason_code == 0
        _LOGGER.info("mqtt: %s", reason_code)
        if not self.mqtt_connected:
            return
        self.mqtt.publish(ha.BRIDGE_AVAILABILITY_TOPIC, "online", qos=1, retain=True)
        for supervisor in self.supervisors:  # rediscovery on every reconnect
            supervisor.announce()
            self.mqtt.subscribe(f"{supervisor.cfg.base_topic}/set", qos=1)

    async def _watchdog(self) -> None:
        notify_systemd("READY=1")
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            notify_systemd("WATCHDOG=1")


def start_status_server(bridge: Bridge, port: int) -> None:
    """Tiny JSON endpoint (``/status``) so Home Assistant can monitor the bridge."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            if self.path not in ("/", "/status"):
                self.send_error(404)
                return
            body = json.dumps(bridge.status()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            _LOGGER.debug("status: " + fmt, *args)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _LOGGER.info("status endpoint on :%d/status", port)
