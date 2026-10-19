"""
C3S multi-system seasonal outlook for Lampang province
=======================================================

Downloads Copernicus C3S seasonal forecasts (monthly means, all contributing
systems) plus their hindcasts (1993-2016) for a small box around Lampang,
computes tercile probabilities the same way C3S does, and writes a compact
JSON file that index.html reads (data/c3s_seasonal.json).

What it adds on top of the official C3S charts
  * Province area-mean outlook (rain + 2 m temperature) per month and for the
    next 3-month season.
  * Unweighted multi-system mean (MME) + each system shown separately, so the
    page can display how much the systems agree.
  * A "trend-adjusted" temperature tercile: the hindcast is shifted along its
    own linear trend to the forecast year before the terciles are computed.
    The fixed 1993-2016 reference period otherwise inflates "above normal"
    probabilities in a warming climate.
  * 1-degree grid probabilities for a map overlay.

Run
  export CDSAPI_KEY=<your CDS personal access token>
  python scripts/c3s_seasonal_lampang.py                 # latest release
  python scripts/c3s_seasonal_lampang.py --init 2026-10  # a specific start

Requirements: see scripts/requirements-c3s.txt. You must accept the dataset
licence once on the CDS website ("seasonal-monthly-single-levels") or every
request fails with a 403.
"""

from __future__ import annotations

import argparse
import calendar
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

log = logging.getLogger("c3s")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
CDS_URL = os.environ.get("CDSAPI_URL", "https://cds.climate.copernicus.eu/api")
DATASET = "seasonal-monthly-single-levels"

# Download box (N, W, S, E) on the C3S 1-degree grid, and the subset of cells
# averaged for the province mean (Lampang ~17.2-19.4N, 98.9-100.2E).
AREA = [20, 98, 17, 101]
PROVINCE_LATS = (17.0, 19.0)
PROVINCE_LONS = (99.0, 100.0)

HINDCAST_YEARS = list(range(1993, 2017))   # C3S reference period
LEADS = [2, 3, 4, 5, 6]                     # forecastMonth: 1 = start month
SEASON_LEADS = [2, 3, 4]                    # "next 3 months" season

# Centres to try. The system number is discovered from the CDS constraints
# for the chosen start date; FALLBACK_SYSTEMS is only used if that lookup
# fails. Update the fallbacks when C3S announces upgrades.
CENTRES = {
    "ecmwf": "ECMWF",
    "ukmo": "UK Met Office",
    "meteo_france": "Météo-France",
    "dwd": "DWD",
    "cmcc": "CMCC",
    "ncep": "NCEP",
    "jma": "JMA",
    "eccc": "ECCC",
    "bom": "BoM",
}
FALLBACK_SYSTEMS = {
    "ecmwf": ["51"], "ukmo": ["604"], "meteo_france": ["9"], "dwd": ["22"],
    "cmcc": ["4"], "ncep": ["2"], "jma": ["3"], "eccc": ["4", "5"], "bom": ["2"],
}
# Centres that legitimately run more than one system at the same time.
MULTI_SYSTEM_CENTRES = {"eccc"}

VARIABLES = {"2m_temperature": "t2m", "total_precipitation": "tprate"}

ROOT = Path(__file__).resolve().parent.parent
OUT_JSON = ROOT / "data" / "c3s_seasonal.json"
CACHE_DIR = Path(os.environ.get("C3S_CACHE_DIR", ROOT / ".c3s_cache"))

