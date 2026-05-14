"""SwimReader ITWS REST client + minimal product parsers.

Only parses products with stable body schemas observed in calm weather:
  - Configured Alerts Product (centerfield wind, per-runway-end shear/wind)
  - Wind Shear ATIS Product / Microburst ATIS Product (ON/OFF + timer)
  - Gust Front ETI Product (minutes-to-arrival)

Polygon products (Gust Front TRACON Map, Microburst Alarm Box, Hazard Text)
have only been observed empty so far; we keep the raw XML for those so a
parser can be written against a real sample later.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

DEFAULT_BASE = "https://swim.vncrcc.org"

CONFIGURED_ALERTS = "Configured Alerts Product"
WIND_SHEAR_ATIS = "Wind Shear ATIS Product"
MICROBURST_ATIS = "Microburst ATIS Product"
GUST_FRONT_ETI = "Gust Front ETI Product"
WIND_PROFILE = "Wind Profile Product"
TORNADO_ALERT = "Tornado Alert Product"
PRECIP_TRACON = "Precipitation TRACON Product"
PRECIP_5NM = "Precipitation 5nm Product"
PRECIP_LONG = "Precipitation Long Range Product"
SM_SEP_TRACON = "SM SEP TRACON Product"
SM_SEP_5NM = "SM SEP 5nm Product"
SM_SEP_LONG = "SM SEP Long Range Product"

WIND_QUALITY_OK = ("VALID", "GOOD")

# Precipitation grid sentinel cells (per FAA spec):
#   level 0..6 = NWS reflectivity classification
#   7 = attenuated, 8 = AP-detected, 9 = bad, 15 = no coverage
PRECIP_SENTINEL_LEVELS = {7, 8, 9, 15}

LLWAS_NO_DATA = 999  # sentinel for both wind_dir and wind_speed in ca_ra_*


def _get(url, timeout=6.0):
    req = urllib.request.Request(url, headers={"User-Agent": "xplane-itws-weather/0.3"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_airport(base, icao, timeout=6.0):
    """Return list of product dicts {key, productType, subId, messageTime, rawXml}, or None on error."""
    url = f"{base}/api/itws/airport/{urllib.parse.quote(icao)}"
    try:
        body = _get(url, timeout=timeout)
        data = json.loads(body.decode("utf-8", errors="replace"))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError):
        return None
    return data.get("products") or []


def _text(elem, tag):
    if elem is None:
        return None
    child = elem.find(tag)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _int(elem, tag):
    s = _text(elem, tag)
    try:
        return int(s) if s is not None else None
    except ValueError:
        return None


def parse_configured_alerts(xml_str):
    """Return dict with centerfield wind, per-runway alerts, llwas_impaired, time."""
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None
    ca = root.find("configured_alert")
    if ca is None:
        return None

    out = {
        "time_s": _int(ca, "ca_seconds"),
        "rwy_name": _text(ca, "ca_rwy_name"),
        "centerfield_wind_dir": _int(ca, "ca_aw_wind_dir"),
        "centerfield_wind_speed": _int(ca, "ca_aw_wind_speed"),
        "centerfield_gust_speed": _int(ca, "ca_aw_gust_speed"),
        "centerfield_time_s": _int(ca, "ca_aw_seconds"),
        "wind_expiration_s": _int(ca, "ca_wind_expiration_seconds"),
        "radar_impaired": bool(_int(ca, "ca_radar_impaired")),
        "llwas_impaired": bool(_int(ca, "ca_llwas_impaired")),
        "runway_alerts": [],
    }

    for ra in ca.findall("ca_rwy_alert"):
        wdir = _int(ra, "ca_ra_llwas_wind_dir")
        wspd = _int(ra, "ca_ra_llwas_wind_speed")
        out["runway_alerts"].append({
            "region_id": _text(ra, "ca_ra_region_id"),
            "alert_type": _text(ra, "ca_ra_type") or "",
            "shear_loss_kt": _int(ra, "ca_ra_value"),
            "first_loc": _text(ra, "ca_ra_first_loc"),
            "last_loc": _text(ra, "ca_ra_last_loc"),
            "wind_dir": wdir if wdir != LLWAS_NO_DATA else None,
            "wind_speed": wspd if wspd != LLWAS_NO_DATA else None,
        })
    return out


def parse_atis_bool(xml_str):
    """Return dict {status: 'ON'|'OFF', timer_min: int, time_s: int} or None."""
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None
    pmsg = root.find("atis_pmsg")
    if pmsg is None:
        return None
    return {
        "status": _text(pmsg, "pmsg_status") or "OFF",
        "timer_min": _int(pmsg, "pmsg_timer") or 0,
        "time_s": _int(pmsg, "pmsg_utc_seconds"),
    }


def parse_gust_front_eti(xml_str):
    """Return dict {minutes: int, horizon_min: int} or None. minutes==-1 means no front."""
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None
    eti = root.find("gf_eti")
    if eti is None:
        return None
    return {
        "minutes": _int(eti, "gf_eti_minutes"),
        "horizon_min": _int(eti, "gf_eti_horizon"),
        "near": bool(_int(eti, "gf_eti_near")),
    }


def parse_wind_profile(xml_str):
    """Return list of profiles: [{loc_name, lines: [{alt_ft, dir, speed, quality}]}].

    Wind Profile Product gives N TRACON grid sample points, each with several
    altitude bands. Altitudes are AGL in feet.
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None
    wp = root.find("wind_profile")
    if wp is None:
        return None
    profiles = []
    for tw in wp.findall("tw_profile"):
        lines = []
        for ln in tw.findall("tw_profile_line"):
            alt = _int(ln, "twln_altitude")
            d = _int(ln, "twln_direction")
            s = _int(ln, "twln_speed")
            q = _text(ln, "twln_quality") or ""
            if alt is None or d is None or s is None:
                continue
            lines.append({"alt_ft_agl": alt, "dir": d, "speed": s, "quality": q})
        if lines:
            profiles.append({
                "loc_name": _text(tw, "twpro_loc_name"),
                "lines": lines,
            })
    return {"user_name": _text(wp, "tw_user_name"), "profiles": profiles}


