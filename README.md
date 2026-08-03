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
  Irrigator runs a real FAO-56 soil-water bucket too — but **reconstructs it from
  statistics on every run** instead of persisting it. Same physics, and the result
  stays a pure function of recorded data: **idempotent and non-destructive**. Run
  it as many times as you like; there is no state to corrupt and nothing to reset.
* **A soil bucket, clamped every step.** Rain that lands on already-full soil
  drains away *in the hour it falls*, instead of being banked and silently
  cancelling the next week's evapotranspiration. This is the difference between a
  deficit curve that ramps and one that sits at zero and then jumps to its cap.
* **Rolling window since the last irrigation.** The balance is reconstructed over
  `[last watering → now]`. When your irrigation actually runs (detected from the
  zone's `irrigation_sensor`), the window resets.
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
   radiation**) for each step and multiply by the crop coefficient `Kc`. By
   default the step is one **hour** (FAO-56 Eq. 53) — see *ET method* below — or
   one calendar day for the daily equation.
4. Walk the steps in order, keeping a soil **depletion** (mm below field
   capacity), starting at 0 and clamped at **every** step:

   ```
   infiltration = min(rain, max_infiltration_rate)      # excess = runoff
   depletion   += ETc − infiltration
   depletion    = clamp(depletion, 0, maximum_deficit)  # excess below 0 = drainage
   ```

5. `duration = depletion / rate × 3600`, where
   `rate [mm/h] = throughput [L/min] × 60 / area [m²]`, then apply `multiplier`,
   `maximum_duration` and `lead_time`.

The recommended run-time (seconds) is published as `sensor.et_irrigator_<zone>`,
with a Smart-Irrigation-compatible set of attributes (`deficit`, `delta`,
`evapotranspiration`, `precipitation`, `size`, `throughput`, …), plus diagnostic
sensors for the balance itself — see *Entities* below. Feed the run-time into your
irrigation automation (e.g. Irrigation Unlimited) exactly as you would feed Smart
Irrigation's `duration`.

### Why clamping every step matters

Summing first and clamping once — `clamp(ΣET·Kc − Σrain, 0, max)` — looks
equivalent and is not. A 35 mm storm stays *inside* the rolling window for the
whole `max_window_days`, retroactively paying for every dry day after it; the hour
it scrolls out, the deficit jumps straight from 0 to `maximum_deficit`. Real soil
does not work that way: it fills, and the rest runs off or percolates past the
roots the same day.

Clamping per step also makes the reconstruction *stable*. Each step is
`f(d) = clamp(d + ETc − infiltration, 0, TAW)`, which is monotone and 1-Lipschitz,
and composing such functions preserves both. So sliding the window by one hour can
move the answer by at most **that hour's net drying** (a fraction of a mm), never
by the rain it contained. And the memory of the `depletion = 0` starting
assumption is erased *entirely* the first time a later step hits a clamp — which
any soil-filling rain does. That is why no state needs to be persisted for the
result to be well-behaved.

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
      # Application rate: EITHER precipitation_rate, OR area + throughput.
      area: 50                  # m²
      throughput: 12            # L/min delivered to the zone
      # precipitation_rate: 14  # mm/h measured directly (alternative; takes priority)
      crop_coefficient: 1.0     # Kc (optional)
      irrigation_sensor: binary_sensor.iu_zone_lawn   # on while watering
      max_window_days: 7        # safety cap if never irrigated
      maximum_deficit: 30       # mm, TAW: what the root zone can hold
      max_infiltration_rate: 15 # mm/h; rain faster than this runs off (optional)
      multiplier: 1.0           # optional fudge factor
      lead_time: 0              # seconds added to every run
      maximum_duration: -1      # seconds, -1 = no cap
