# ET Irrigator

A Home Assistant custom integration that recommends per-zone irrigation run-times
from **reference evapotranspiration (FAO-56 Penman-Monteith)** and rainfall, using
Home Assistant's own **long-term statistics** as the data source.

It is a lighter, YAML-first alternative to
[Smart Irrigation](https://github.com/jeroenterheerdt/HAsmartirrigation). The
evapotranspiration maths are the same (the vendored `pyeto` FAO-56 code), but the
engineering is different:

* **Recompute, never accumulate.** Smart Irrigation keeps a mutable *bucket* float
  that is mutated every run and can only be reset through a service call. ET
  Irrigator instead **recomputes the whole water balance from statistics on every
  run**, so the result is a pure function of recorded data — **idempotent and
  non-destructive**. Run it as many times as you like.
* **Rolling window since the last irrigation.** The deficit is integrated over
  `[last watering → now]`. When your irrigation actually runs (detected from the
  zone's `irrigation_sensor`), the window resets — no stale bucket.
* **Hourly, on real data.** It recomputes the moment Home Assistant commits a new
  hour of long-term statistics (`recorder_hourly_statistics_generated`), not once a
  day on a forecast.
* **No forecast hacks.** It needs real sensor history (a weather station), not a
  forecast. If you only have a forecast, this integration is not for you.

## How it works

For each zone, on every recompute:

1. Find the end of the last irrigation from the zone's `irrigation_sensor`
   (a `binary_sensor`/`switch` that is `on` while watering). If none in the last
   `max_window_days`, fall back to that cap.
2. Read hourly long-term statistics for your weather sensors over
   `[reference → now]`.
3. Compute reference ET (FAO-56 Penman-Monteith, using your **measured solar
   radiation**) over the window and multiply by the crop coefficient `Kc`. By
   default this is the **hourly** equation (FAO-56 Eq. 53) summed per hour — see
   *ET method* below — or the daily equation per calendar day.
4. `deficit = clamp(ΣET·Kc − Σrain, 0, maximum_deficit)`.
5. `duration = deficit / rate × 3600`, where
   `rate [mm/h] = throughput [L/min] × 60 / area [m²]`, then apply `multiplier`,
   `maximum_duration` and `lead_time`.

The recommended run-time (seconds) is published as `sensor.et_irrigator_<zone>`,
with a Smart-Irrigation-compatible set of attributes (`deficit`, `delta`,
`evapotranspiration`, `precipitation`, `size`, `throughput`, …). Feed that into
your irrigation automation (e.g. Irrigation Unlimited) exactly as you would feed
Smart Irrigation's `duration`.

## Installation

HACS → custom repository → this repo → install. Or copy
`custom_components/et_irrigator` into your `config/custom_components/`.

No Python requirements are pulled in: the FAO-56 maths are vendored
(`pyeto/`, BSD-3) and rely only on `numpy`, which Home Assistant already ships.

## Configuration (YAML only, for now)

```yaml
et_irrigator:
  # elevation defaults to your HA location; latitude/longitude are taken from HA.
  elevation: 250
  et_method: hourly             # hourly (default) | daily
  wind_measurement_height: 10   # metres; your anemometer height (default 2)
  sensors:
    temperature: sensor.ws_temperature      # required
    dewpoint: sensor.ws_dewpoint            # vapour-pressure source
    wind_speed: sensor.ws_wind
    solar_radiation: sensor.ws_solar        # measured irradiance
    rain: sensor.ws_rain_total              # total_increasing
  zones:
    - name: Lawn
      area: 50                  # m²
      throughput: 12            # L/min delivered to the zone
      crop_coefficient: 1.0     # Kc (optional)
      irrigation_sensor: binary_sensor.iu_zone_lawn   # on while watering
      max_window_days: 7        # safety cap if never irrigated
      maximum_deficit: 30       # mm, field-capacity cap
      multiplier: 1.0           # optional fudge factor
      lead_time: 0              # seconds added to every run
      maximum_duration: -1      # seconds, -1 = no cap
```

All weather channels except `temperature` are optional, but **solar radiation is
strongly recommended** — without it the FAO-56 net-radiation term degrades.

### Sensor units

Every sensor must have **long-term statistics enabled** (i.e. a `state_class`).
Units are handled as follows:

| Sensor            | Accepted units                          | Notes                                              |
| ----------------- | --------------------------------------- | -------------------------------------------------- |
| `temperature`     | any temperature (`°C`, `°F`, `K`)       | **auto-converted** to °C by Home Assistant         |
| `wind_speed`      | any speed (`m/s`, `km/h`, `mph`, `kn`…) | **auto-converted** to m/s by Home Assistant        |
| `dewpoint`        | same unit class as temperature          | **auto-converted** to °C                           |
| `solar_radiation` | **must be `W/m²`**                       | read as-is (integrated to MJ/m²/day internally)    |
| `rain`            | **must be `mm`**, `total_increasing`     | uses the per-hour increase (`change`) — see below  |

So temperature and wind are converted from whatever your station reports; solar
radiation and rain are read natively and must already be in `W/m²` and `mm`.

> **The rain sensor must have `state_class: total_increasing`** (a cumulative
> rainfall total). Home Assistant only derives the per-period `change` from a
> `sum`, which only exists for `total`/`total_increasing` sensors. A `measurement`
> rain sensor produces no `change`, so **precipitation would silently be treated as
> zero** and you'd over-irrigate. The integration logs a warning if it detects
> this. If your station only exposes rain *rate* (mm/h, measurement), feed a
> [utility_meter](https://www.home-assistant.io/integrations/utility_meter/) or
> Riemann-sum integral (giving a `total_increasing` mm total) instead.

### ET method (`et_method`)

* **`hourly`** (default) — FAO-56 hourly Penman-Monteith (Eq. 53) computed for
  each hour and summed over the window. Matches the hourly granularity of the
  data: the window edges (e.g. an irrigation at 05:30) are exact rather than a
  partial-day approximation, and the integrated deficit is **monotonic** between
  irrigations (no day-aggregate revision wobble). Recommended.
* **`daily`** — FAO-56 daily Penman-Monteith (Eq. 6) per calendar day. Slightly
  cheaper, kept mainly for A/B comparison; the in-progress day's estimate is
  revised as new hours arrive, so the duration can wobble a few seconds.

Both use your measured solar radiation. Over full days they agree within a few
percent (hourly-summed is typically a touch lower and is considered more accurate
under variable conditions).

## Limitations (v1, by design)

* **Window start assumes field capacity.** The deficit is integrated from the end
  of the last irrigation, taking the soil as full (deficit 0) at that point — true
  right after watering. In the fallback case (no irrigation within
  `max_window_days`), it assumes field capacity that many days ago, which is a
  guess; the `maximum_deficit` cap bounds the error, but after a long dry spell the
  deficit can be **under-estimated**.
* **Solar is integrated from hourly statistics.** Daily radiation is the sum of the
  covered hours' mean irradiance. Recorder gaps at night are harmless (0), but
  daytime gaps under-count the day's energy and slightly under-estimate ET.
* **No soil/runoff model beyond the field-capacity cap.** Heavy rain above
  `maximum_deficit` is treated as run-off; there is no drainage curve.

## Services

* `et_irrigator.recalculate` — force an immediate recompute of all zones. Idempotent.
* `et_irrigator.reload` — re-read the YAML config and apply zone/sensor changes
  **without restarting** Home Assistant (also appears in *Developer Tools → YAML*
  and `homeassistant.reload_all`). Note: this reloads **configuration** only —
  changes to the integration's Python code still need a Home Assistant restart.

## Design notes (architecture choices)

**YAML-only, no config flow (deliberate, for now).** This integration configures
via `async_setup` + a YAML block, not Home Assistant's recommended UI config flow
/ config entries. That's a conscious trade-off, not an oversight:

* What we give up: a *Settings → Devices & Services* card, an options flow (editing
  from the UI), config-entry features (diagnostics, repairs, reauth), and
  eligibility for **HA core** (core mandates config flow — HACS does not).
* What we keep: entity `unique_id`s + device registry (renameable, customizable
  entities), read-only recorder access via the recorder executor, and correct
  `state_class`/`device_class` for statistics. The deviations are about the
  *management surface*, not the calculation engine.
* Config changes apply without a restart via `et_irrigator.reload` (the standard
  reload helpers target the legacy `sensor: - platform:` style and don't fit this
  hub + discovery integration, so the reload is implemented manually).
* Migration path stays open: a config flow can be added *alongside* YAML later
  (import pattern), reusing `calc.py` / `coordinator.py` unchanged — they are
  decoupled from how the config arrives.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install pytest-homeassistant-custom-component
pytest -q
```

`custom_components/et_irrigator/calc.py` is pure (no HA imports) and is validated
against the FAO-56 worked example in `tests/test_calc.py`.

## Credits

Evapotranspiration maths vendored from
[PyETo](https://github.com/woodcrafty/PyETo) / aquacrop-eto (BSD-3-Clause).
Inspired by [Smart Irrigation](https://github.com/jeroenterheerdt/HAsmartirrigation).
