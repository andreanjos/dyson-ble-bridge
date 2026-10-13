# Agent brief: the four Dyson lamps

Facts and gotchas for an agent that already manages this Home Assistant. Everything below exists
today; nothing here needs setup.

## Devices

Three Dyson **Solarcycle Morph (E5R)** lamps, arriving as **MQTT** lights from a local bridge
(`dyson-ble-bridge`, host `10.10.10.81`) that owns their Bluetooth links and mirrors each lamp.
Dyson's cloud is not involved.

The owner is renaming the entities, so match on the **device**, not on the current `entity_id`
(`dyson_conservatory_1_dyson_conservatory_1` is a known wart):

| Device | Light | Motion |
| --- | --- | --- |
| Dyson Downstairs | `light.dyson_downstairs_dyson_downstairs` | `binary_sensor.dyson_downstairs_dyson_downstairs_motion` |
| Dyson Conservatory 1 | `light.dyson_conservatory_1_dyson_conservatory_1` | `binary_sensor.dyson_conservatory_1_dyson_conservatory_1_motion` |
| Dyson Conservatory 2 | `light.dyson_conservatory_2_dyson_conservatory_2` | `binary_sensor.dyson_conservatory_2_dyson_conservatory_2_motion` |
| Dyson Study | `light.dyson_study` | `binary_sensor.dyson_study_motion` |

## What they can do

* On/off, **brightness** and **colour temperature 2700–6500 K**. No RGB, no effects.
* Brightness: `brightness_pct` (0–100) or `brightness` (0–255). The lamp's range is 100–1000 lm, so
  `brightness_pct: 10` is its dimmest usable step.
* Colour temperature: `color_temp_kelvin` (2700 warm → 6500 daylight). Not mireds.
* `binary_sensor.*_motion` is the lamp's own occupancy sensor (`device_class: motion`).

## Gotchas (not discoverable from the entities)

1. Brightness and colour temperature work in a **single** `light.turn_on` call; the bridge paces
   the two BLE writes. No second call, no retry loop.
2. State follows the lamp: the bridge re-reads the device after each command and every ~13 s, so
   allow 1–2 s before the entity settles. Never pin or force the state with templates.
3. The first HA brightness/colour write switches the lamp to manual mode and thereby **disables its
   built-in Solarcycle daylight automation** (it ignores manual writes otherwise). Treat the lamps
   as fully HA-controlled.
4. Motion is event-driven and never retained: after a bridge restart the motion sensor reads
   `unknown` until the next movement. `unknown` is not "no motion".
5. An entity becomes `unavailable` when its lamp's BLE link drops (out of range) or the bridge
   restarts (30–60 s to reconnect). Guard triggers with `not_from: ["unavailable", "unknown"]` and
   avoid "turn off when unavailable" logic.
6. Four lamps are integrated; these are all of them. Do not reference any other Dyson light.

## Example: motion-lit conservatory after dark, off ten minutes after the room empties

```yaml
- alias: Conservatory lamps on motion after sunset
  triggers:
    - trigger: state
      entity_id:
        - binary_sensor.dyson_conservatory_1_dyson_conservatory_1_motion
        - binary_sensor.dyson_conservatory_2_dyson_conservatory_2_motion
      to: "on"
      not_from: ["unavailable", "unknown"]
  conditions:
    - condition: sun
      after: sunset
  actions:
    - action: light.turn_on
      target:
        entity_id:
          - light.dyson_conservatory_1_dyson_conservatory_1
          - light.dyson_conservatory_2_dyson_conservatory_2
      data:
        brightness_pct: 35
        color_temp_kelvin: 3000
  mode: single

- alias: Conservatory lamps off when the room is empty
  triggers:
    - trigger: state
      entity_id:
        - binary_sensor.dyson_conservatory_1_dyson_conservatory_1_motion
        - binary_sensor.dyson_conservatory_2_dyson_conservatory_2_motion
      to: "off"
      for: "00:10:00"
  actions:
    - action: light.turn_off
      target:
        entity_id:
          - light.dyson_conservatory_1_dyson_conservatory_1
          - light.dyson_conservatory_2_dyson_conservatory_2
  mode: single
```