def parse_tornado_alert(xml_str):
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None
    ta = root.find("tornado_alert")
    if ta is None:
        return None
    return {
        "exists": bool(_int(ta, "trnal_exists_flag")),
        "radius_nm": _int(ta, "trnal_radius"),
        "message": _text(ta, "trnal_message") or "",
        "time_s": _int(ta, "trnal_current_seconds"),
    }


def _decode_precip_rle(s):
    """Simple RLE: 'level,count level,count ...' -> flat list of int levels."""
    out = []
    if not s:
        return out
    for tok in s.split():
        try:
            level_s, count_s = tok.split(",", 1)
            level = int(level_s)
            count = int(count_s)
        except ValueError:
            continue
        if count > 0:
            out.extend([level] * count)
    return out


def parse_precip(xml_str):
    """Decode a Precipitation Product into metadata + flat row-major level grid.

    Grid cells: 0..6 = NWS reflectivity level (0=no precip), 7/8/9/15 sentinels.
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None
    p = root.find("precip")
    if p is None:
        return None
    nws = p.find("prcp_nws_levels")
    grid_str = _text(nws, "prcp_grid_compressed") if nws is not None else None
    nrows = _int(p, "prcp_nrows")
    ncols = _int(p, "prcp_ncols")
    if not grid_str or not nrows or not ncols:
        return None
    cells = _decode_precip_rle(grid_str)
    if len(cells) != nrows * ncols:
        # Don't trust a partially decoded grid.
        return None

    # XML stores lat/lon as integer degrees * 1e6.
    trp_lat = (_int(p, "prcp_TRP_latitude") or 0) / 1_000_000.0
    trp_lon = (_int(p, "prcp_TRP_longitude") or 0) / 1_000_000.0
    rotation_milli = _int(p, "prcp_rotation") or 0  # degrees * 1000
    return {
        "trp_lat": trp_lat,
        "trp_lon": trp_lon,
        "rotation_deg": rotation_milli / 1000.0,
        "x_offset_m": _int(p, "prcp_xoffset") or 0,
        "y_offset_m": _int(p, "prcp_yoffset") or 0,
        "dx_m": _int(p, "prcp_dx") or 1000,
        "dy_m": _int(p, "prcp_dy") or 1000,
        "nrows": nrows,
        "ncols": ncols,
        "max_level": _int(nws, "prcp_grid_max_precip_level") if nws is not None else None,
        "volume_scan_num": _int(p, "prcp_volume_scan_num"),
        "cells": cells,  # row-major: cells[r * ncols + c]
    }


def precip_cell_at(grid, lat, lon):
    """Return the NWS level under (lat, lon), or None if outside the grid.

    Equirectangular projection at the TRP latitude, then -rotation, then
    offset/dx grid index lookup. Good enough at airport scale.
    """
    import math
    if grid is None:
        return None
    dlat = lat - grid["trp_lat"]
    dlon = lon - grid["trp_lon"]
    # meters east / north relative to TRP
    y_m = dlat * 111_320.0
    x_m = dlon * 111_320.0 * math.cos(math.radians(grid["trp_lat"]))
    # Apply -rotation to align with grid axes.
    rot = math.radians(-grid["rotation_deg"])
    cs, sn = math.cos(rot), math.sin(rot)
    xg = x_m * cs - y_m * sn
    yg = x_m * sn + y_m * cs
    col = int(round((xg - grid["x_offset_m"]) / grid["dx_m"]))
    row = int(round((yg - grid["y_offset_m"]) / grid["dy_m"]))
    if not (0 <= col < grid["ncols"] and 0 <= row < grid["nrows"]):
        return None
    return grid["cells"][row * grid["ncols"] + col]


def precip_max_near(grid, lat, lon, radius_m=5000):
    """Max real precip level (0..6) within a square of radius_m around (lat,lon).

    Sentinel cells (7/8/9/15) are skipped. Returns 0 if no precip in the box,
    or None if the airport is outside the grid entirely.
    """
    if grid is None:
        return None
    # Use a square around the airport pixel — cheap and close enough at this scale.
    center_level = precip_cell_at(grid, lat, lon)
    if center_level is None:
        return None
    half_x = max(1, radius_m // grid["dx_m"])
    half_y = max(1, radius_m // grid["dy_m"])

    # Get the airport's pixel coordinate the same way.
    import math
    dlat = lat - grid["trp_lat"]
    dlon = lon - grid["trp_lon"]
    y_m = dlat * 111_320.0
    x_m = dlon * 111_320.0 * math.cos(math.radians(grid["trp_lat"]))
    rot = math.radians(-grid["rotation_deg"])
    xg = x_m * math.cos(rot) - y_m * math.sin(rot)
    yg = x_m * math.sin(rot) + y_m * math.cos(rot)
    cc = int(round((xg - grid["x_offset_m"]) / grid["dx_m"]))
    rc = int(round((yg - grid["y_offset_m"]) / grid["dy_m"]))

    best = 0
    ncols = grid["ncols"]
    cells = grid["cells"]
    r0 = max(0, rc - half_y)
    r1 = min(grid["nrows"], rc + half_y + 1)
    c0 = max(0, cc - half_x)
    c1 = min(ncols, cc + half_x + 1)
    for r in range(r0, r1):
        base = r * ncols
        for c in range(c0, c1):
            lvl = cells[base + c]
            if lvl in PRECIP_SENTINEL_LEVELS:
                continue
            if lvl > best:
                best = lvl
    return best


def parse_sm_sep(xml_str):
    """Storm Motion / Storm cell Extrapolated Position.

    Currently parses the header (count + TRP) only; the per-storm child
    schema hasn't been seen with real data yet (num_storms always 0 so far).
    The plugin captures the raw XML when num_storms>0 so we can extend later.
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None
    sm = root.find("sm_sep")
    if sm is None:
        return None
    return {
        "num_storms": _int(sm, "sm_num_storms") or 0,
        "trp_lat": float(_text(sm, "sm_latitude") or 0),
        "trp_lon": float(_text(sm, "sm_longitude") or 0),
        "rotation_deg": float(_text(sm, "sm_rotation") or 0),
    }


