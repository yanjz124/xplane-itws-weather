"""X-Plane 12 ITWS Weather Plugin (XPPython3).

For each ITWS airport in the aircraft's vicinity, the plugin tries weather
sources in priority order and injects the first one that returns a sample:

  1. SwimReader (private SWIM bridge) — Configured Alerts: centerfield wind
     and per-runway-end LLWAS wind. Currently PHL only.
  2. AviationWeather.gov ITWS endpoint — minute-resolution airport wind.
  3. (fallthrough) Do nothing — X-Plane's default weather source remains.

Install: copy this file plus itws_airports.py and itws_swim.py into
  <X-Plane>/Resources/plugins/PythonPlugins/

Requires XPPython3 4.0+ and X-Plane 12.0+ (12.3+ recommended).
"""

import json
import math
import os
import threading
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

from XPLMDefs import xplm_FlightLoop_Phase_AfterFlightModel
from XPLMDataAccess import XPLMFindDataRef, XPLMGetDataf
from XPLMProcessing import (
    XPLMCreateFlightLoop,
    XPLMDestroyFlightLoop,
    XPLMScheduleFlightLoop,
    XPLMCreateFlightLoop_t,
)
from XPLMUtilities import XPLMDebugString, XPLMGetSystemPath
from XPLMMenus import (
    XPLMAppendMenuItem,
    XPLMCheckMenuItem,
    XPLMCreateMenu,
    XPLMDestroyMenu,
    XPLMFindPluginsMenu,
    xplm_Menu_Checked,
    xplm_Menu_Unchecked,
)
from XPLMWeather import XPLMSetWeatherAtLocation, XPLMWeatherInfo_t, XPLMWeatherInfoWinds_t
from XPWidgets import (
    XPCreateWidget,
    XPDestroyWidget,
    XPGetWidgetDescriptor,
    XPSetWidgetDescriptor,
    XPSetWidgetProperty,
    XPAddWidgetCallback,
    XPShowWidget,
    XPHideWidget,
    XPIsWidgetVisible,
)
from XPStandardWidgets import (
    xpWidgetClass_MainWindow,
    xpWidgetClass_Caption,
    xpWidgetClass_TextField,
    xpWidgetClass_Button,
    xpProperty_MainWindowHasCloseBoxes,
    xpProperty_ButtonType,
    xpPushButton,
    xpMsg_PushButtonPressed,
    xpMessage_CloseButtonPushed,
)

from itws_airports import ITWS_AIRPORTS
import itws_swim


AWC_ITWS_URL = "https://aviationweather.gov/api/data/itws?id={icao}&format=json"
METAR_URL = "https://aviationweather.gov/api/data/metar?ids={icao}&format=json"

CONFIG_FILE = "itws_weather.json"
LOOP_INTERVAL_S = 5.0  # flight-loop tick; not the network poll interval

DEFAULTS = {
    "enabled": True,
    "poll_interval_s": 60.0,
    "vicinity_nm": 15.0,
    "max_alt_agl_ft": 2000.0,
    "swim_base_url": itws_swim.DEFAULT_BASE,
    "swim_enabled": True,
    "awc_enabled": True,
}


def _log(msg: str) -> None:
    XPLMDebugString(f"[ITWSWeather] {msg}\n")


def _nm_between(lat1, lon1, lat2, lon2):
    r_nm = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r_nm * math.asin(math.sqrt(a))


def _config_path():
    base = XPLMGetSystemPath()
    return os.path.join(base, "Resources", "plugins", "PythonPlugins", CONFIG_FILE)


def _load_config():
    path = _config_path()
    cfg = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in DEFAULTS.items():
            if k in data:
                cfg[k] = type(v)(data[k]) if not isinstance(v, bool) else bool(data[k])
    except (OSError, ValueError):
        pass
    return cfg


def _save_config(cfg):
    path = _config_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        _log(f"config save failed: {e}")


# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

