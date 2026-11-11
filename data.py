"""Data layer: load the HE study file locally, derive daily measures, build page payloads.

The real de-identified file never lives in this repo. By default it is the single .rds file in
../de-identified data/ (override with HE_DATA_FILE); when it is missing the app falls back to a
small synthetic cohort so the UI still runs anywhere.
"""
from __future__ import annotations

import copy
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

HERE = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = HERE.parent / "de-identified data"


def default_data_file(folder: Path = DEFAULT_DATA_DIR) -> Path:
    """The single .rds file in `folder`. Raises when there are none or several: then set HE_DATA_FILE."""
    found = sorted(p for p in folder.glob("*.rds") if not p.name.startswith("."))
    if len(found) != 1:
        raise FileNotFoundError(f"Expected exactly one .rds file in {folder}, found {len(found)}. "
                                "Set HE_DATA_FILE to the study file.")
    return found[0]


def _data_file() -> Path | None:
    if os.environ.get("HE_DATA_FILE"):
        return Path(os.environ["HE_DATA_FILE"]).expanduser()
    try:
        return default_data_file()
    except FileNotFoundError:
        return None  # auto mode falls back to synthetic; study mode re-raises in load()


DATA_FILE = _data_file()
DATA_SOURCE = os.environ.get("HE_DATA_SOURCE", "auto").lower()
# Hosted public demo: always the in-memory synthetic cohort; study data and HE_MRN_MAP are never read.
PUBLIC_DEMO = os.environ.get("HE_PUBLIC_DEMO", "").strip().lower() in {"1", "true", "yes"}
SYNTHETIC_FILE = Path(os.environ.get(
    "HE_SYNTHETIC_FILE", HERE / "synthetic_data" / "synthetic_he.csv.gz")).expanduser()
# Cache sits next to the data (outside the repo) so a converted copy can't be committed by accident.
_DATA_ROOT = (DATA_FILE.parent if DATA_FILE else DEFAULT_DATA_DIR).parent
CACHE_DIR = Path(os.environ.get("HE_CACHE_DIR", _DATA_ROOT / "exploration_outputs" / "_cache")).expanduser()

SLOTS, DAY_STARTS_AT = 288, 4  # Oura activity day: 04:00 → 04:00 local, 5-minute slots

METRICS = {
    "night_rest_h": {"label": "Night rest", "unit": "h", "digits": 1, "higher_is": "better",
                     "help": "Longest rest bout between 6 pm and noon (sleep proxy from the activity string)"},
    "sri": {"label": "Rest regularity", "unit": "SRI", "digits": 0, "higher_is": "better",
            "help": "How alike consecutive days are in resting vs. not resting (100 = identical)"},
    "rest_mid_6pm": {"label": "Rest midpoint", "unit": "clock", "digits": 1, "higher_is": None,
                       "help": "Middle of the night rest bout (circadian timing)"},
    "average_hrv": {"label": "HRV", "unit": "ms", "digits": 0, "higher_is": "better",
                    "help": "Average heart rate variability (Oura)"},
    "average_heart_rate": {"label": "Average HR", "unit": "bpm", "digits": 0, "higher_is": "worse",
                           "help": "Average heart rate (Oura); 0 bpm readings removed"},
    "steps": {"label": "Steps", "unit": "/day", "digits": 0, "higher_is": "better", "help": "Daily steps"},
    "spo2": {"label": "SpO₂", "unit": "%", "digits": 1, "higher_is": "better",
             "help": "Blood oxygen; not available every night"},
}

GROUPS = {
    "decomp": {"label": "Decompensation", "levels": {0: "Compensated", 1: "Decompensated"}},
    "che_phes": {"label": "Covert HE (PHES)", "levels": {"No": "No covert HE", "Yes": "Covert HE"}},
    "ctp_cat": {"label": "Child-Pugh", "levels": {"Child A": "Child A", "Child B/C": "Child B/C"}},
    "anyOHE": {"label": "Overt HE (ever)", "levels": {0: "No overt HE", 1: "Overt HE"}},
    "he_meds": {"label": "HE meds at enrolment", "levels": {"No": "No HE meds", "Yes": "On HE meds"}},
    "ascites": {"label": "Ascites", "levels": {0: "No ascites", 1: "Ascites"}},
    "sex": {"label": "Sex", "levels": {"Female": "Female", "Male": "Male"}},
}

# Tiles on the patient profile, in order. Trim or reorder here once clinicians have prioritised measures.
PATIENT_TILES = ["night_rest_h", "sri", "rest_mid_6pm", "average_hrv", "average_heart_rate", "steps", "spo2"]