def _has_active_shapes(xml_str):
    """Quick heuristic: any *_num_detections/predictions/cells > 0 ?

    Used to flag polygon products (Microburst Map, Gust Front Map, Hazard Text)
    as worth saving a raw sample of, since their schemas can only be reverse-
    engineered when the body is non-empty.
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return False
    for tag in ("mbt_num_detections", "mbt_num_predictions",
                "gft_rdr_num_detections", "ht_num_cells",
                "sm_num_storms"):
        for elem in root.iter(tag):
            try:
                if int((elem.text or "0").strip()) > 0:
                    return True
            except ValueError:
                continue
    return False


def find_active_polygon_samples(parsed):
    """Yield (productType, subId, rawXml) for every polygon product currently
    carrying real shape data — i.e. worth dumping for offline parser dev.
    """
    polygon_types = (
        "Microburst TRACON Map Product",
        "Gust Front TRACON Map Product",
        "Hazard Text TRACON Product",
        "Hazard Text 5nm Product",
        SM_SEP_TRACON,
        SM_SEP_5NM,
        SM_SEP_LONG,
    )
    for pt in polygon_types:
        for raw in (parsed.get(pt, {}).get("raw") or []):
            xml = raw.get("rawXml") or ""
            if _has_active_shapes(xml):
                yield pt, raw.get("subId"), xml


PARSERS = {
    CONFIGURED_ALERTS: parse_configured_alerts,
    WIND_SHEAR_ATIS: parse_atis_bool,
    MICROBURST_ATIS: parse_atis_bool,
    GUST_FRONT_ETI: parse_gust_front_eti,
    WIND_PROFILE: parse_wind_profile,
    TORNADO_ALERT: parse_tornado_alert,
    PRECIP_TRACON: parse_precip,
    PRECIP_5NM: parse_precip,
    PRECIP_LONG: parse_precip,
    SM_SEP_TRACON: parse_sm_sep,
    SM_SEP_5NM: parse_sm_sep,
    SM_SEP_LONG: parse_sm_sep,
}


def parse_products(products):
    """Group raw product list by productType and parse what we can.

    Returns dict:
      {
        productType: {
          'parsed': [parsed_dict, ...],   # one per sub-id, parser-dependent
          'raw': [{'subId', 'messageTime', 'rawXml'}, ...],   # always retained
        }
      }
    """
    out = {}
    for p in products or []:
        pt = p.get("productType")
        slot = out.setdefault(pt, {"parsed": [], "raw": []})
        slot["raw"].append({
            "subId": p.get("subId"),
            "messageTime": p.get("messageTime"),
            "rawXml": p.get("rawXml"),
        })
        parser = PARSERS.get(pt)
        if parser is None:
            continue
        parsed = parser(p.get("rawXml") or "")
        if parsed is not None:
            parsed["subId"] = p.get("subId")
            slot["parsed"].append(parsed)
    return out


def centerfield_wind(parsed):
    """Pick a single centerfield wind sample from parsed Configured Alerts list, or None."""
    cas = parsed.get(CONFIGURED_ALERTS, {}).get("parsed") or []
    for ca in cas:
        wd, ws = ca.get("centerfield_wind_dir"), ca.get("centerfield_wind_speed")
        if wd is not None and ws is not None and wd != LLWAS_NO_DATA:
            return {
                "wdir": float(wd),
                "wspd": float(ws),
                "wgst": float(ca.get("centerfield_gust_speed") or ws),
                "time_s": ca.get("centerfield_time_s"),
                "source": "ITWS-Configured-Alerts",
                "llwas_impaired": ca.get("llwas_impaired", False),
            }
    return None


def upper_wind_layers(parsed):
    """Aggregate Wind Profile Product into list of per-altitude wind layers.

    Returns [{alt_ft_agl, wdir, wspd, n_stations, qualities}] sorted by altitude.
    Direction averaged via vector mean (handles 350 vs 10 case correctly).
    Only includes lines with quality in WIND_QUALITY_OK.
    """
    import math
    wp = parsed.get(WIND_PROFILE, {}).get("parsed") or []
    if not wp:
        return []
    by_alt = {}  # alt_ft -> list of (dir, speed, quality)
    for doc in wp:
        for prof in doc.get("profiles", []):
            for ln in prof["lines"]:
                if ln["quality"] not in WIND_QUALITY_OK:
                    continue
                by_alt.setdefault(ln["alt_ft_agl"], []).append(
                    (ln["dir"], ln["speed"], ln["quality"])
                )
    layers = []
    for alt, samples in sorted(by_alt.items()):
        # Vector-mean direction weighted by speed; fall back to scalar mean
        # if all speeds are zero.
        u = sum(s * math.sin(math.radians(d)) for d, s, _ in samples)
        v = sum(s * math.cos(math.radians(d)) for d, s, _ in samples)
        mean_speed = sum(s for _, s, _ in samples) / len(samples)
        if u == 0 and v == 0:
            mean_dir = sum(d for d, _, _ in samples) / len(samples)
        else:
            mean_dir = (math.degrees(math.atan2(u, v)) + 360.0) % 360.0
        layers.append({
            "alt_ft_agl": alt,
            "wdir": round(mean_dir, 1),
            "wspd": round(mean_speed, 1),
            "n_stations": len(samples),
            "qualities": sorted({q for _, _, q in samples}),
        })
    return layers


def best_precip_grid(parsed, lat, lon):
    """Pick the most useful precip grid covering (lat, lon).

    Prefers higher-resolution products. Returns the parsed grid dict (with
    cells), or None if no product covers the airport.
    """
    for pt in (PRECIP_5NM, PRECIP_TRACON, PRECIP_LONG):
        for g in (parsed.get(pt, {}).get("parsed") or []):
            if precip_cell_at(g, lat, lon) is not None:
                return g
    return None


def runway_winds(parsed):
    """Per-runway-end winds from Configured Alerts, only where LLWAS reported real data."""
    out = []
    for ca in (parsed.get(CONFIGURED_ALERTS, {}).get("parsed") or []):
        for ra in ca.get("runway_alerts", []):
            if ra.get("wind_dir") is None or ra.get("wind_speed") is None:
                continue
            out.append({
                "region_id": ra["region_id"],
                "wdir": float(ra["wind_dir"]),
                "wspd": float(ra["wind_speed"]),
                "shear_loss_kt": ra.get("shear_loss_kt") or 0,
            })
    return out