def _fetch_json(url, timeout=6.0):
    req = urllib.request.Request(url, headers={"User-Agent": "xplane-itws-weather/0.2"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _latest_awc_itws(icao):
    try:
        data = _fetch_json(AWC_ITWS_URL.format(icao=icao))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError):
        return None
    if not isinstance(data, list) or not data:
        return None
    latest = data[-1]
    try:
        wdir = float(latest.get("wdir"))
        wspd = float(latest.get("wspd"))
        g = latest.get("wgst")
        wgst = float(g) if g not in (None, "") else wspd
        return {
            "wdir": wdir,
            "wspd": wspd,
            "wgst": wgst,
            "source": "AWC-ITWS",
        }
    except (TypeError, ValueError):
        return None


_polygon_capture_seen = set()  # (productType, subId, messageTime) we've already dumped


def _capture_polygon_samples(icao, parsed):
    """Save first-seen non-empty polygon-product XML to disk for offline parser dev."""
    base = XPLMGetSystemPath()
    out_dir = os.path.join(base, "Resources", "plugins", "PythonPlugins",
                           "itws_captures")
    for pt, sub_id, xml in itws_swim.find_active_polygon_samples(parsed):
        # Use a stable per-message dedupe key.
        try:
            mtime = ET.fromstring(xml).findtext(
                ".//product_header_generation_time_seconds") or "0"
        except ET.ParseError:
            mtime = "0"
        key = (pt, sub_id, mtime)
        if key in _polygon_capture_seen:
            continue
        _polygon_capture_seen.add(key)
        try:
            os.makedirs(out_dir, exist_ok=True)
            safe_pt = pt.replace(" ", "_").replace("/", "_")
            safe_sub = (sub_id or "default").replace("/", "_")
            fname = f"{icao}_{safe_pt}_{safe_sub}_{mtime}.xml"
            with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as f:
                f.write(xml)
            _log(f"captured polygon sample: {fname}")
        except OSError as e:
            _log(f"polygon capture failed for {pt}: {e}")


def _latest_swim(icao, base_url):
    """SwimReader: returns dict with centerfield wind + per-runway alerts +
    upper-wind profile layers + alert flags, or None if no useful product seen.
    """
    products = itws_swim.fetch_airport(base_url, icao)
    if not products:
        return None
    parsed = itws_swim.parse_products(products)
    cf = itws_swim.centerfield_wind(parsed)
    upper = itws_swim.upper_wind_layers(parsed)
    if cf is None and not upper:
        return None
    if cf is None:
        # No centerfield wind but profile data exists — synthesize centerfield
        # from the lowest profile band so the existing single-wind path works.
        low = upper[0]
        cf = {
            "wdir": low["wdir"], "wspd": low["wspd"], "wgst": low["wspd"],
            "source": "ITWS-Wind-Profile",
        }
    cf["runway_alerts"] = itws_swim.runway_winds(parsed)
    cf["upper_layers"] = upper
    _capture_polygon_samples(icao, parsed)

    # Useful alert flags (not injected into weather, but logged on transition)
    ws = (parsed.get(itws_swim.WIND_SHEAR_ATIS, {}).get("parsed") or [{}])[0]
    mb = (parsed.get(itws_swim.MICROBURST_ATIS, {}).get("parsed") or [{}])[0]
    eti = (parsed.get(itws_swim.GUST_FRONT_ETI, {}).get("parsed") or [{}])[0]
    tor = (parsed.get(itws_swim.TORNADO_ALERT, {}).get("parsed") or [{}])[0]
    cf["alerts"] = {
        "wind_shear_atis": ws.get("status") == "ON",
        "microburst_atis": mb.get("status") == "ON",
        "gust_front_minutes": eti.get("minutes"),
        "tornado": tor.get("exists", False),
    }
    return cf


def _latest_metar(icao):
    try:
        data = _fetch_json(METAR_URL.format(icao=icao))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError):
        return None
    if not isinstance(data, list) or not data:
        return None
    m = data[0]
    try:
        return {
            "wdir": float(m["wdir"]) if m.get("wdir") not in (None, "VRB", "") else 0.0,
            "wspd": float(m.get("wspd") or 0.0),
            "wgst": float(m["wgst"]) if m.get("wgst") not in (None, "") else float(m.get("wspd") or 0.0),
            "temp": float(m["temp"]) if m.get("temp") is not None else None,
            "dewp": float(m["dewp"]) if m.get("dewp") is not None else None,
            "altim_hpa": float(m["altim"]) if m.get("altim") is not None else None,
            "visib_sm": _parse_visib(m.get("visib")),
        }
    except (TypeError, ValueError, KeyError):
        return None