BASELINE_FIELDS = [
    ("sex", "Sex"), ("age_yrs", "Age"), ("ctp_class", "Child-Pugh"), ("meld", "MELD"),
    ("cirrhosis_etiology", "Etiology"), ("decomp", "Decompensated"), ("anyhe", "Any HE"),
    ("anyOHE", "Overt HE"), ("che_phes", "Covert HE (PHES)"), ("phes_score", "PHES score"),
    ("he_meds", "HE meds"), ("ascites", "Ascites"), ("variceal_bleeding_history", "Variceal bleed"),
    ("psqiglobal", "PSQI"), ("cldq_total", "CLDQ total"), ("liver_frailty_index", "Frailty index"),
]

R_TO_CSV = r"""
args <- commandArgs(trailingOnly = TRUE)
x <- readRDS(args[1])
# Per-minute MET (1,440 values/day) → 288 five-minute means, aligned with class_5_min (day starts 04:00)
if ("met_val" %in% names(x)) {
  x$met_5min <- vapply(x$met_val, function(v) {
    if (length(v) != 1440) return(NA_character_)
    paste(round(colMeans(matrix(v, nrow = 5)), 2), collapse = ",")
  }, character(1))
}
x <- x[, !vapply(x, is.list, logical(1)), drop = FALSE]
x[] <- lapply(x, function(v) if (inherits(v, "haven_labelled")) haven::as_factor(v) else v)
write.csv(x, args[2], row.names = FALSE, na = "")
"""


def _read_rds(path: Path) -> pd.DataFrame:
    # pyreadr can't parse haven-labelled or list columns, so R converts the file once.
    cache = CACHE_DIR / (path.stem + ".v2.csv")  # v2 adds met_5min
    if not cache.exists() or cache.stat().st_mtime < path.stat().st_mtime:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        script = CACHE_DIR / "rds_to_csv.R"
        script.write_text(R_TO_CSV)
        subprocess.run(["Rscript", str(script), str(path), str(cache)], check=True)
    return pd.read_csv(cache, low_memory=False, dtype={"class_5_min": str, "name": str, "met_5min": str})


# Every assumption behind the synthetic cohort, in one place. These are hand-picked, round,
# clinically plausible values, not estimates from the study data. docs/synthetic-data.md explains
# each one. Override any of them with synthetic_cohort(config=...) or the generator's --config JSON.
# Ranges [lo, hi] are inclusive integers, except cldq_total (uniform float). A list of categories is
# drawn uniformly; a {category: probability} dict is drawn with those weights.
SYNTHETIC_DEFAULTS: dict = {
    "decompensated_share": 0.5,  # P(decompensated); drives every built-in clinical-wearable relationship
    "baseline": {  # baseline fields drawn the same way for everyone
        "sex": ["Female", "Male"],
        "age_yrs": [35, 74],
        "cirrhosis_etiology": ["Alcohol", "NAFLD", "Hepatitis C"],
        "che_phes": {"No": 0.6, "Yes": 0.4},  # independent of decompensation
        "variceal_bleeding_history": "No",
        "phes_score": [-8, 2],
        "psqiglobal": [3, 14],
        "cldq_total": [3.0, 6.5],
    },
    "compensated": {
        "anyhe": 0.0, "anyOHE": 0.0, "he_meds": 0.0, "ascites": 0.0,  # probabilities
        "ctp_class": "Child A", "meld": [6, 11],
        "bedtime_h": 23.3, "bedtime_night_sd": 0.6, "rest_h": 6.6,  # clock hours (24.5 = 00:30), hours
        "hrv_ms": 28, "hr_bpm": 66, "steps": 8500,  # patient means are drawn around these
    },
    "decompensated": {
        "anyhe": 0.6, "anyOHE": 0.4, "he_meds": 0.5, "ascites": 0.5,
        "ctp_class": "Child B", "meld": [12, 21],
        "bedtime_h": 24.5, "bedtime_night_sd": 1.2, "rest_h": 5.2,
        "hrv_ms": 20, "hr_bpm": 72, "steps": 5500,
    },
    "between_patient_sd": {"bedtime_h": 0.6, "rest_h": 0.4, "hrv_ms": 4, "hr_bpm": 4, "steps": 1500},
    "day_to_day_sd": {"rest_h": 1.0, "hrv_ms": 4, "hr_bpm": 3, "steps": 2000},
    "floors": {"rest_h": 2.5, "hrv_ms": 5, "steps": 0},
    "activity": {
        "active_share": 0.25,  # share of non-rest 5-minute slots that are low/medium/high activity
        "classes": {"3": 0.7, "4": 0.25, "5": 0.05},  # low / medium / high
        "met_by_class": [0.0, 0.95, 1.2, 2.2, 3.8, 6.5],  # ring off, rest, inactive, low, medium, high
        "met_jitter": [0.85, 1.15],  # per-slot multiplier
    },
    "wear": {"invalid_day_share": 0.15, "gap_slots": 90, "gap_start_slot": [0, 199]},
    "spo2": {"day_share": 0.4, "mean": 97, "sd": 1},
}


