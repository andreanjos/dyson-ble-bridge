#!/usr/bin/env bash
# Home Assistant add-on entry point: turn add-on options into the bridge's config.
set -euo pipefail

options=/data/options.json
config=/data/bridge-config.json

python3 - "$options" "$config" <<'PY'
"""Merge add-on options with the MQTT service credentials from the supervisor."""
import json
import os
import sys
import urllib.request

options_path, config_path = sys.argv[1], sys.argv[2]
options = json.load(open(options_path, encoding="utf-8"))

mqtt = {"host": "core-mosquitto", "port": 1883, "username": "", "password": ""}
token = os.environ.get("SUPERVISOR_TOKEN")
if token:
    request = urllib.request.Request(
        "http://supervisor/services/mqtt",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        service = json.load(response)["data"]
    mqtt = {
        "host": service["host"],
        "port": service["port"],
        "username": service["username"],
        "password": service["password"],
    }

config = {
    "account_uuid": options["account_uuid"],
    "mqtt": mqtt,
    "status_port": options.get("status_port", 8765),
    "lamps": options["lamps"],
}
with open(config_path, "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2)
os.chmod(config_path, 0o600)
print(f"bridge config: {len(config['lamps'])} lamp(s), mqtt {mqtt['host']}:{mqtt['port']}", flush=True)
PY

exec dyson-ble-bridge --config "${config}" --log-level "${LOG_LEVEL:-INFO}"