def _parse_visib(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().rstrip("+")
    try:
        return float(s)
    except ValueError:
        return None


class _Fetcher(threading.Thread):
    """Per-airport background poller. Tries source chain in priority order."""

    def __init__(self, icao, cfg):
        super().__init__(daemon=True, name=f"ITWSFetcher-{icao}")
        self.icao = icao
        self.cfg = cfg
        self.interval_s = max(15.0, float(cfg["poll_interval_s"]))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._sample = None

    def stop(self):
        self._stop.set()

    def latest(self):
        with self._lock:
            return self._sample

    def run(self):
        while not self._stop.is_set():
            sample = self._fetch_chain()
            with self._lock:
                self._sample = sample
            for _ in range(int(self.interval_s)):
                if self._stop.is_set():
                    return
                time.sleep(1.0)

    def _fetch_chain(self):
        # 1. SwimReader (richer: per-runway data when available)
        if self.cfg.get("swim_enabled", True):
            s = _latest_swim(self.icao, self.cfg.get("swim_base_url", itws_swim.DEFAULT_BASE))
            if s is not None:
                metar = _latest_metar(self.icao)
                if metar:
                    # Borrow visibility/temp/altim from METAR; keep ITWS wind.
                    for k in ("temp", "dewp", "altim_hpa", "visib_sm"):
                        if metar.get(k) is not None:
                            s.setdefault(k, metar[k])
                return s

        # 2. AWC ITWS (single airport-level wind)
        if self.cfg.get("awc_enabled", True):
            awc = _latest_awc_itws(self.icao)
            if awc is not None:
                metar = _latest_metar(self.icao)
                if metar:
                    for k in ("temp", "dewp", "altim_hpa", "visib_sm"):
                        if metar.get(k) is not None:
                            awc.setdefault(k, metar[k])
                return awc

        # 3. Fallthrough: nothing — X-Plane default weather stays in effect.
        return None


# ---------------------------------------------------------------------------
# UI: menu + settings window
# ---------------------------------------------------------------------------

class _SettingsWindow:
    """XPWidgets-based settings panel."""

    def __init__(self, plugin):
        self.plugin = plugin
        self.widget = None
        self.fields = {}  # key -> text-field widget id
        self._cb = None

    def show(self):
        if self.widget is not None:
            if not XPIsWidgetVisible(self.widget):
                XPShowWidget(self.widget)
            return
        self._build()

    def _build(self):
        x, y, w, h = 100, 600, 460, 320
        self.widget = XPCreateWidget(
            x, y, x + w, y - h,
            1, "ITWS Weather Settings", 1, 0, xpWidgetClass_MainWindow,
        )
        XPSetWidgetProperty(self.widget, xpProperty_MainWindowHasCloseBoxes, 1)

        rows = [
            ("enabled", "Enabled (1/0)"),
            ("swim_enabled", "SwimReader source (1/0)"),
            ("awc_enabled", "AWC fallback (1/0)"),
            ("swim_base_url", "SwimReader base URL"),
            ("poll_interval_s", "Poll interval (seconds, >=15)"),
            ("vicinity_nm", "Vicinity radius (nm)"),
            ("max_alt_agl_ft", "Max altitude above field (ft)"),
        ]
        row_y = y - 30
        for key, label in rows:
            XPCreateWidget(x + 10, row_y, x + 220, row_y - 20,
                           1, label, 0, self.widget, xpWidgetClass_Caption)
            tf = XPCreateWidget(x + 230, row_y, x + w - 10, row_y - 20,
                                1, str(self.plugin.cfg[key]), 0, self.widget,
                                xpWidgetClass_TextField)
            self.fields[key] = tf
            row_y -= 28

        save_btn = XPCreateWidget(x + 10, row_y - 10, x + 110, row_y - 32,
                                  1, "Save", 0, self.widget, xpWidgetClass_Button)
        XPSetWidgetProperty(save_btn, xpProperty_ButtonType, xpPushButton)
        cancel_btn = XPCreateWidget(x + 130, row_y - 10, x + 230, row_y - 32,
                                    1, "Close", 0, self.widget, xpWidgetClass_Button)
        XPSetWidgetProperty(cancel_btn, xpProperty_ButtonType, xpPushButton)

        self._save_btn = save_btn
        self._cancel_btn = cancel_btn
        self._cb = self._handler  # keep ref so GC doesn't eat it
        XPAddWidgetCallback(self.widget, self._cb)

    def _handler(self, message, widget, param1, param2):
        if message == xpMessage_CloseButtonPushed and widget == self.widget:
            XPHideWidget(self.widget)
            return 1
        if message == xpMsg_PushButtonPressed:
            if param1 == self._save_btn:
                self._apply_from_fields()
                XPHideWidget(self.widget)
                return 1
            if param1 == self._cancel_btn:
                XPHideWidget(self.widget)
                return 1
        return 0

    def _apply_from_fields(self):
        cfg = dict(self.plugin.cfg)
        bool_keys = ("enabled", "swim_enabled", "awc_enabled")
        str_keys = ("swim_base_url",)
        for key, tf in self.fields.items():
            raw = XPGetWidgetDescriptor(tf).strip()
            try:
                if key in bool_keys:
                    cfg[key] = raw not in ("0", "false", "False", "")
                elif key in str_keys:
                    cfg[key] = raw or itws_swim.DEFAULT_BASE
                else:
                    cfg[key] = float(raw)
            except ValueError:
                _log(f"ignoring invalid value for {key}: {raw!r}")
        cfg["poll_interval_s"] = max(15.0, float(cfg["poll_interval_s"]))
        cfg["vicinity_nm"] = max(1.0, float(cfg["vicinity_nm"]))
        cfg["max_alt_agl_ft"] = max(100.0, float(cfg["max_alt_agl_ft"]))
        self.plugin.update_config(cfg)

    def destroy(self):
        if self.widget is not None:
            XPDestroyWidget(self.widget, 1)
            self.widget = None
            self.fields = {}


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class _Plugin:
    def __init__(self):
        self.name = "ITWS Weather"
        self.sig = "yanjz.itws.weather"
        self.desc = "FAA ITWS one-minute surface wind injection for X-Plane 12."
        self.cfg = dict(DEFAULTS)
        self._loop_id = None
        self._lat_ref = None
        self._lon_ref = None
        self._fetchers = {}  # icao -> _Fetcher
        self._last_alerts = {}  # icao -> previous alerts dict, for transition logging

        # Menu
        self._menu_container_idx = None
        self._menu_id = None
        self._menu_enable_idx = None

        # Settings UI
        self._settings = _SettingsWindow(self)
        self._menu_cb = None

    # ---- lifecycle ----
    def XPluginStart(self):
        self.cfg = _load_config()
        self._lat_ref = XPLMFindDataRef("sim/flightmodel/position/latitude")
        self._lon_ref = XPLMFindDataRef("sim/flightmodel/position/longitude")

        params = XPLMCreateFlightLoop_t()
        params.phase = xplm_FlightLoop_Phase_AfterFlightModel
        params.callbackFunc = self._flight_loop
        params.refcon = None
        self._loop_id = XPLMCreateFlightLoop(params)
        XPLMScheduleFlightLoop(self._loop_id, LOOP_INTERVAL_S, 1)

        self._build_menu()
        _log(f"started; cfg={self.cfg}")
        return self.name, self.sig, self.desc

    def XPluginStop(self):
        if self._loop_id is not None:
            XPLMDestroyFlightLoop(self._loop_id)
            self._loop_id = None
        self._stop_all_fetchers()
        self._settings.destroy()
        if self._menu_id is not None:
            XPLMDestroyMenu(self._menu_id)
            self._menu_id = None
        _log("stopped")

    def XPluginEnable(self):
        return 1

    def XPluginDisable(self):
        self._stop_all_fetchers()

    def XPluginReceiveMessage(self, *_):
        pass

    # ---- menu ----
    def _build_menu(self):
        plugins_menu = XPLMFindPluginsMenu()
        self._menu_container_idx = XPLMAppendMenuItem(plugins_menu, "ITWS Weather", 0, 1)
        self._menu_cb = self._menu_handler  # retain ref
        self._menu_id = XPLMCreateMenu("ITWS Weather", plugins_menu,
                                       self._menu_container_idx, self._menu_cb, None)
        self._menu_enable_idx = XPLMAppendMenuItem(self._menu_id, "Enabled", "toggle", 1)
        XPLMAppendMenuItem(self._menu_id, "Settings...", "settings", 1)
        self._refresh_menu()

    def _refresh_menu(self):
        if self._menu_id is None:
            return
        state = xplm_Menu_Checked if self.cfg["enabled"] else xplm_Menu_Unchecked
        XPLMCheckMenuItem(self._menu_id, self._menu_enable_idx, state)

    def _menu_handler(self, _menu_ref, item_ref):
        if item_ref == "toggle":
            self.cfg["enabled"] = not self.cfg["enabled"]
            _save_config(self.cfg)
            if not self.cfg["enabled"]:
                self._stop_all_fetchers()
            self._refresh_menu()
        elif item_ref == "settings":
            self._settings.show()

    def update_config(self, cfg):
        old_interval = self.cfg["poll_interval_s"]
        self.cfg = cfg
        _save_config(cfg)
        self._refresh_menu()
        if old_interval != cfg["poll_interval_s"]:
            # Restart all active fetchers with the new interval.
            active = list(self._fetchers.keys())
            self._stop_all_fetchers()
            for icao in active:
                self._start_fetcher(icao)
        _log(f"config updated: {cfg}")

    # ---- core loop ----
    def _flight_loop(self, *_):
        if not self.cfg["enabled"]:
            self._stop_all_fetchers()
            return LOOP_INTERVAL_S

        lat = XPLMGetDataf(self._lat_ref)
        lon = XPLMGetDataf(self._lon_ref)
        in_range = self._airports_in_vicinity(lat, lon, self.cfg["vicinity_nm"])

        # Stop fetchers for airports we've left.
        for icao in list(self._fetchers.keys()):
            if icao not in in_range:
                self._stop_fetcher(icao)

        # Start fetchers for newly-entered airports.
        for icao in in_range:
            if icao not in self._fetchers:
                self._start_fetcher(icao)

        # Push every available sample. XPLMSetWeatherAtLocation will let
        # X-Plane blend observations across multiple stations.
        for icao, fetcher in self._fetchers.items():
            sample = fetcher.latest()
            if sample:
                self._apply(icao, sample)
                self._log_alert_transitions(icao, sample.get("alerts") or {})
        return LOOP_INTERVAL_S

    def _log_alert_transitions(self, icao, alerts):
        prev = self._last_alerts.get(icao, {})
        for k, v in alerts.items():
            if prev.get(k) != v:
                _log(f"[{icao}] {k}: {prev.get(k)!r} -> {v!r}")
        self._last_alerts[icao] = dict(alerts)

    def _start_fetcher(self, icao):
        if icao in self._fetchers:
            return
        f = _Fetcher(icao, self.cfg)
        f.start()
        self._fetchers[icao] = f
        _log(f"fetcher started for {icao}")

    def _stop_fetcher(self, icao):
        f = self._fetchers.pop(icao, None)
        self._last_alerts.pop(icao, None)
        if f is not None:
            f.stop()
            _log(f"fetcher stopped for {icao}")

    def _stop_all_fetchers(self):
        for icao in list(self._fetchers.keys()):
            self._stop_fetcher(icao)

    @staticmethod
    def _airports_in_vicinity(lat, lon, radius_nm):
        out = {}
        for icao, (alat, alon, _elev) in ITWS_AIRPORTS.items():
            d = _nm_between(lat, lon, alat, alon)
            if d <= radius_nm:
                out[icao] = d
        return out

    def _apply(self, icao, s):
        alat, alon, elev_ft = ITWS_AIRPORTS[icao]
        info = XPLMWeatherInfo_t()
        info.max_altitude_msl_ft = elev_ft + self.cfg["max_alt_agl_ft"]

        # Centerfield surface wind.
        layers = [self._wind_layer(elev_ft, s["wdir"], s["wspd"],
                                   s.get("wgst") or s["wspd"])]

        # Per-runway-end LLWAS winds (when LLWAS healthy).
        for ra in (s.get("runway_alerts") or []):
            shear = float(ra.get("shear_loss_kt") or 0)
            layers.append(self._wind_layer(elev_ft, ra["wdir"], ra["wspd"],
                                           ra["wspd"], shear=shear))

        # Upper-wind layers from Wind Profile Product (AGL -> MSL).
        for u in (s.get("upper_layers") or []):
            alt_msl = elev_ft + float(u["alt_ft_agl"])
            layers.append(self._wind_layer(alt_msl, u["wdir"], u["wspd"],
                                           u["wspd"]))
            # If a profile altitude exceeds our injection ceiling, raise it.
            if alt_msl + 100 > info.max_altitude_msl_ft:
                info.max_altitude_msl_ft = alt_msl + 100

        info.wind_layers = layers

        if s.get("visib_sm") is not None:
            info.visibility = float(s["visib_sm"]) * 1609.34
        if s.get("altim_hpa") is not None:
            info.pressure_sl = float(s["altim_hpa"]) * 100.0
        if s.get("temp") is not None:
            info.temperature_alt = float(s["temp"])
        if s.get("dewp") is not None:
            info.dewpoint_alt = float(s["dewp"])

        XPLMSetWeatherAtLocation(alat, alon, elev_ft * 0.3048, info)

    @staticmethod
    def _wind_layer(elev_ft, wdir, wspd, wgst, shear=0.0):
        w = XPLMWeatherInfoWinds_t()
        w.alt_msl = float(elev_ft)
        w.speed = float(wspd)
        w.direction = float(wdir)
        w.gust_speed = float(wgst)
        w.shear = float(shear)
        w.turbulence = 0.0
        return w


_plugin = _Plugin()


def XPluginStart():
    return _plugin.XPluginStart()


def XPluginStop():
    _plugin.XPluginStop()


def XPluginEnable():
    return _plugin.XPluginEnable()


def XPluginDisable():
    _plugin.XPluginDisable()


def XPluginReceiveMessage(from_who, msg, param):
    _plugin.XPluginReceiveMessage(from_who, msg, param)