def synthetic_config(overrides: dict | None = None) -> dict:
    """Defaults with `overrides` applied. Sections merge key by key; values inside a section are replaced whole."""
    cfg = copy.deepcopy(SYNTHETIC_DEFAULTS)
    for key, val in (overrides or {}).items():
        if key not in cfg:
            raise ValueError(f"unknown synthetic setting: {key}")
        if isinstance(cfg[key], dict):
            if unknown := set(val) - set(cfg[key]):
                raise ValueError(f"unknown synthetic setting(s) in {key}: {', '.join(sorted(unknown))}")
            cfg[key].update(copy.deepcopy(val))
        else:
            cfg[key] = val
    return cfg


def _pick(rng: np.random.Generator, spec):
    if isinstance(spec, dict):
        return rng.choice(list(spec), p=list(spec.values()))
    return rng.choice(spec) if isinstance(spec, list) else spec


def _chance(rng: np.random.Generator, p: float) -> bool:
    # No draw at p = 0 or 1, so certain outcomes don't shift the random stream.
    return p >= 1 or (p > 0 and rng.random() < p)


def _int_between(rng: np.random.Generator, lo_hi: list[int]) -> int:
    return int(rng.integers(lo_hi[0], lo_hi[1] + 1))


def synthetic_cohort(n: int = 24, seed: int = 7, min_days: int = 90, max_days: int = 180,
                     config: dict | None = None) -> pd.DataFrame:
    """A fake cohort in the study file's format: one row per participant-day. See docs/synthetic-data.md."""
    if n < 1 or min_days < 1 or max_days < min_days:
        raise ValueError("participants and days must be positive, with min_days <= max_days")
    cfg = synthetic_config(config)
    b, sd, day_sd, floor = cfg["baseline"], cfg["between_patient_sd"], cfg["day_to_day_sd"], cfg["floors"]
    act, wear, spo2 = cfg["activity"], cfg["wear"], cfg["spo2"]
    met_by_class = np.array(act["met_by_class"], dtype=float)
    clock = DAY_STARTS_AT + np.arange(SLOTS) * 5 / 60
    rng = np.random.default_rng(seed)
    rows = []
    # Draw order is part of the output: reordering these calls changes every dataset for a given seed.
    for i in range(n):
        pid = f"DEMO-{i + 1:02d}"
        sick = _chance(rng, cfg["decompensated_share"])
        s = cfg["decompensated" if sick else "compensated"]
        base = {"name": pid, "sex": _pick(rng, b["sex"]), "age_yrs": _int_between(rng, b["age_yrs"]), "decomp": float(sick)}
        base["anyhe"] = float(_chance(rng, s["anyhe"]))
        base["anyOHE"] = float(_chance(rng, s["anyOHE"]))
        base["che_phes"] = _pick(rng, b["che_phes"])
        base["he_meds"] = "Yes" if _chance(rng, s["he_meds"]) else "No"
        base["ascites"] = float(_chance(rng, s["ascites"]))
        base["ctp_class"] = s["ctp_class"]
        base["ctp_cat"] = "Child A" if s["ctp_class"] == "Child A" else "Child B/C"
        base["meld"] = _int_between(rng, s["meld"])
        base["cirrhosis_etiology"] = _pick(rng, b["cirrhosis_etiology"])
        base["variceal_bleeding_history"] = _pick(rng, b["variceal_bleeding_history"])
        base["phes_score"] = _int_between(rng, b["phes_score"])
        base["psqiglobal"] = _int_between(rng, b["psqiglobal"])
        base["cldq_total"] = round(float(rng.uniform(*b["cldq_total"])), 1)
        base["liver_frailty_index"] = np.nan
        bed = rng.normal(s["bedtime_h"], sd["bedtime_h"])
        length = rng.normal(s["rest_h"], sd["rest_h"])
        hrv0, hr0, steps0 = rng.normal(s["hrv_ms"], sd["hrv_ms"]), rng.normal(s["hr_bpm"], sd["hr_bpm"]), rng.normal(s["steps"], sd["steps"])
        for day in range(1, _int_between(rng, [min_days, max_days]) + 1):
            start = bed + rng.normal(0, s["bedtime_night_sd"])
            end = start + max(floor["rest_h"], rng.normal(length, day_sd["rest_h"]))
            slots = np.array(["2"] * SLOTS)
            rest = ((clock >= start) & (clock < end)) | ((clock + 24 >= start) & (clock + 24 < end))
            slots[rest] = "1"
            day_mask = ~rest & (rng.random(SLOTS) < act["active_share"])
            slots[day_mask] = rng.choice(list(act["classes"]), size=day_mask.sum(), p=list(act["classes"].values()))
            if rng.random() <= wear["invalid_day_share"]:
                slots[_int_between(rng, wear["gap_start_slot"]):][:wear["gap_slots"]] = "0"
            met = met_by_class[slots.astype(int)] * rng.uniform(*act["met_jitter"], SLOTS)
            non_wear_pct = float((slots == "0").mean())
            rows.append({**base, "met_5min": ",".join(f"{m:.2f}" for m in met), "days_since_start": day,
                         "day_type": "Weekend" if day % 7 in (6, 0) else "Weekday",
                         "valid80": int(non_wear_pct <= 0.2), "non_wear_pct": non_wear_pct,
                         "average_hrv": max(floor["hrv_ms"], rng.normal(hrv0, day_sd["hrv_ms"])),
                         "average_heart_rate": rng.normal(hr0, day_sd["hr_bpm"]),
                         "steps": max(floor["steps"], rng.normal(steps0, day_sd["steps"])),
                         "spo2": rng.normal(spo2["mean"], spo2["sd"]) if rng.random() < spo2["day_share"] else np.nan,
                         "class_5_min": "".join(slots)})
    return pd.DataFrame(rows)