```

All weather channels except `temperature` are optional, but **solar radiation is
strongly recommended** — without it the FAO-56 net-radiation term degrades.

**Application rate per zone.** Each zone needs to know how fast it applies water
(mm/h). Provide it either way:

* **`precipitation_rate`** (mm/h) — the rate directly, e.g. measured with the
  [catch-cup method](https://en.wikipedia.org/wiki/Irrigation_sprinkler#Uniformity)
  (cans spread across the zone, run N minutes, average depth ÷ minutes × 60). This
  is what catch-cups give you, with no conversion. **Takes priority if both given.**
* **`area`** (m²) + **`throughput`** (L/min) — the rate is derived as
  `throughput × 60 / area` (1 L over 1 m² = 1 mm).

The run-time only depends on this rate, so the two forms are interchangeable.

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
  each hour, and the water balance stepped hour by hour. Matches the hourly
  granularity of the data: the window edges (e.g. an irrigation at 05:30) are exact
  rather than a partial-day approximation, rain is drained against the soil state of
  the hour it actually fell in, and the deficit is **monotonic** through dry spells
  (no day-aggregate revision wobble). Recommended.
* **`daily`** — FAO-56 daily Penman-Monteith (Eq. 6) per calendar day. Slightly
  cheaper, kept mainly for A/B comparison; the in-progress day's estimate is
  revised as new hours arrive, so the duration can wobble a few seconds.

Both use your measured solar radiation. Over full days they agree within a few
percent (hourly-summed is typically a touch lower and is considered more accurate
under variable conditions).

## Entities

Each zone publishes one **run-time** sensor plus a set of **diagnostic** sensors
in mm/%. The diagnostics exist because state *attributes* get no long-term
statistics: to graph the deficit in Home Assistant's native History/Statistics
cards it has to be a state, not an attribute. They all carry
`state_class: measurement` (which is what makes the recorder keep them) and
`entity_category: diagnostic` (which keeps them off the auto-generated dashboard).

| Entity | Unit | Meaning |
| --- | --- | --- |
| `sensor.et_irrigator_<zone>` | s | **Recommended run-time.** Carries the full attribute set. |
| `…_deficit` | mm | Soil depletion, `0 … maximum_deficit`. The curve to watch. |
| `…_net_deficit` | mm | **Signed**, unclamped: positive = dry, **negative = surplus**. |
| `…_soil_moisture` | % | `100 × (1 − deficit / maximum_deficit)`. |
| `…_evapotranspiration` | mm | ETo × Kc summed over the window. |
| `…_precipitation` | mm | Gauge rain over the window (gross). |
| `…_rain_lost` | mm | Rain that never reached the roots: drained + run off. |
| `…_hourly_rain` | mm | **Per hour**, gauge rain. State `unknown` by construction. |
| `…_hourly_runoff` | mm | **Per hour**, rain shed by `max_infiltration_rate`. |
| `…_hourly_drainage` | mm | **Per hour**, water pushed past field capacity. |

**The three `hourly_*` entities have no state — that is deliberate.** Everything
above is a *level*: a sum over the sliding window, correct as a curve but wrong
as a bar, because the coordinator only samples it an hour after the rain fell,
and a Home Assistant restart writes the same value again (which a column chart
draws as a second, phantom shower). The `hourly_*` series instead go straight
into the recorder's long-term statistics, each row stamped with the hour it
actually belongs to. Read them as statistics, never as states:

```yaml
# apexcharts-card
- entity: sensor.et_irrigator_lawn_hourly_rain
  type: column
  statistics:
    type: change
    period: hour
