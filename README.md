# dyson-ble-bridge

Expose **Dyson BLE lamps** (Solarcycle Morph, Lightcycle Morph — product family `E5R`) to
Home Assistant over MQTT discovery, running on any Linux host that has a Bluetooth adapter.

Home Assistant itself can only use a **local USB adapter** or an **ESPHome Bluetooth proxy**.
If your adapter lives on another machine (a desktop in the same room, a Pi, a NAS), this bridge
gives that machine's radio to Home Assistant: it owns the BLE links, speaks the lamp's
authenticated protocol, and publishes standard MQTT lights — no custom component required.

```
lamp  ──BLE──▶  this bridge (Linux host)  ──MQTT──▶  Home Assistant
```

## Why a bridge instead of an ESPHome proxy

ESPHome proxies work well, but they are a second radio budget, they need `active: true` and
`connection_slots`, and the ESP32-S3/C3 path has had its own GATT regressions. A host that
already has a decent USB adapter (`TP-Link UB500` here) connects reliably, and its logs,
counters and reconnect behaviour are yours to inspect and tune.

## What is verified

Everything below was validated against real lamps (firmware `5.17`, product code `PF000552`):

| Fact | Evidence |
| --- | --- |
| Key derivation | `HKDF-SHA256(salt=b"", ikm=LTK, info="USER_AUTH_AES\0\0\0", len=16)` — a unit-test vector is a key observed in a live session |
| Envelope | `IV(16) ‖ AES-128-CBC ‖ HMAC-SHA256(key, ciphertext)` (encrypt-then-MAC — *not* AES-GCM) |
| Re-auth | `0x06` = account GUID ‖ `00 00` ‖ envelope(nonce); `0x07` = `00 00` ‖ IV ‖ CT(32) ‖ MAC(32), where CT = nonce echo ‖ challenge; `0x08` → `0x26` |
| Framing | 20-byte fragments, first header `0x80 | (total-1)`, later `index & 0x7F`; pace writes ~60 ms |
| Control | power `2dd11005` (1 byte), brightness `2dd11009` (uint16 LE lumens), colour `2dd11001` (uint16 LE K) — all with **write-without-response** |
| Daylight mode | the lamp ignores manual brightness/colour writes while its daylight algorithm runs, so the bridge writes `13 20 01 00 00` to `2dd10021` first |
| CCCD | some stacks accept `start_notify` but leave the descriptor at `0000`; the bridge writes `0x0001` when the stack lets it |

## Install

```bash
git clone https://github.com/andreanjos/dyson-ble-bridge
cd dyson-ble-bridge
python3 -m venv .venv
.venv/bin/pip install .
```

Get the lamp's serial, BLE MAC and LTK once from the Dyson cloud (`GET /v1/lec/<serial>/ltk`
with your account's bearer token, e.g. `libdyson-rest` or `opendyson`), then write
`~/dyson-bridge/config.json` with mode `0600`:

```json
{
  "account_uuid": "00000000-0000-0000-0000-000000000000",
  "mqtt": {"host": "127.0.0.1", "port": 1883, "username": "homeassistant", "password": "…"},
  "status_port": 8765,
  "lamps": [
    {"serial": "E5R-US-PKA0710A", "mac": "C7:4D:76:76:15:EB", "name": "Downstairs",
     "ltk": "00112233445566778899aabbccddeeff"}
  ]
}
```

Validate, then run:

```bash
.venv/bin/dyson-ble-bridge --config ~/dyson-bridge/config.json --check
.venv/bin/dyson-ble-bridge --config ~/dyson-bridge/config.json --log-level DEBUG
```

### systemd

```bash
sudo cp deploy/dyson-ble-bridge.service /etc/systemd/system/
sudo systemctl enable --now dyson-ble-bridge
```

Install it under the unit's `User=`; BLE access on Linux normally needs root or the `bluetooth`
group (BlueZ's D-Bus policy).

## Home Assistant

The bridge publishes MQTT discovery for each lamp (`homeassistant/light/dyson_ble_<serial>/config`):

* `light.<name>` — on/off, brightness (lumen scale, 100–1000), colour temperature (2700–6500 K)
* `binary_sensor.<name>_motion` — the lamp's occupancy sensor

Availability is published per lamp **and** bridge-wide, so a dead bridge marks the entities
unavailable rather than leaving stale state.

### Wire contract with Home Assistant

Discovery is `schema: json`, `brightness: true`, `brightness_scale: 1000`,
`color_temp_kelvin: true`, `supported_color_modes: [color_temp]`. Three details of that schema
matter, because getting any of them wrong fails quietly:

* HA *keeps the `color_temp` key name* and switches only its unit — with `color_temp_kelvin: true`
  it reads and writes kelvin under `color_temp`, never under `color_temp_kelvin`. State and
  commands both use `color_temp`.
* `brightness_scale: 1000` makes HA scale its 0–255 UI range to the lumen range the lamp uses,
  so the bridge passes `brightness` through as lumens.
* Without `color_mode` in the state payload HA never exposes colour temperature at all, and a
  `color_temp` value that arrives while the mode says `brightness` is dropped.

Send `{"state": "OFF"}` (no attributes) when the lamp is off; HA keeps the last brightness and
colour until the lamp reports them again.

## Monitoring

`GET http://<host>:8765/status` returns per-lamp connection state, counters and the last error:

```json
{"mqtt_connected": true, "uptime_s": 91.1,
 "lamps": {"E5R-US-PKA0710A": {"connected": true, "connects": 3, "failures": 1,
                               "last_error": null, "state": {"on": true, "lumens": 400, "kelvin": 2700}}}}
```

That endpoint is how you tell "lamp is off" from "radio is wedged".

## Design notes

* **Pure protocol layer** (`protocol.py`) — framing, crypto and payload shaping are pure
  functions with no I/O, covered by unit tests including wire vectors captured from a lamp.
* **Injectable transport** (`link.py`) — the session logic runs against a `FakeLamp` in tests,
  so the handshake, pacing and state reconciliation are tested without hardware.
* **Serialized BLE work** — one connect procedure at a time per adapter; parallel connects fail
  with `org.bluez.Error.InProgress`.
* **Reconnect discipline** — jittered exponential backoff, instant link-loss detection, and a
  once-a-minute read-back so missed notifications cannot leave stale state.
* **Single instance** — an advisory `flock` prevents two bridges from fighting over one lamp.
* **Watchdog** — `Type=notify` + `WatchdogSec=60` restarts a bridge whose event loop stops
  making progress.

## Credits

The BLE protocol was originally reverse-engineered by **S-Termi** (shared in
[cmgrayb/hass-dyson discussion #334](https://github.com/cmgrayb/hass-dyson/discussions/334)) and
independently confirmed here against the MyDyson Android app and the lamps themselves. The
Home Assistant BLE-light integration this complements is
[cmgrayb/hass-dyson](https://github.com/cmgrayb/hass-dyson).

## License

MIT — see [LICENSE](LICENSE).