def synthetic_table() -> tuple[pd.DataFrame, str]:
    """Raw synthetic participant-days for the notebooks, and where they came from.

    SYNTHETIC_FILE if it exists, otherwise the default in-memory cohort (unlike load(), which needs
    the file when HE_DATA_SOURCE=synthetic). The public demo always uses the in-memory cohort.
    """
    if not PUBLIC_DEMO and SYNTHETIC_FILE.exists():
        return (pd.read_csv(SYNTHETIC_FILE, low_memory=False, dtype={"class_5_min": str, "name": str, "met_5min": str}),
                str(SYNTHETIC_FILE))
    return synthetic_cohort(), "data.synthetic_cohort() with the default settings (seed 7)"


_bout_re = re.compile(r"1(?:[0-5]{0,2}1)*")
_win_start, _win_end = (18 - DAY_STARTS_AT) * 12, SLOTS + (12 - DAY_STARTS_AT) * 12


def _night_rest(today, tomorrow):
    if pd.isna(today) or pd.isna(tomorrow):
        return (np.nan, np.nan)
    window = (today + tomorrow)[_win_start:_win_end]
    best = max(_bout_re.finditer(window), key=lambda m: m.end() - m.start(), default=None)
    if best is None or best.end() - best.start() < 24:  # under 2 h is not a night
        return (np.nan, np.nan)
    return ((best.end() - best.start()) * 5 / 60, (best.start() + best.end()) / 2 * 5 / 60)


def _sri(today, tomorrow):
    if pd.isna(today) or pd.isna(tomorrow):
        return np.nan
    a = np.frombuffer(today.encode(), dtype=np.uint8) - 48
    b = np.frombuffer(tomorrow.encode(), dtype=np.uint8) - 48
    worn = (a != 0) & (b != 0)
    if worn.sum() < SLOTS * 0.8:
        return np.nan
    return 200 * ((a == 1) == (b == 1))[worn].mean() - 100


@lru_cache(maxsize=1)
def load() -> tuple[pd.DataFrame, str]:
    """Return (daily table with derived measures, source label). Cached; restart to reload."""
    if DATA_SOURCE not in {"auto", "study", "synthetic"}:
        raise ValueError("HE_DATA_SOURCE must be auto, study, or synthetic")
    if PUBLIC_DEMO:
        if DATA_SOURCE == "study" or os.environ.get("HE_MRN_MAP"):
            raise RuntimeError("HE_PUBLIC_DEMO=1 serves synthetic data only: unset HE_DATA_SOURCE=study and HE_MRN_MAP")
        # Not even HE_SYNTHETIC_FILE: on a host it could point at anything.
        df, source = synthetic_cohort(), "synthetic"
    elif DATA_SOURCE == "synthetic":
        if not SYNTHETIC_FILE.exists():
            raise FileNotFoundError(f"Synthetic data not found: {SYNTHETIC_FILE}. Run the generator first.")
        df = pd.read_csv(SYNTHETIC_FILE, low_memory=False,
                         dtype={"class_5_min": str, "name": str, "met_5min": str})
        source = "synthetic"
    elif DATA_FILE and DATA_FILE.exists():
        df, source = _read_rds(DATA_FILE), "study"
    elif DATA_SOURCE == "study":
        if DATA_FILE is None:
            default_data_file(DEFAULT_DATA_DIR)  # raises, saying how many .rds files it found
        raise FileNotFoundError(f"Study data not found: {DATA_FILE}")
    else:
        df, source = synthetic_cohort(), "synthetic"
    df = df.sort_values(["name", "days_since_start"]).reset_index(drop=True)
    strings = df["class_5_min"].where(df["class_5_min"].str.fullmatch(rf"[0-5]{{{SLOTS}}}", na=False))
    next_ok = (df["name"].shift(-1) == df["name"]) & (df["days_since_start"].shift(-1) == df["days_since_start"] + 1)
    nxt = strings.shift(-1).where(next_ok)
    valid_day = pd.to_numeric(df["valid80"], errors="coerce").eq(1)
    night_pair_ok = next_ok & valid_day & valid_day.shift(-1, fill_value=False)
    df[["night_rest_h", "rest_mid_6pm"]] = pd.DataFrame(
        [_night_rest(t, n) for t, n in zip(strings.where(night_pair_ok), nxt.where(night_pair_ok))], index=df.index)
    df["sri"] = [_sri(t, n) for t, n in zip(strings, nxt)]
    df.loc[~df["night_rest_h"].between(2, 16), "night_rest_h"] = np.nan
    df["average_heart_rate"] = df["average_heart_rate"].where(df["average_heart_rate"] > 0)
    df["class_5_min"] = strings
    for col, spec in GROUPS.items():
        if col in df:
            df[f"grp_{col}"] = df[col].map(lambda v: int(v) if isinstance(v, float) and not np.isnan(v) else v).map(spec["levels"])
    return df, source