```

They keep a `sensor.` entity so charts can resolve them, but carry no
`state_class` and no numeric state: with a `state_class` the recorder would
compile competing statistics for the same id, and with a numeric state Home
Assistant would raise a permanent `state_class_removed` repair.

Sign convention: everything named *deficit* is a deficit, so **positive means the
soil is dry**. The legacy `delta` attribute is the opposite sign (rain − ET), kept
unchanged for Smart-Irrigation compatibility; `net_deficit` is simply `−delta`.

**The diagnostic graph.** Plot `deficit` and `net_deficit` on the same axis. Where
they coincide, the bucket is running freely; where they diverge, a clamp is
binding — `net_deficit` below zero is surplus being discarded, `deficit` flat at
`maximum_deficit` is the ceiling. Note that `net_deficit` is a plain window sum, so
it *does* still step when old data scrolls out of the window: that is what it is
for (seeing the raw balance), and it is why the run-time is not computed from it.

## Tuning

* **`maximum_deficit` (TAW)** — how much water the root zone holds, in mm. Roughly
  `root depth (m) × available water capacity (mm/m)`: a lawn at 15 cm in loam is
  ≈ 0.15 × 130 ≈ 20 mm. Too high and the zone asks for a soaking it can't absorb;
  too low and `capped` (an attribute) starts accumulating — that number is your
  chronic-under-watering signal.
* **`max_infiltration_rate`** — the soil's intake rate in mm/h. The bucket already
  handles *volume*-driven runoff (rain on full soil), but not *intensity*-driven
  runoff: 35 mm in two hours mostly runs off even on dry soil. Sandy soils take
  20-30 mm/h, loam ~10-15, clay under 5. Watch `…_hourly_runoff` after a storm to
  calibrate — it isolates the intensity-driven half, which is the only part this
  setting controls directly (`…_rain_lost` mixes it with volume-driven drainage).
  Leave it unset to count all gauge rain as infiltrating; with no cap set,
  `…_hourly_runoff` stays flat at zero, which is correct rather than broken.
  Changing it takes effect on `et_irrigator.reload`, which also redraws the
  runoff already published for the last `max_window_days`.
* **Deep, infrequent watering** — this integration publishes a recommended
  run-time, not a schedule. To water deeply every few days instead of a trickle
  daily, gate your automation on the deficit, e.g.
  `{{ state_attr('sensor.et_irrigator_lawn', 'deficit') > 15 }}` (FAO-56 suggests
  refilling at ~50% of TAW for turf).

## Limitations (by design)

* **Window start assumes field capacity.** The balance starts at depletion 0 at the
  end of the last irrigation — true right after watering. In the fallback case (no
  irrigation within `max_window_days`) that is a guess, but a *bounded* one: the
  per-step clamps erase it as soon as any rain fills the soil or the depletion
  reaches `maximum_deficit`. See *Why clamping every step matters*.
* **Solar is integrated from hourly statistics.** Daily radiation is the sum of the
  covered hours' mean irradiance. Recorder gaps at night are harmless (0), but
  daytime gaps under-count the day's energy and slightly under-estimate ET. An hour
  with rain statistics but no temperature statistics is kept, with ETo 0 — losing
  the rain would be the far worse error.
* **Single-layer bucket.** No root-depth profile, no percolation curve, no capillary
  rise. Infiltration is capped and everything above field capacity is gone the same
  step.
* **No water-stress coefficient (`Ks`).** FAO-56 reduces actual ET once depletion
  passes the readily-available fraction, so a bucket pinned at `maximum_deficit`
  slightly over-estimates the real requirement.
* **Irrigation is a window reset, not a metered input.** Any detected `on` → `off`
  transition of the `irrigation_sensor` resets the balance to zero regardless of how
  long it actually ran, so a partial run is credited as a full refill.
* **The daily method approximates twice.** Rain and ET within the same day net out
  before the clamp, so drainage is under-counted, and `max_infiltration_rate` is
  applied as `rate × 24 h` and effectively never binds. Use the default `hourly`.
* **No rain forecast.** Only recorded history.
* **The hourly series reach back one window, and no further.** They are rebuilt
  from `[now − max_window_days, now]`, so there is no backfill of history older
  than that, and an hour's bar appears about an hour late — hour H is published
  when the recorder compiles H's statistics, around H+1:12. The hour in progress
  is never written, because its rain is still accumulating.
* **Retuning `max_infiltration_rate` redraws runoff, not drainage.** Rain and
  runoff are pure functions of the hour's mm and the cap, so they are recomputed
  identically every time and safely rewritten. Drainage is path dependent — it
  follows the bucket's trajectory, hence the window start — so each hour is
  written once, from the balance that starts at the last irrigation, and never
  revised. The cap still affects drainage indirectly (through what infiltrates),
  but the signal to calibrate it is the runoff. The single hour an irrigation
  *ends* in falls outside the balance and is recorded as drainage 0.
* **Correcting a value by hand.** *Developer tools → Statistics* can adjust the
  sum from a given hour onwards, which changes that one hour's bar. On
  `…_hourly_drainage` the correction sticks, since that series is never
  rewritten; on `…_hourly_rain` and `…_hourly_runoff` it is overwritten by the
  next rewrite (restart, `reload`, `recalculate`) while the hour is still inside
  `max_window_days`.

## Upgrading from 0.1.x

0.2.0 replaces sum-then-clamp with the per-step soil bucket. Nothing in your YAML
has to change, but:

* **Expect different — usually larger — deficits right after upgrading**, especially
  if it rained recently. That is the fix, not a regression: surplus rain is no
  longer paying for later evapotranspiration.
* `maximum_deficit` is unchanged in name and default but is now genuinely the soil's
  holding capacity (TAW), because it is the bucket ceiling rather than a post-hoc
  cap. It is also now rejected at 0 (which used to silently zero the zone).
* Six diagnostic entities per zone are new. The run-time sensor keeps its exact
  `entity_id` and `unique_id`, so existing automations and history are untouched.
* The `delta` attribute is unchanged; `net_deficit` is its negation, in the deficit
  sign convention used everywhere else.

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
  entities), recorder access through the recorder's own executor (all reads, plus
  the per-hour statistics import), and correct `state_class`/`device_class` for
  statistics. The deviations are about the *management surface*, not the
  calculation engine.
* Config changes apply without a restart via `et_irrigator.reload` (the standard
  reload helpers target the legacy `sensor: - platform:` style and don't fit this
  hub + discovery integration, so the reload is implemented manually).
* Migration path stays open: a config flow can be added *alongside* YAML later
  (import pattern), reusing `calc.py` / `coordinator.py` unchanged — they are
  decoupled from how the config arrives.

## Development

```bash
uv python install 3.14
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

**Pin the test environment to the Home Assistant version you deploy to.**
`requirements-dev.txt` pins `pytest-homeassistant-custom-component`, which in
turn pins `homeassistant` exactly; that one line is what selects the core
version, and the core version is what dictates the Python version. Testing
against an older core is how a change to the recorder's statistics metadata
(`mean_type` / `unit_class`, required from HA 2026.11) passed unnoticed
locally — the tests were green against a core that did not have it.
`tests/test_export.py` now asserts our metadata against the installed
`StatisticMetaData.__required_keys__`, so the next such change fails a test
instead of a deployment.

`custom_components/et_irrigator/calc.py` is pure (no HA imports) and is validated
against the FAO-56 worked example in `tests/test_calc.py`.

## Credits

Evapotranspiration maths vendored from
[PyETo](https://github.com/woodcrafty/PyETo) / aquacrop-eto (BSD-3-Clause).
Inspired by [Smart Irrigation](https://github.com/jeroenterheerdt/HAsmartirrigation).