THAI_MONTHS = ["", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
               "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]


# --------------------------------------------------------------------------- #
# Small date helpers
# --------------------------------------------------------------------------- #
def add_months(year: int, month: int, n: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + n
    return idx // 12, idx % 12 + 1


def valid_month(init_year: int, init_month: int, lead: int) -> tuple[int, int]:
    """forecastMonth 1 is the start month itself."""
    return add_months(init_year, init_month, lead - 1)


def latest_release(now: datetime | None = None) -> tuple[int, int]:
    """C3S publishes on the 13th at 12 UTC. Before that, use last month."""
    now = now or datetime.now(timezone.utc)
    if now.day > 13 or (now.day == 13 and now.hour >= 13):
        return now.year, now.month
    return add_months(now.year, now.month, -1)


# --------------------------------------------------------------------------- #
# CDS access
# --------------------------------------------------------------------------- #
def make_client():
    from ecmwf.datastores import Client

    key = os.environ.get("CDSAPI_KEY")
    if not key:
        sys.exit("CDSAPI_KEY is not set (CDS personal access token).")
    return Client(url=CDS_URL, key=key, progress=False)


def _as_list(v):
    if v is None:
        return []
    return [str(x) for x in (v if isinstance(v, (list, tuple, set)) else [v])]


def discover_systems(client, centre: str, year: int, month: int) -> list[str]:
    """Systems that have a forecast for this start AND a hindcast."""
    try:
        fc = client.apply_constraints(DATASET, {
            "originating_centre": centre, "year": [str(year)], "month": [f"{month:02d}"],
            "product_type": ["monthly_mean"],
        })
        systems = sorted(_as_list(fc.get("system")), key=lambda s: int(s) if s.isdigit() else 0)
        ok = []
        for s in systems:
            hc = client.apply_constraints(DATASET, {
                "originating_centre": centre, "system": [s], "month": [f"{month:02d}"],
                "product_type": ["monthly_mean"],
            })
            years = {int(y) for y in _as_list(hc.get("year")) if y.isdigit()}
            if len(years & set(HINDCAST_YEARS)) >= 20:
                ok.append(s)
        if not ok:
            return []
        return ok if centre in MULTI_SYSTEM_CENTRES else [ok[-1]]
    except Exception as exc:  # constraints endpoint unavailable -> fallback
        log.warning("constraints lookup failed for %s (%s); using fallback", centre, exc)
        return FALLBACK_SYSTEMS.get(centre, [])


def build_request(centre: str, system: str, years: list[int], month: int) -> dict:
    return {
        "originating_centre": centre,
        "system": system,
        "variable": list(VARIABLES),
        "product_type": ["monthly_mean"],
        "year": [str(y) for y in years],
        "month": [f"{month:02d}"],
        "leadtime_month": [str(l) for l in LEADS],
        "area": AREA,
        "data_format": "grib",
    }


def download(client, centre: str, system: str, years: list[int], month: int,
             kind: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tag = f"{years[0]}" if len(years) == 1 else f"{years[0]}-{years[-1]}"
    target = CACHE_DIR / f"{centre}_s{system}_{kind}_{tag}_m{month:02d}.grib"
    if target.exists() and target.stat().st_size > 0:
        log.info("cache hit %s", target.name)
        return target
    log.info("requesting %s system %s %s %s-%02d", centre, system, kind, tag, month)
    client.retrieve(DATASET, build_request(centre, system, years, month), str(target))
    return target


# --------------------------------------------------------------------------- #
# Decoding: GRIB -> {var: {year: array[member, lead, lat, lon]}}
# --------------------------------------------------------------------------- #
def open_grib(path: Path):
    import xarray as xr

    # time_dims as recommended in the C3S documentation: gives a forecastMonth
    # dimension and keeps lagged start dates (UKMO, NCEP, ...) on "time".
    return xr.open_dataset(
        path, engine="cfgrib",
        backend_kwargs={"time_dims": ("forecastMonth", "time"), "indexpath": ""},
    )


def split_by_year(ds, var: str) -> tuple[dict[int, np.ndarray], np.ndarray, np.ndarray]:
    """Return {nominal year: array[member, lead, lat, lon]} plus lats, lons.

    Lagged-start systems store members on several "time" values (e.g. late in
    the previous month). Members are the (time, number) pairs that contain
    data; the nominal year is that of time + 16 days.
    """
    da = ds[var]
    for dim in ("number", "time"):
        if dim not in da.dims:
            da = da.expand_dims(dim)
    da = da.transpose("number", "forecastMonth", "time", "latitude", "longitude")
    da = da.sel(forecastMonth=[l for l in LEADS if l in da["forecastMonth"].values])
    if list(da["forecastMonth"].values) != LEADS:
        raise ValueError(f"missing lead months in {var}: {da['forecastMonth'].values}")

    times = da["time"].values.astype("datetime64[D]").astype(datetime)
    nominal_years = np.array([(t + timedelta(days=16)).year for t in times])
    vals = da.values  # number, lead, time, lat, lon

    out: dict[int, np.ndarray] = {}
    for y in np.unique(nominal_years):
        sel = vals[:, :, nominal_years == y]            # number, lead, t, lat, lon
        sel = np.moveaxis(sel, 2, 1)                     # number, t, lead, lat, lon
        sel = sel.reshape(-1, *sel.shape[2:])            # member, lead, lat, lon
        keep = ~np.all(np.isnan(sel), axis=(1, 2, 3))
        if keep.any():
            out[int(y)] = sel[keep]
    lats = da["latitude"].values.astype(float)
    lons = da["longitude"].values.astype(float)
    return out, lats, lons


def to_physical(var: str, arr: np.ndarray, init_year_of: int, init_month: int) -> np.ndarray:
    """t2m K -> degC ; tprate m/s -> mm/month (valid month length)."""
    if var == "t2m":
        return arr - 273.15
    days = np.array([
        calendar.monthrange(*valid_month(init_year_of, init_month, l))[1] for l in LEADS
    ], dtype=float)
    return arr * 1000.0 * 86400.0 * days[None, :, None, None]


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
def tercile_probs(fc: np.ndarray, hc_pooled: np.ndarray) -> np.ndarray:
    """fc: [member, ...], hc_pooled: [sample, ...] -> [3, ...] (below, normal, above)."""
    lo, hi = np.nanquantile(hc_pooled, [1 / 3, 2 / 3], axis=0)
    valid = ~np.isnan(fc)
    n = valid.sum(axis=0).astype(float)
    n[n == 0] = np.nan
    below = np.sum(valid & (fc < lo), axis=0) / n
    above = np.sum(valid & (fc > hi), axis=0) / n
    return np.stack([below, 1 - below - above, above])


def trend_shift(hc_by_year: dict[int, np.ndarray], target_year: float) -> np.ndarray:
    """Shift each hindcast year along the linear trend of its ensemble mean.

    Returns the pooled [sample, ...] hindcast as it would look in target_year.
    """
    years = np.array(sorted(hc_by_year), dtype=float)
    means = np.stack([np.nanmean(hc_by_year[int(y)], axis=0) for y in years])  # year, ...
    flat = means.reshape(len(years), -1)
    good = ~np.isnan(flat).any(axis=0)
    slope = np.full(flat.shape[1], 0.0)
    if good.any():
        slope[good] = np.polyfit(years, flat[:, good], 1)[0]
    slope = slope.reshape(means.shape[1:])
    shifted = [hc_by_year[int(y)] + slope * (target_year - y) for y in years]
    return np.concatenate(shifted, axis=0)


def season_aggregate(arr: np.ndarray, var: str) -> np.ndarray:
    """[member, lead, ...] -> [member, ...] over SEASON_LEADS (sum rain, mean T)."""
    idx = [LEADS.index(l) for l in SEASON_LEADS]
    sub = arr[:, idx]
    return np.nansum(sub, axis=1) if var == "tprate" else np.nanmean(sub, axis=1)


def area_mean(arr: np.ndarray, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
    """Cos-lat weighted mean over the province cells; keeps leading dims."""
    lat_ok = (lats >= PROVINCE_LATS[0]) & (lats <= PROVINCE_LATS[1])
    lon_ok = (lons >= PROVINCE_LONS[0]) & (lons <= PROVINCE_LONS[1])
    sub = arr[..., lat_ok, :][..., lon_ok]
    w = np.cos(np.deg2rad(lats[lat_ok]))[:, None] * np.ones(lon_ok.sum())[None, :]
    return np.nansum(sub * w, axis=(-2, -1)) / np.sum(w)


def r(x, nd=3):
    if x is None:
        return None
    x = float(x)
    return None if np.isnan(x) else round(x, nd)


def probs_dict(p) -> dict:
    return {"below": r(p[0]), "normal": r(p[1]), "above": r(p[2])}


def most_likely(p: dict) -> str:
    return max(("below", "normal", "above"), key=lambda k: (p[k] or 0))


# --------------------------------------------------------------------------- #
# Per-system processing
# --------------------------------------------------------------------------- #
def process_system(fc_path: Path, hc_path: Path, init_year: int, init_month: int) -> dict:
    fc_ds, hc_ds = open_grib(fc_path), open_grib(hc_path)
    result = {"vars": {}}
    for var in VARIABLES.values():
        fc_years, lats, lons = split_by_year(fc_ds, var)
        if init_year not in fc_years:
            raise ValueError(f"forecast year {init_year} not in file")
        fc = to_physical(var, fc_years[init_year], init_year, init_month)
        hc_raw, _, _ = split_by_year(hc_ds, var)
        hc = {y: to_physical(var, a, y, init_month) for y, a in hc_raw.items()
              if y in HINDCAST_YEARS}
        if len(hc) < 20:
            raise ValueError(f"only {len(hc)} hindcast years for {var}")

        # Monthly + season fields: [member, period, lat, lon]
        def periods(a):
            return np.concatenate([a, season_aggregate(a, var)[:, None]], axis=1)

        fc_p = periods(fc)
        hc_p = {y: periods(a) for y, a in hc.items()}
        hc_pool = np.concatenate(list(hc_p.values()), axis=0)

        grid_probs = tercile_probs(fc_p, hc_pool)                      # 3, period, lat, lon
        fc_area = area_mean(fc_p, lats, lons)                          # member, period
        hc_area_by_year = {y: area_mean(a, lats, lons) for y, a in hc_p.items()}
        hc_area_pool = np.concatenate(list(hc_area_by_year.values()), axis=0)
        area_probs = tercile_probs(fc_area, hc_area_pool)              # 3, period
        clim = np.nanmean(hc_area_pool, axis=0)
        fcm = np.nanmean(fc_area, axis=0)

        entry = {
            "area_probs": area_probs, "grid_probs": grid_probs,
            "fc_mean": fcm, "clim_mean": clim,
            "n_fc": int(fc.shape[0]), "n_hc": int(hc_pool.shape[0]),
            "lats": lats, "lons": lons,
        }
        if var == "t2m":
            entry["area_probs_trend"] = tercile_probs(
                fc_area, trend_shift(hc_area_by_year, init_year + (init_month - 1) / 12))
        result["vars"][var] = entry
    return result


# --------------------------------------------------------------------------- #
# Assemble JSON
# --------------------------------------------------------------------------- #
def period_meta(init_year: int, init_month: int) -> list[dict]:
    out = []
    for lead in LEADS:
        y, m = valid_month(init_year, init_month, lead)
        out.append({"id": f"{y}-{m:02d}", "type": "month", "lead": lead,
                    "label": f"{THAI_MONTHS[m]} {y + 543}"})
    first = valid_month(init_year, init_month, SEASON_LEADS[0])
    last = valid_month(init_year, init_month, SEASON_LEADS[-1])
    letters = "".join(
        "JFMAMJJASOND"[valid_month(init_year, init_month, l)[1] - 1] for l in SEASON_LEADS)
    out.append({"id": letters, "type": "season", "leads": SEASON_LEADS,
                "label": f"{THAI_MONTHS[first[1]]} {first[0] + 543} – "
                         f"{THAI_MONTHS[last[1]]} {last[0] + 543}"})
    return out


def assemble(results: dict, failed: list, init_year: int, init_month: int) -> dict:
    periods = period_meta(init_year, init_month)
    keys = list(results)
    any_sys = results[keys[0]]
    lats = any_sys["vars"]["t2m"]["lats"]
    lons = any_sys["vars"]["t2m"]["lons"]

    area_mean_out = {}
    grid_out = {"lats": [float(x) for x in lats], "lons": [float(x) for x in lons],
                "resolution_deg": 1.0, "fields": {}}

    for pi, per in enumerate(periods):
        block = {}
        for var, label in (("tprate", "precip"), ("t2m", "t2m")):
            sys_out = {}
            stack, stack_trend = [], []
            for k in keys:
                v = results[k]["vars"][var]
                p = probs_dict(v["area_probs"][:, pi])
                d = {**p, "most_likely": most_likely(p),
                     "fc_mean": r(v["fc_mean"][pi], 2), "clim_mean": r(v["clim_mean"][pi], 2)}
                stack.append(v["area_probs"][:, pi])
                if var == "t2m":
                    stack_trend.append(v["area_probs_trend"][:, pi])
                    d["trend_adjusted"] = probs_dict(v["area_probs_trend"][:, pi])
                sys_out[k] = d
            mme = probs_dict(np.nanmean(stack, axis=0))
            fc_mean = np.nanmean([results[k]["vars"][var]["fc_mean"][pi] for k in keys])
            clim_mean = np.nanmean([results[k]["vars"][var]["clim_mean"][pi] for k in keys])
            ml = most_likely(mme)
            item = {
                "mme": mme, "most_likely": ml,
                "agreement": sum(1 for k in keys if sys_out[k]["most_likely"] == ml),
                "n_systems": len(keys),
                "fc_mean": r(fc_mean, 2), "clim_mean": r(clim_mean, 2),
                "systems": sys_out,
            }
            if var == "tprate":
                item["anom_pct"] = r(100 * (fc_mean / clim_mean - 1), 1) if clim_mean else None
                item["unit"] = "mm"
            else:
                item["anom_c"] = r(fc_mean - clim_mean, 2)
                item["unit"] = "°C"
                tm = probs_dict(np.nanmean(stack_trend, axis=0))
                item["mme_trend_adjusted"] = tm
                item["most_likely_trend_adjusted"] = most_likely(tm)
            block[label] = item
        area_mean_out[per["id"]] = block

        # Grid MME for the map overlay
        for var, cat, name in (("tprate", 0, "precip_below"), ("tprate", 2, "precip_above"),
                               ("t2m", 2, "t2m_above")):
            g = np.nanmean([results[k]["vars"][var]["grid_probs"][cat, pi] for k in keys], axis=0)
            grid_out["fields"].setdefault(name, {})[per["id"]] = [
                [r(x) for x in row] for row in g]

    return {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "init": f"{init_year}-{init_month:02d}",
        "source": "Copernicus Climate Change Service (C3S), " + DATASET,
        "reference_period": f"{HINDCAST_YEARS[0]}-{HINDCAST_YEARS[-1]}",
        "method": ("Terciles from each system's hindcast (all years x members pooled), "
                   "probability = fraction of forecast members per category, "
                   "multi-system = unweighted mean of system probabilities (as C3S). "
                   "trend_adjusted: hindcast shifted along its own linear trend to the "
                   "forecast year before computing terciles (temperature only)."),
        "area": {"download_box_NWSE": AREA,
                 "province_cells_lat": list(PROVINCE_LATS),
                 "province_cells_lon": list(PROVINCE_LONS)},
        "systems": [{"id": k, "centre": k.split(":")[0],
                     "label": f'{CENTRES[k.split(":")[0]]} (s{k.split(":")[1]})',
                     "n_forecast_members": results[k]["vars"]["t2m"]["n_fc"],
                     "n_hindcast_samples": results[k]["vars"]["t2m"]["n_hc"]}
                    for k in keys],
        "systems_failed": failed,
        "periods": periods,
        "area_mean": area_mean_out,
        "grid": grid_out,
    }


# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--init", help="start month YYYY-MM (default: latest release)")
    ap.add_argument("--centres", help="comma list, default all")
    ap.add_argument("--out", default=str(OUT_JSON))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.init:
        init_year, init_month = (int(x) for x in args.init.split("-"))
    else:
        init_year, init_month = latest_release()
    centres = args.centres.split(",") if args.centres else list(CENTRES)
    log.info("start date %d-%02d, centres %s", init_year, init_month, centres)

    client = make_client()
    results, failed = {}, []
    for centre in centres:
        systems = discover_systems(client, centre, init_year, init_month)
        if not systems:
            failed.append({"centre": centre, "reason": "no system for this start date"})
            continue
        for system in systems:
            key = f"{centre}:{system}"
            try:
                fc = download(client, centre, system, [init_year], init_month, "fc")
                hc = download(client, centre, system, HINDCAST_YEARS, init_month, "hc")
                results[key] = process_system(fc, hc, init_year, init_month)
                log.info("ok %s", key)
            except Exception as exc:
                log.warning("skip %s: %s", key, exc)
                failed.append({"centre": centre, "system": system, "reason": str(exc)[:200]})

    if not results:
        log.error("no system succeeded; leaving %s unchanged", args.out)
        return 1

    data = assemble(results, failed, init_year, init_month)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("wrote %s (%d systems, %d failed)", out, len(results), len(failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