def _fmt(v, digits=1):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return int(round(float(v))) if digits == 0 else round(float(v), digits)


def _clock(h):
    if h is None or (isinstance(h, float) and np.isnan(h)):
        return None
    m = int(round(h * 60)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _baseline_value(v):
    if isinstance(v, np.generic):
        v = v.item()
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    if isinstance(v, float) and v.is_integer() and v in (0.0, 1.0):
        return "Yes" if v else "No"
    if isinstance(v, float):
        return int(v) if v.is_integer() else round(v, 1)
    return v


def _attention(g: pd.DataFrame) -> list[str]:
    """Measures whose last 7 valid days are beyond the patient's own 10th/90th percentile, in the worse
    direction. Stricter than the cards' middle-50% range so the flag isn't triggered by ordinary weeks."""
    full = g.set_index("days_since_start")
    flags = []
    for key in PATIENT_TILES:
        vals = full[key].where(full["valid80"] == 1)
        if vals.notna().sum() >= 14 and _is_worse(_status(vals, 0.1, 0.9)[0], METRICS[key]["higher_is"]):
            flags.append(METRICS[key]["label"])
    return flags


def _he_status(row: pd.Series) -> str | None:
    """Return the most specific supported HE status; leave incomplete/conflicting rows unknown."""
    if row.get("anyOHE") == 1:
        return "Overt"
    if row.get("che_phes") == "Yes":
        return "Covert"
    return "No" if row.get("anyhe") == 0 else None


@lru_cache(maxsize=1)
def patient_list() -> list[dict]:
    df, _ = load()
    valid = df[df["valid80"] == 1]
    out = []
    for pid, g in df.groupby("name", sort=True):
        v = valid[valid["name"] == pid]
        first = g.iloc[0]
        out.append({
            "id": pid,
            "sex": _baseline_value(first.get("sex")),
            "age": _baseline_value(first.get("age_yrs")),
            "ctp": _baseline_value(first.get("ctp_class")),
            "meld": _baseline_value(first.get("meld")),
            "he": _he_status(first),
            "decomp": _baseline_value(first.get("decomp")),
            "ascites": _baseline_value(first.get("ascites")),
            "he_meds": _baseline_value(first.get("he_meds")),
            "etiology": _baseline_value(first.get("cirrhosis_etiology")),
            "attention": _attention(g),
            "days": int(len(g)),
            "valid_pct": int(round(100 * (g["valid80"] == 1).mean())),
            "night_rest": _fmt(v["night_rest_h"].median()),
            "sri": _fmt(v["sri"].median(), 0),
            "hrv": _fmt(v["average_hrv"].median(), 0),
            "steps": _fmt(v["steps"].median(), 0),
            "spark": [_fmt(x) for x in v.set_index("days_since_start")["night_rest_h"].reindex(range(1, int(g["days_since_start"].max()) + 1))
                      .rolling(7, min_periods=3).mean().iloc[::3]],
        })
    return out


ACTIVITY_CLASSES = ["Ring off", "Rest", "Inactive", "Low", "Medium", "High"]
MRN_SYSTEM = os.environ.get("SMART_MRN_SYSTEM", "https://jupyterhealth.org/fhir/identifier/mrn")


def _met_rows(strings: pd.Series) -> np.ndarray:
    out = np.full((len(strings), SLOTS), np.nan)
    for i, s in enumerate(strings):
        if isinstance(s, str):
            vals = np.array(s.split(","), dtype=float)
            if len(vals) == SLOTS:
                out[i] = vals
    return out


def _status(vals: pd.Series, lo: float = 0.25, hi: float = 0.75):
    """Last 7 valid days vs. the patient's own usual range (by default the middle 50% of their days)."""
    q1, med, q3 = vals.quantile([lo, 0.5, hi])
    last7 = vals.dropna().iloc[-7:].median()
    status = "below" if last7 < q1 else "above" if last7 > q3 else "typical"
    return status, last7, q1, med, q3


def _is_worse(status: str, higher_is: str | None) -> bool:
    return higher_is is not None and status != "typical" and (status == "below") == (higher_is == "better")


def _trend(vals: pd.Series, spread: float) -> str:
    v = vals.dropna()
    if len(v) < 28 or not spread:
        return "Not enough data"
    recent, before = v.iloc[-14:].median(), v.iloc[-42:-14].median()
    change = (recent - before) / spread
    return "Increasing" if change > 0.5 else "Decreasing" if change < -0.5 else "Stable"


def patient_detail(pid: str) -> dict | None:
    df, source = load()
    g = df[df["name"] == pid]
    if g.empty:
        return None
    first = g.iloc[0]
    days = list(range(1, int(g["days_since_start"].max()) + 1))
    full = g.set_index("days_since_start").reindex(days)
    is_valid = full["valid80"].eq(1)
    weeks = [(d - 1) // 7 + 1 for d in days]
    series, tiles = {}, []
    for key in PATIENT_TILES:
        spec = METRICS[key]
        vals = full[key].where(is_valid)
        if vals.notna().sum() == 0:
            continue
        roll = vals.rolling(7, min_periods=3).mean()
        series[key] = {"values": [_fmt(x, 2) for x in vals], "rolling": [_fmt(x, 2) for x in roll]}
        recent = vals.dropna().iloc[-7:]
        status, last7, q1, med, q3 = _status(vals)
        weekly = vals.groupby(weeks).median()
        show = (lambda x: _clock(18 + x)) if spec["unit"] == "clock" else (lambda x: _fmt(x, spec["digits"]))
        tiles.append({"key": key, "label": spec["label"], "unit": "" if spec["unit"] == "clock" else spec["unit"],
                      "help": spec["help"], "last7": show(last7), "baseline": show(med),
                      "range": [show(q1), show(q3)], "status": status, "higher_is": spec["higher_is"],
                      "n_obs": int(vals.notna().sum()), "trend": _trend(vals, q3 - q1),
                      "spark": [_fmt(x, 2) for x in weekly], "band": [_fmt(q1, 2), _fmt(q3, 2)],
                      "clock": spec["unit"] == "clock"})

    valid_rows = g[(g["valid80"] == 1) & g["class_5_min"].notna()]
    met = _met_rows(valid_rows["met_5min"]) if "met_5min" in g else np.empty((0, SLOTS))
    cls = np.array([list(x) for x in valid_rows["class_5_min"]], dtype=int) if len(valid_rows) else np.empty((0, SLOTS), int)
    profile = None
    if len(cls):
        with np.errstate(all="ignore"):
            pct = np.nanpercentile(met, [10, 25, 50, 75, 90], axis=0) if np.isfinite(met).any() else None
        profile = {
            "hours": [round(DAY_STARTS_AT + i * 5 / 60, 3) for i in range(SLOTS)],
            "rest_share": [round(float(x), 3) for x in (cls == 1).mean(axis=0)],
            "met": None if pct is None else {k: [_fmt(x, 2) for x in row] for k, row in zip(["p10", "p25", "p50", "p75", "p90"], pct)},
            "days": int(len(cls)),
        }
        time_in_class = [{"label": ACTIVITY_CLASSES[k], "share": round(float((cls == k).mean()), 3),
                          "hours": round(float((cls == k).sum() * 5 / 60 / len(cls)), 1)} for k in range(6)]
    else:
        time_in_class = []
    wear = (1 - g["non_wear_pct"]).median() * 100 if "non_wear_pct" in g else None
    summary = [
        {"label": "Night rest", "value": _fmt(full["night_rest_h"].where(is_valid).median()), "unit": "h", "help": METRICS["night_rest_h"]["help"]},
        {"label": "Rest regularity", "value": _fmt(full["sri"].where(is_valid).median(), 0), "unit": "SRI", "help": METRICS["sri"]["help"]},
        {"label": "Rest midpoint", "value": _clock(18 + full["rest_mid_6pm"].where(is_valid).median()), "unit": "", "help": METRICS["rest_mid_6pm"]["help"]},
        {"label": "Ring wear coverage", "value": int(round(100 * is_valid.mean())), "unit": "% valid days",
         "help": f"Days with ≥80% wear; median wear {wear:.0f}% of each day" if wear is not None else "Days with ≥80% wear"},
    ]
    return {
        "id": pid,
        "synthetic": source == "synthetic",
        "mrn": synthetic_mrn(pid) if source == "synthetic" else None,
        "sex": _baseline_value(first.get("sex")),
        "age": _baseline_value(first.get("age_yrs")),
        "ctp": _baseline_value(first.get("ctp_class")),
        "baseline": [{"label": label, "value": _baseline_value(first.get(col))} for col, label in BASELINE_FIELDS if col in g],
        "days": days,
        "valid": [bool(x) for x in is_valid],
        "valid_pct": int(round(100 * is_valid.mean())),
        "series": series,
        "tiles": tiles,
        "summary": summary,
        "profile": profile,
        "time_in_class": time_in_class,
        "actogram": [s if isinstance(s, str) else "" for s in full["class_5_min"]],
        "metrics": {k: METRICS[k] for k in series},
    }


def intraday(pid: str, start: int, n_days: int) -> dict | None:
    """5-minute MET and activity class for a window of consecutive study days."""
    df, _ = load()
    g = df[df["name"] == pid].set_index("days_since_start")
    if g.empty:
        return None
    last = int(g.index.max())
    n_days = max(1, min(int(n_days), 14))
    start = max(1, min(int(start), last - n_days + 1))
    days = list(range(start, min(last, start + n_days - 1) + 1))
    w = g.reindex(days)
    met = _met_rows(w["met_5min"]) if "met_5min" in w else np.full((len(days), SLOTS), np.nan)
    return {
        "last_day": last,
        "days": [{"day": d, "valid": bool(w.at[d, "valid80"] == 1) if pd.notna(w.at[d, "valid80"]) else False,
                  "met": [None if np.isnan(x) else round(float(x), 2) for x in met[i]],
                  "cls": w.at[d, "class_5_min"] if isinstance(w.at[d, "class_5_min"], str) else ""}
                 for i, d in enumerate(days)],
    }


def synthetic_mrn(pid: str) -> str:
    """Deterministic made-up MRN for a synthetic patient (DEMO-07 → JH-DEMO07); scripts/seed_medplum.py uses it too.
    Only used when the data source is synthetic, so a study participant never gets an MRN this way."""
    return "JH-" + "".join(ch for ch in pid if ch.isalnum()).upper()


def resolve_mrn(mrn: str) -> str | None:
    """MRN from an EHR launch → local patient ID. Synthetic patients have generated MRNs;
    the de-identified study file has none, so real patients need a local mapping file."""
    df, source = load()
    ids = set(df["name"].unique())
    if source == "synthetic":
        return next((pid for pid in ids if synthetic_mrn(pid) == mrn), None)
    if PUBLIC_DEMO:
        return None
    map_file = os.environ.get("HE_MRN_MAP")  # CSV with columns mrn,study_id; must live outside the repo
    if map_file and Path(map_file).exists():
        m = pd.read_csv(map_file, dtype=str)
        hit = m.loc[m["mrn"] == mrn, "study_id"]
        return hit.iloc[0] if len(hit) and hit.iloc[0] in ids else None
    return None


SELECTION = "_selection"
SELECTION_LEVELS = ("This selection", "Rest of cohort")


# Patients-tab filters. Each travels as URL params (ranges as <key>_min / <key>_max), so a filtered
# cohort can be bookmarked, sent to the Cohort tab and stored in exported notebooks without patient IDs.
FILTERS = {
    "q": {"label": "Patient ID", "kind": "text"},
    "he": {"label": "HE status", "kind": "choice", "fmt": "HE: {}"},
    "ctp": {"label": "Child-Pugh", "kind": "choice", "fmt": "{}"},
    "decomp": {"label": "Decompensated", "kind": "choice", "fmt": "Decompensated: {}"},
    "ascites": {"label": "Ascites", "kind": "choice", "fmt": "Ascites: {}"},
    "he_meds": {"label": "HE meds", "kind": "choice", "fmt": "HE meds: {}"},
    "etiology": {"label": "Etiology", "kind": "choice", "fmt": "{}"},
    "sex": {"label": "Sex", "kind": "choice", "fmt": "{}"},
    "age": {"label": "Age", "kind": "range"},
    "meld": {"label": "MELD", "kind": "range"},
    "valid_pct": {"label": "Valid days ≥ (%)", "kind": "min", "fmt": "valid days ≥ {}%"},
    "attention": {"label": "Needs attention", "kind": "flag", "fmt": "needs attention"},
}


def filter_options() -> dict[str, list[str]]:
    rows = patient_list()
    order = {"he": ["Overt", "Covert", "No"]}
    out = {}
    for key, spec in FILTERS.items():
        if spec["kind"] == "choice":
            values = {str(r[key]) for r in rows if r.get(key) is not None}
            out[key] = [v for v in order.get(key, []) if v in values] + sorted(values - set(order.get(key, [])))
    return out


def parse_filters(args) -> dict[str, str]:
    """Keep only recognised, non-empty, well-formed filter params (args: any mapping, e.g. request.args)."""
    out = {}
    for key, spec in FILTERS.items():
        names = [f"{key}_min", f"{key}_max"] if spec["kind"] == "range" else [key]
        for name in names:
            v = str(args.get(name, "") or "").strip()
            if not v:
                continue
            if spec["kind"] in ("range", "min"):
                try:
                    float(v)
                except ValueError:
                    continue
            if spec["kind"] == "flag" and v not in ("1", "true", "on"):
                continue
            out[name] = "1" if spec["kind"] == "flag" else v
    return out


def _matches(row: dict, params: dict[str, str]) -> bool:
    for key, spec in FILTERS.items():
        kind, val = spec["kind"], row.get(key)
        if kind == "text" and key in params and params[key].lower() not in str(row["id"]).lower():
            return False
        if kind == "choice" and key in params and str(val) != params[key]:
            return False
        if kind == "flag" and key in params and not val:
            return False
        if kind == "min" and key in params and (val is None or val < float(params[key])):
            return False
        if kind == "range":
            lo, hi = params.get(f"{key}_min"), params.get(f"{key}_max")
            if (lo or hi) and val is None:
                return False
            if (lo and val < float(lo)) or (hi and val > float(hi)):
                return False
    return True


def select_ids(params: dict | None = None, **kwargs) -> list[str]:
    """Patient IDs matching the Patients-tab filters."""
    params = parse_filters({**(params or {}), **kwargs})
    return [r["id"] for r in patient_list() if _matches(r, params)]


def describe_filters(params: dict[str, str]) -> str:
    parts = []
    for key, spec in FILTERS.items():
        if spec["kind"] == "range":
            lo, hi = params.get(f"{key}_min"), params.get(f"{key}_max")
            if lo and hi:
                parts.append(f"{spec['label']} {lo}–{hi}")
            elif lo or hi:
                parts.append(f"{spec['label']} {'≥ ' + lo if lo else '≤ ' + hi}")
        elif key in params:
            parts.append(f"ID contains “{params[key]}”" if spec["kind"] == "text" else spec["fmt"].format(params[key]))
    return " · ".join(parts) or "All patients"


def cohort_summary(ids: list[str] | None = None) -> dict:
    full, source = load()
    df = full if ids is None else full[full["name"].isin(ids)]
    people = df.groupby("name").first()
    valid = df[df["valid80"] == 1]
    per_person = valid.groupby("name")[["night_rest_h", "sri"]].median()
    groups = {k: v["label"] for k, v in GROUPS.items() if f"grp_{k}" in df}
    if ids is not None:
        groups = {SELECTION: "This selection vs rest of cohort", **groups}
    return {
        "source": source,
        "participants": int(df["name"].nunique()),
        "of_total": int(full["name"].nunique()),
        "days": int(len(df)),
        "valid_pct": int(round(100 * (df["valid80"] == 1).mean())) if len(df) else 0,
        "median_days": int(df.groupby("name").size().median()) if len(df) else 0,
        "sex": {k: int(v) for k, v in people["sex"].value_counts().items()},
        "night_rest": _fmt(per_person["night_rest_h"].median()),
        "sri": _fmt(per_person["sri"].median(), 0),
        "groups": groups,
        "metrics": {k: {"label": v["label"], "unit": v["unit"]} for k, v in METRICS.items()},
    }


def cohort_compare(group: str, metric: str, ids: list[str] | None = None) -> dict:
    df, _ = load()
    if metric not in METRICS or (group not in GROUPS and not (group == SELECTION and ids is not None)):
        raise KeyError("unknown group or metric")
    valid = df[df["valid80"] == 1].copy()
    if group == SELECTION:
        valid["grp"] = np.where(valid["name"].isin(ids), *SELECTION_LEVELS)
        levels, label = list(SELECTION_LEVELS), "This selection vs rest of cohort"
    else:
        if ids is not None:
            valid = valid[valid["name"].isin(ids)]
        valid["grp"] = valid[f"grp_{group}"]
        levels, label = list(dict.fromkeys(GROUPS[group]["levels"].values())), GROUPS[group]["label"]
    valid = valid[valid["grp"].notna()]
    valid["week"] = (valid["days_since_start"] - 1) // 7 + 1
    weekly = valid.groupby(["name", "week", "grp"])[metric].mean().reset_index()
    per_person = valid.groupby(["name", "grp"])[metric].median().reset_index()
    out = {"group": label, "metric": METRICS[metric], "levels": []}
    samples = []
    for level in levels:
        w = weekly[weekly["grp"] == level].groupby("week")[metric]
        n = w.count()
        q = w.quantile([0.25, 0.5, 0.75]).unstack().reindex(n.index) if len(n) else pd.DataFrame(columns=[0.25, 0.5, 0.75])
        q = q[n >= 5] if len(n) else q
        pp = per_person.loc[per_person["grp"] == level, metric].dropna()
        samples.append(pp)
        out["levels"].append({
            "name": level, "n": int(len(pp)),
            "weeks": [int(x) for x in q.index],
            "median": [_fmt(x, 2) for x in q[0.5]], "q1": [_fmt(x, 2) for x in q[0.25]], "q3": [_fmt(x, 2) for x in q[0.75]],
            "people": [_fmt(x, 2) for x in pp], "overall_median": _fmt(pp.median(), 2),
        })
    if len(samples) == 2 and min(len(s) for s in samples) >= 3:
        res = stats.mannwhitneyu(samples[0], samples[1])
        out["p_value"] = round(float(res.pvalue), 4)
        out["effect_r"] = round(1 - 2 * float(res.statistic) / (len(samples[0]) * len(samples[1])), 2)
    return out
