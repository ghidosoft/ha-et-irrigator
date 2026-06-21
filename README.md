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
   `[reference → now]` and aggregate them per day.
3. Compute daily reference ET (FAO-56 Penman-Monteith, using your **measured solar
   radiation**), sum it over the window, multiply by the crop coefficient `Kc`.
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
  # elevation defaults to your HA location; latitude is taken from HA.
  elevation: 250
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
| `rain`            | **must be `mm`**                         | read as-is, uses the per-hour increase (`change`)  |

So temperature and wind are converted from whatever your station reports; solar
radiation and rain are read natively and must already be in `W/m²` and `mm`.

## Services

* `et_irrigator.recalculate` — force an immediate recompute of all zones. Idempotent.

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
