# xplane-itws-weather

X-Plane 12 plugin (XPPython3) that injects FAA Integrated Terminal Weather System (ITWS) data as real X-Plane weather at airports the aircraft is flying near.

## Data sources (priority order)

1. **SwimReader** — private FAA SWIM bridge (Solace → REST). Provides:
   - Centerfield wind (Configured Alerts Product)
   - Per-runway-end LLWAS wind, when LLWAS is healthy
   - 9 upper-wind layers from 2000–10000 ft AGL (Wind Profile Product), vector-meaned across all TRACON grid stations
   - Wind shear / microburst / gust-front / tornado alert flags (logged on transition)
   - Auto-captures raw XML for polygon products (microburst alarm box, gust front map, hazard text cells) the first time they carry real shape data, so parsers can be written against real samples
2. **AviationWeather.gov ITWS endpoint** — minute-resolution surface wind for any of the ~45 ITWS-equipped airports
3. **X-Plane default** — fallthrough when neither source has data

## Install

Copy these three files into `<X-Plane>/Resources/plugins/PythonPlugins/`:

- `PI_ITWSWeather.py`
- `itws_airports.py`
- `itws_swim.py`

Requires XPPython3 4.0+ and X-Plane 12.0+ (12.3+ recommended).

## Configuration

Settings are written to `Resources/plugins/PythonPlugins/itws_weather.json` and exposed via the **Plugins → ITWS Weather → Settings…** menu:

| Key | Default | Notes |
|---|---|---|
| `enabled` | `true` | Master on/off (also a toggle in the menu) |
| `swim_enabled` | `true` | Use SwimReader as primary source |
| `awc_enabled` | `true` | Fall back to AviationWeather.gov ITWS |
| `swim_base_url` | `https://swim.vncrcc.org` | Override to point at a local SWIM bridge |
| `poll_interval_s` | `60` | Network poll cadence (≥15) |
| `vicinity_nm` | `15` | Aircraft must be within this radius for an airport's data to be injected |
| `max_alt_agl_ft` | `2000` | Injection ceiling above field elevation (auto-raised to cover wind-profile altitudes) |

## Behavior

The plugin starts a background fetcher per ITWS airport in vicinity, tries the source chain on each poll, and pushes everything it gets into a single `XPLMSetWeatherAtLocation` call per airport. Multi-station data (per-runway LLWAS, per-altitude wind profile) is sent as multiple `XPLMWeatherInfoWinds_t` layers, which X-Plane blends.

Alert-flag transitions (e.g. wind-shear ATIS going ON, gust-front ETI dropping below the horizon, tornado appearing) are logged to `XPLMDebugString`, which surfaces in `Resources/plugins/PythonPlugins/Log.txt`.
