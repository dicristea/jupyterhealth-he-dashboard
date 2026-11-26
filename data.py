"""Data layer: load the HE study file locally, derive daily measures, build page payloads.

The real de-identified file never lives in this repo. By default it is the single .rds file in
../de-identified data/ (override with HE_DATA_FILE); when it is missing the app falls back to a
small synthetic cohort so the UI still runs anywhere. The hosted study site (HE_HOSTED_STUDY=1)
instead downloads the converted cache from HE_DATA_URL at startup.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
import urllib.parse
import urllib.request
import zlib
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
# Hosted de-identified study site (docs/hosting.md): study data downloaded from HE_DATA_URL, password required.
HOSTED_STUDY = os.environ.get("HE_HOSTED_STUDY", "").strip().lower() in {"1", "true", "yes"}
if PUBLIC_DEMO and HOSTED_STUDY:
    raise RuntimeError("HE_PUBLIC_DEMO and HE_HOSTED_STUDY can't both be on: one serves synthetic data only, "
                       "the other serves study data")


def hosted() -> bool:
    """On a host rather than this computer: local-only features (JupyterLab, writing files, real EHR launches,
    the MRN map) are off. A function, not a constant, so tests can switch either mode on."""
    return PUBLIC_DEMO or HOSTED_STUDY


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
    "average_hrv": {"label": "HRV", "unit": "ms", "digits": 0, "higher_is": "better", "mask": "main_sleep",
                    "help": "Average heart rate variability during the main night (Oura)"},
    "average_heart_rate": {"label": "Average HR", "unit": "bpm", "digits": 0, "higher_is": "worse",
                           "mask": "main_sleep",
                           "help": "Average heart rate during the main night (Oura); 0 bpm readings removed"},
    "steps": {"label": "Steps", "unit": "/day", "digits": 0, "higher_is": "better", "help": "Daily steps"},
    "spo2": {"label": "SpO₂", "unit": "%", "digits": 1, "higher_is": "better", "mask": "main_sleep",
             "help": "Blood oxygen during the main night; not available every night"},
}
# "mask": "main_sleep" = an Oura nightly value, summarised over main nights (see sleep_nights()); every other
# METRICS key is summarised over valid days (valid80 == 1).

# Cohort "Compare by": column → label and level names (in order; the first level is the reference). All baseline
# (recorded at enrollment). meld_band, ascites_now and etiology_group are derived in harmonise_columns().
GROUPS = {
    "anyOHE": {"label": "Overt HE (ever), at enrollment", "levels": {0: "No overt HE", 1: "Overt HE"}},
    "che_phes": {"label": "Covert HE (PHES) at enrollment", "levels": {"No": "No covert HE", "Yes": "Covert HE"}},
    "he_meds": {"label": "HE medication at enrollment", "levels": {"No": "No HE meds", "Yes": "On HE meds"}},
    "ctp_cat": {"label": "Child-Pugh A vs B/C at enrollment", "levels": {"Child A": "Child A", "Child B/C": "Child B/C"}},
    "ctp_class": {"label": "Child-Pugh A / B / C at enrollment",
                  "levels": {"Child A": "Child A", "Child B": "Child B", "Child C": "Child C"}},
    "meld_band": {"label": "MELD <10 / 10–14 / ≥15 at enrollment",
                  "levels": {"<10": "MELD <10", "10–14": "MELD 10–14", "≥15": "MELD ≥15"}},
    "ascites_now": {"label": "Ascites now, at enrollment", "levels": {"Absent": "No ascites now", "Present": "Ascites now"}},
    "ascites": {"label": "Ascites (now or before enrollment)", "levels": {0: "No ascites", 1: "Ascites"}},
    "decomp": {"label": "Decompensation at enrollment", "levels": {0: "Compensated", 1: "Decompensated"},
               "hint": "Overlaps heavily with Child-Pugh B/C."},
    "bb_use_t0": {"label": "Beta-blocker at enrollment", "levels": {"No": "No beta-blocker", "Yes": "On beta-blocker"},
                  "hint": "Beta-blockers lower heart rate: check this before reading any HR or HRV difference."},
    "sex": {"label": "Sex", "levels": {"Female": "Female", "Male": "Male"}},
    "etiology_group": {"label": "Etiology at enrollment",
                       "levels": {"Alcohol": "Alcohol", "MASLD": "MASLD", "Other": "Other etiology"}},
}
GROUP_MENU = {
    "Encephalopathy": ["anyOHE", "che_phes", "he_meds"],
    "Liver severity": ["ctp_cat", "ctp_class", "meld_band", "ascites_now", "ascites", "decomp"],
    "Medications": ["bb_use_t0"],
    "Demographics": ["sex", "etiology_group"],
}

# Oura sleep measures for the patient profile, one value per night (row d = the night that ended on the morning of
# study day d). Kept out of METRICS, which defines the Cohort tab's measures. "words" are the status pill for
# below / above the usual range; "change" is the flag text's direction; both_ways: the non-worse side is not "better".
SLEEP_METRICS = {
    "sleep_mid_h18": {"label": "Sleep midpoint", "unit": "clock", "digits": 2, "higher_is": "worse",
                      "words": ("Earlier than usual", "Later than usual"), "change": ("earlier", "later"),
                      "help": "Middle of the night's main sleep, halfway between bedtime and waking (clock time)"},
    "sleep_tst_h": {"label": "Total sleep", "unit": "h", "digits": 1, "higher_is": "better", "both_ways": True,
                    "words": ("Shorter than usual", "Longer than usual"), "change": ("shorter", "longer"),
                    "help": "Hours asleep in the main sleep (Oura total sleep). Very long sleep is not better: "
                            "longer than usual is shown as a change, not an improvement"},
    "sleep_eff_pct": {"label": "Sleep efficiency", "unit": "%", "digits": 0, "higher_is": "better",
                      "words": ("Lower than usual", "Higher than usual"), "change": ("lower", "higher"),
                      "help": "Share of time in bed actually asleep (Oura total sleep ÷ time from bedtime to waking)"},
    "sleep_waso_min": {"label": "Awake after sleep onset", "unit": "min", "digits": 0, "higher_is": "worse",
                       "words": ("Less than usual", "More than usual"), "change": ("less", "more"),
                       "help": "Minutes awake between first falling asleep and final waking, from the 5-minute "
                               "sleep-stage record"},
    "sleep_bed_sd_min": {"label": "Bedtime variability", "unit": "min", "digits": 0, "higher_is": "worse",
                         "words": ("Steadier than usual", "More variable than usual"), "change": ("less", "more"),
                         "help": "How much bedtime moves night to night: standard deviation of bedtime over the main "
                                 "nights in the 14 study days up to each night (needs 7 nights)"},
}
# Status pill words for the activity and secondary tiles (METRICS keys).
TILE_WORDS = {"steps": ("Fewer than usual", "More than usual"),
              "sri": ("Less regular than usual", "More regular than usual")}
PATIENT_METRICS = {**METRICS, **SLEEP_METRICS}
SLEEP_TILES = list(SLEEP_METRICS)
# Tiles on the patient profile, in order; MORE_TILES sit in the collapsed "More measures" section.
PATIENT_TILES = [*SLEEP_TILES, "steps"]
MORE_TILES = ["average_hrv", "average_heart_rate", "spo2", "sri"]
# Recent value = median of the nights (or valid days) in the last `window` study days (WINDOWS; the profile's
# ?window=), at least MIN_RECENT of them; the usual range comes from the values before that window, at least
# MIN_BASE of them. Otherwise: "not enough nights". Bedtime variability is the SD over the window's main nights,
# needing BED_SD_MIN[window] of them. The Patients list always uses RECENT_DAYS.
RECENT_DAYS, MIN_RECENT, MIN_BASE = 14, 4, 14
WINDOWS = (7, 14)
BED_SD_MIN = {7: 4, 14: 7}
MAIN_SLEEP_TYPE = "long_sleep"

# Cohort tab measures, grouped for the Measure menu (first = default). Sleep and nightly vitals use main nights,
# activity measures valid days. A person counts with at least MIN_PERSON values of the measure; a person-week with
# at least MIN_WEEK; a study week is drawn with at least MIN_PEOPLE_WEEK people; a level is tested with MIN_TEST.
COHORT_MENU = {
    "Sleep (Oura)": list(SLEEP_METRICS),
    "Activity": ["steps", "sri", "night_rest_h", "rest_mid_6pm"],
    "Physiology": ["average_heart_rate", "average_hrv", "spo2"],
}
COHORT_METRICS = {k: PATIENT_METRICS[k] for keys in COHORT_MENU.values() for k in keys}
DEFAULT_COHORT_METRIC = "sleep_mid_h18"
MIN_PERSON, MIN_WEEK, MIN_PEOPLE_WEEK, MIN_TEST = 14, 3, 5, 5


def nightly(key: str) -> bool:
    """True for measures summarised over main nights (sleep and Oura nightly vitals), False for valid days."""
    return key in SLEEP_METRICS or PATIENT_METRICS.get(key, {}).get("mask") == "main_sleep"

# Clinical snapshot on the patient profile, all at enrollment: group → (column, label). "ctp" is Child-Pugh score and
# class together. A column the data lacks is left out; a present column with no value for this patient goes into one
# "Not recorded at enrollment" line, and a group with nothing to show is hidden. The "Study" group is shown as small
# text. meld_cat, lfi_cat and fli_cat are numeric codes with no labels in the study file, so they are left out.
CLINICAL_SNAPSHOT = {
    "Liver severity": [("meld", "MELD"), ("ctp", "Child-Pugh"), ("total_bilirubin_t0", "Bilirubin (mg/dL)"),
                       ("albumin_t0", "Albumin (g/dL)"), ("inr_t0", "INR")],
    "Decompensation": [("decomp", "Decompensated"), ("current_ascites", "Ascites now")],
    "Encephalopathy": [("che_phes", "Covert HE (PHES)"), ("phes_score", "PHES score")],
    "Frailty": [("liver_frailty_index", "Liver Frailty Index")],
    "Study": [("whichsite", "Site"), ("whicharm", "Arm"), ("cirrhosis_etiology", "Etiology")],
}
# The Medications and Clinical history dropdowns: fields recorded at enrollment (both data sources; same rules as
# above). The study file has no medication list and no dated events, so only synthetic patients also get a few
# illustrative dated rows (synthetic_clinical_extras).
SNAPSHOT_MEDICATIONS = [("bb_use_t0", "Beta-blocker"), ("medication_for_hepatic_enc", "HE medication"),
                        ("medication_for_ascites", "Ascites medication")]
SNAPSHOT_HISTORY = [("previous_encephalopathy", "Overt HE"), ("previous_ascites", "Ascites"),
                    ("variceal_bleeding_history", "Variceal bleed")]
SNAPSHOT_SECTIONS = {"status": "At enrollment", "medications": "Medications", "history": "Clinical history"}
EXTRAS_SEED = 7  # the synthetic generator's default seed; with the patient id it fixes each patient's rows


def _yes(v) -> bool:
    return _baseline_value(v) == "Yes"


def synthetic_clinical_extras(pid: str, first: pd.Series, last_day: int) -> dict:
    """Illustrative medications (0–4) and clinical events (0–4) for one synthetic patient, consistent with
    their baseline fields. Seeded by patient id, so the same patient always gets the same rows. Never for study data."""
    rng = np.random.default_rng([EXTRAS_SEED, zlib.crc32(pid.encode())])
    decomp, ascites, he_meds, ohe = (_yes(first.get(c)) for c in ("decomp", "ascites", "he_meds", "anyOHE"))
    varices = _yes(first.get("variceal_bleeding_history"))

    def since() -> str:
        return "before enrollment" if rng.random() < 0.7 else f"since study day {int(rng.integers(1, max(2, last_day)))}"

    meds = []
    if he_meds:
        meds.append({"name": "Lactulose", "dose": "20 g", "freq": "2–3 times daily, to 2–3 soft stools", "since": since()})
        if ohe or rng.random() < 0.5:
            meds.append({"name": "Rifaximin", "dose": "550 mg", "freq": "twice daily", "since": since()})
    if ascites:
        meds.append({"name": "Spironolactone", "dose": "100 mg", "freq": "once daily", "since": since()})
        if rng.random() < 0.7:
            meds.append({"name": "Furosemide", "dose": "40 mg", "freq": "once daily", "since": since()})
    # A beta-blocker row exactly when the enrollment field says so (or, without that field, after a variceal bleed)
    on_bb = _yes(first.get("bb_use_t0")) if "bb_use_t0" in first.index else varices or (decomp and rng.random() < 0.4)
    if on_bb:
        meds.append({"name": "Nadolol", "dose": "20 mg", "freq": "once daily", "since": since()} if varices else
                    {"name": "Carvedilol", "dose": "6.25 mg", "freq": "twice daily", "since": since()})
    meds = meds[:4]

    def day() -> int:
        return int(rng.integers(1, max(2, last_day + 1)))

    events = []
    if rng.random() < 0.75:
        d = day()
        scan = "Liver MRI" if not decomp and rng.random() < 0.3 else "Liver ultrasound"
        events.append({"day": d, "kind": "Imaging", "title": f"{scan}: HCC surveillance", "detail": "No focal lesion"})
        if rng.random() < 0.8:
            events.append({"day": min(last_day, d + int(rng.integers(0, 8))), "kind": "Labs",
                           "title": "AFP checked: HCC surveillance", "detail": f"{rng.uniform(2.0, 8.0):.1f} ng/mL"})
    if decomp and rng.random() < 0.5:
        reason = ("Overt HE episode" if ohe else "Ascites: large-volume paracentesis" if ascites
                  else "Upper GI bleed: endoscopy")
        events.append({"day": day(), "kind": "Hospitalization", "title": reason,
                       "detail": f"{int(rng.integers(2, 8))}-day stay"})
    if decomp and rng.random() < 0.4:
        events.append({"day": day(), "kind": "Labs", "title": "Liver panel and INR (MELD labs)", "detail": "Outpatient draw"})
    events = sorted(events, key=lambda e: e["day"])[:4]
    return {"medications": meds, "history": events}


def _snapshot_fields(first: pd.Series, columns: set, spec) -> tuple[list[dict], list[str]]:
    """(shown fields, labels of present-but-empty fields) for one list of (column, label)."""
    shown, missing = [], []
    for col, label in spec:
        if col == "ctp":
            if not {"ctp_score", "ctp_class"} & columns:
                continue
            value = _ctp_text(first)
        elif col in columns:
            value = _baseline_value(first.get(col))
        else:
            continue
        if value is None:
            missing.append(label)
        else:
            shown.append({"label": label, "value": value})
    return shown, missing


def snapshot_extras(pid: str, first: pd.Series, last_day: int, source: str, columns) -> dict:
    """Medications and Clinical history for the panel: the enrollment fields (any data source) plus, on synthetic
    data only, illustrative dated rows. A dropdown with nothing to show is hidden (show_*)."""
    columns = set(columns)
    meds, meds_missing = _snapshot_fields(first, columns, SNAPSHOT_MEDICATIONS)
    hist, hist_missing = _snapshot_fields(first, columns, SNAPSHOT_HISTORY)
    rows = synthetic_clinical_extras(pid, first, last_day) if source == "synthetic" else {"medications": [], "history": []}
    return {"synthetic": source == "synthetic",
            "med_fields": meds, "med_missing": meds_missing, "medications": rows["medications"],
            "history_fields": hist, "history_missing": hist_missing, "history": rows["history"],
            "show_medications": bool(meds or meds_missing or rows["medications"]),
            "show_history": bool(hist or hist_missing or rows["history"])}


def _ctp_text(first: pd.Series) -> str | None:
    """Child-Pugh as "7, class B" from ctp_score and ctp_class (either may be missing)."""
    score, cls = _baseline_value(first.get("ctp_score")), _baseline_value(first.get("ctp_class"))
    letter = str(cls).removeprefix("Child").strip() if cls is not None else None
    if letter is None and isinstance(score, (int, float)):
        letter = "A" if score <= 6 else "B" if score <= 9 else "C"
    if score is None:
        return f"class {letter}" if letter else None
    return f"{score}, class {letter}" if letter else str(score)


def clinical_snapshot(first: pd.Series, columns) -> dict:
    """The "At enrollment" panel for one patient's first row; `columns` are the columns the loaded data has.
    Returns {"groups": [{group, fields}], "study": [fields], "not_recorded": [labels]}."""
    columns = set(columns)
    groups, study, not_recorded = [], [], []
    for name, spec in CLINICAL_SNAPSHOT.items():
        shown, missing = _snapshot_fields(first, columns, spec)
        not_recorded += missing
        if name == "Study":
            study = shown
        elif shown:
            groups.append({"group": name, "fields": shown})
    return {"groups": groups, "study": study, "not_recorded": not_recorded}


def clinical_headline(first: pd.Series) -> str:
    """One line under the patient name, all from enrollment: "70 y · male · Child-Pugh B (7) · MELD 14 · prior HE"."""
    v = {c: _baseline_value(first.get(c)) for c in ("age_yrs", "sex", "ctp_score", "ctp_class", "meld",
                                                    "previous_encephalopathy", "anyOHE", "medication_for_hepatic_enc",
                                                    "he_meds")}
    parts = []
    if v["age_yrs"] is not None:
        parts.append(f"{v['age_yrs']} y")
    if v["sex"] is not None:
        parts.append(str(v["sex"]).lower())
    if v["ctp_class"] is not None or v["ctp_score"] is not None:
        cls = str(v["ctp_class"]).removeprefix("Child").strip() if v["ctp_class"] is not None else ""
        score = f" ({v['ctp_score']})" if v["ctp_score"] is not None else ""
        parts.append(f"Child-Pugh {cls}{score}".replace("  ", " ").strip())
    if v["meld"] is not None:
        parts.append(f"MELD {v['meld']}")
    prior = v["previous_encephalopathy"] if v["previous_encephalopathy"] is not None else v["anyOHE"]
    if prior == "Yes":
        parts.append("prior HE")
    meds = v["medication_for_hepatic_enc"] if v["medication_for_hepatic_enc"] is not None else v["he_meds"]
    if meds == "Yes":
        parts.append("on HE medication")
    return " · ".join(parts)


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
# Calendar dates must never leave this script (HIPAA Safe Harbor). A night is filed on its wake day, so bedtimes
# become wall-clock hours after 18:00 local on the evening before the wake date: continuous across midnight,
# start < end. time_in_bed_s is real elapsed time (differs from the clock span by 1 h on DST nights).
if (all(c("bedtime_start", "bedtime_end") %in% names(x))) {
  tz <- attr(x$bedtime_end, "tzone")
  if (is.null(tz) || !nzchar(tz[1])) tz <- "America/New_York"
  tz <- tz[1]
  anchor <- as.numeric(as.Date(x$bedtime_end, tz = tz)) - 1
  h18 <- function(t) {
    lt <- as.POSIXlt(t, tz = tz)
    round(((as.numeric(as.Date(t, tz = tz)) - anchor) * 24 + lt$hour + lt$min / 60 + lt$sec / 3600 - 18) * 60) / 60
  }
  x$bedtime_start_h18 <- h18(x$bedtime_start)
  x$bedtime_end_h18 <- h18(x$bedtime_end)
  x$time_in_bed_s <- round(as.numeric(difftime(x$bedtime_end, x$bedtime_start, units = "secs")))
  x$bedtime_start <- NULL
  x$bedtime_end <- NULL
  rm(anchor)
}
x$movement_30_sec <- NULL  # numeric garbage in the export (mostly Inf / ~1e296)
x <- x[, !vapply(x, is.list, logical(1)), drop = FALSE]
x[] <- lapply(x, function(v) if (inherits(v, "haven_labelled")) haven::as_factor(v) else v)
dated <- names(x)[vapply(x, function(v) inherits(v, c("POSIXt", "Date")), logical(1))]
if (length(dated)) stop("date columns would leave the R step: ", paste(dated, collapse = ", "))
write.csv(x, args[2], row.names = FALSE, na = "")
"""

# Bump when R_TO_CSV's output changes, so an older cache is rebuilt rather than reused.
# v2 added met_5min; v3 replaces bedtime date-times with bedtime_*_h18 + time_in_bed_s and refuses any date column.
CACHE_VERSION = "v3"
# Columns pandas must keep as text: digit strings would otherwise be parsed as huge numbers.
TEXT_COLUMNS = {"class_5_min": str, "name": str, "met_5min": str, "sleep_phase_5_min": str, "type": str}


def _safe_url(url: str) -> str:
    """URL without credentials, query string or fragment, for logs and error messages."""
    u = urllib.parse.urlsplit(url)
    return f"{u.scheme}://{u.hostname or ''}{u.path}"


def fetch_hosted_data(url: str, token: str = "", dest_dir: Path | None = None) -> Path:
    """Download the study cache (gzipped .v3.csv) once into a temp folder and return its path.

    Made for a GitHub release asset API URL. The token goes in an *unredirected* header: it reaches
    api.github.com only, not the storage host GitHub redirects to (which rejects a second credential).
    """
    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream",
                                               "User-Agent": "he-dashboard-hosted"})
    if token:
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    dest = Path(dest_dir or tempfile.mkdtemp(prefix="he-data-")) / f"study.{CACHE_VERSION}.csv.gz"
    log = logging.getLogger("he.data")
    try:
        with urllib.request.urlopen(req, timeout=120) as res, dest.open("wb") as out:
            shutil.copyfileobj(res, out, length=1 << 20)
    except Exception as exc:  # the message names neither the token nor a signed redirect URL
        reason = getattr(exc, "code", None) or type(exc).__name__
        log.error("Study data download failed from %s (%s)", _safe_url(url), reason)
        raise RuntimeError(f"Could not download study data from {_safe_url(url)} ({reason})") from None
    log.warning("Study data downloaded from %s (%d bytes)", _safe_url(url), dest.stat().st_size)
    return dest


HOSTED_DATA_FILE: Path | None = None  # the downloaded study cache, set by load() in HE_HOSTED_STUDY mode

# The hosted study site's own JupyterLite (docs/jupyterlite.md → hosted study site) ships this marker next to
# data.py and no data at all; there, load() and lite_study_table() fetch the study cache from the site.
LITE_MARKER = HERE / "hosted_study.json"


def in_hosted_lite() -> bool:
    """Running in the browser (Pyodide) inside the hosted study site's JupyterLite."""
    return sys.platform == "emscripten" and LITE_MARKER.exists()


def _browser_get(url: str) -> bytes:
    """GET a same-origin URL from the Pyodide kernel, with the page's login cookie. The kernel runs in a web
    worker, where synchronous requests are allowed; the text fallback covers browsers that refuse binary ones."""
    from js import XMLHttpRequest  # only exists under Pyodide
    req = XMLHttpRequest.new()
    req.open("GET", url, False)
    try:
        req.responseType = "arraybuffer"
        binary = True
    except Exception:
        req.overrideMimeType("text/plain; charset=x-user-defined")
        binary = False
    req.send(None)
    if req.status != 200:
        raise RuntimeError(f"The study site answered {req.status} for {url}: log in to the dashboard in this "
                           "browser, then run the cell again.")
    if binary:
        return bytes(req.response.to_py())
    return bytes(ord(ch) & 0xFF for ch in req.responseText)


@lru_cache(maxsize=1)
def lite_study_table() -> tuple[pd.DataFrame, str]:
    """Raw study participant-days for notebooks in the hosted site's JupyterLite, and where they came from."""
    if not in_hosted_lite():
        raise RuntimeError("lite_study_table() only works in the hosted study site's JupyterLite")
    url = json.loads(LITE_MARKER.read_text()).get("data_url", "/api/study-data.csv.gz")
    path = Path(tempfile.mkdtemp(prefix="he-data-")) / f"study.{CACHE_VERSION}.csv.gz"  # the kernel's memory filesystem
    path.write_bytes(_browser_get(url))
    return _read_cache_csv(path), "the study site (de-identified study data)"


def _read_cache_csv(path: Path) -> pd.DataFrame:
    """The R-converted study cache (.v3.csv), plain or gzipped (told apart by magic bytes, not the name,
    so a downloaded cache loads whatever it is called)."""
    with path.open("rb") as fh:
        gz = fh.read(2) == b"\x1f\x8b"
    return pd.read_csv(path, low_memory=False, compression="gzip" if gz else None, dtype=TEXT_COLUMNS)


def _read_rds(path: Path) -> pd.DataFrame:
    # pyreadr can't parse haven-labelled or list columns, so R converts the file once.
    cache = CACHE_DIR / f"{path.stem}.{CACHE_VERSION}.csv"
    if not cache.exists() or cache.stat().st_mtime < path.stat().st_mtime:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        script = CACHE_DIR / "rds_to_csv.R"
        script.write_text(R_TO_CSV)
        partial = cache.with_suffix(".partial")  # a failed run must not leave a cache that looks complete
        subprocess.run(["Rscript", str(script), str(path), str(partial)], check=True)
        partial.replace(cache)
    return _read_cache_csv(cache)


# Every assumption behind the synthetic cohort, in one place. These are hand-picked, round,
# clinically plausible values, not estimates from the study data. docs/synthetic-data.md explains
# each one. Override any of them with synthetic_cohort(config=...) or the generator's --config JSON.
# Ranges [lo, hi] are inclusive integers, except cldq_total and the sleep "hours" ranges (uniform floats).
# A list of categories is drawn uniformly; a {category: probability} dict is drawn with those weights.
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
        "current_ascites_if_ascites": {"Slight": 0.55, "Moderate": 0.25, "Absent": 0.2},  # Absent = resolved
        "whichsite": {"WCM": 0.65, "CUMC": 0.35},
        "whicharm": {"Pragmatic": 0.6, "Frailty": 0.4},
    },
    "compensated": {
        "anyhe": 0.0, "anyOHE": 0.0, "he_meds": 0.0, "ascites": 0.0, "bb_use": 0.3,  # probabilities
        "ctp_class": "Child A", "ctp_score": [5, 6], "meld": [6, 11],
        "bedtime_h": 23.3, "bedtime_night_sd": 0.6, "rest_h": 6.6,  # clock hours (24.5 = 00:30), hours
        "hrv_ms": 28, "hr_bpm": 66, "steps": 8500,  # patient means are drawn around these
        # Oura sleep record (one per night, filed on the wake day)
        "no_sleep_record_share": 0.06, "non_main_share": 0.05, "time_in_bed_h": 7.8,
        "latency_min": 12, "wake_bouts_per_h": 0.6, "deep_share": 0.15, "rem_share": 0.20,
        "restless_per_h": 22, "breath_rpm": 15.5, "bdi": 3,
    },
    "decompensated": {
        "anyhe": 0.6, "anyOHE": 0.4, "he_meds": 0.5, "ascites": 0.8, "bb_use": 0.55,
        "ctp_class": "Child B", "ctp_score": [7, 12], "meld": [12, 21],  # Child B becomes C at score >= 10
        "bedtime_h": 24.5, "bedtime_night_sd": 1.2, "rest_h": 5.2,
        "hrv_ms": 20, "hr_bpm": 72, "steps": 5500,
        "no_sleep_record_share": 0.10, "non_main_share": 0.15, "time_in_bed_h": 8.3,
        "latency_min": 25, "wake_bouts_per_h": 1.2, "deep_share": 0.09, "rem_share": 0.14,
        "restless_per_h": 32, "breath_rpm": 16.5, "bdi": 6,
    },
    "between_patient_sd": {"bedtime_h": 0.6, "rest_h": 0.4, "hrv_ms": 4, "hr_bpm": 4, "steps": 1500,
                           "time_in_bed_h": 0.4, "stage_share": 0.03, "breath_rpm": 1.0},
    "day_to_day_sd": {"rest_h": 1.0, "hrv_ms": 4, "hr_bpm": 3, "steps": 2000, "time_in_bed_h": 0.8,
                      "breath_rpm": 0.5},
    "floors": {"rest_h": 2.5, "hrv_ms": 5, "steps": 0, "time_in_bed_h": 3.0},
    "activity": {
        "active_share": 0.25,  # share of non-rest 5-minute slots that are low/medium/high activity
        "classes": {"3": 0.7, "4": 0.25, "5": 0.05},  # low / medium / high
        "met_by_class": [0.0, 0.95, 1.2, 2.2, 3.8, 6.5],  # ring off, rest, inactive, low, medium, high
        "met_jitter": [0.85, 1.15],  # per-slot multiplier
    },
    "wear": {"invalid_day_share": 0.15, "gap_slots": 90, "gap_start_slot": [0, 199]},
    "spo2": {"day_share": 0.4, "mean": 97, "sd": 1},
    "sleep": {
        "non_main_types": {"sleep": 0.85, "late_nap": 0.10, "rest": 0.05},
        "non_main_hours": [0.5, 3.0], "non_main_start_hour": [10.0, 20.0],  # clock hour on the wake day
        "cycle_slots": 18,  # 90-minute sleep cycles
        "deep_by_cycle": [0.45, 0.35, 0.15, 0.05],  # deep sleep front-loaded; later cycles get none
        "rem_per_cycle": [2, 6],  # REM grows across the night (relative weights, first → last cycle)
        "wake_bout_slots": [1, 3], "final_wake_slots": [1, 3],
        "bdi_share": 0.4,  # breathing disturbance index present on this share of nights
    },
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


def _ctp_points(bili: float, albumin: float, inr: float, current_ascites: str, prior_he: bool) -> int:
    """Child-Pugh points: 1–3 each for bilirubin, albumin, INR, ascites and encephalopathy (5–15)."""
    return int(5 + (bili >= 2) + (bili > 3) + (albumin <= 3.5) + (albumin < 2.8) + (inr >= 1.7) + (inr > 2.3)
               + {"Absent": 0, "Slight": 1, "Moderate": 2}[current_ascites] + prior_he)


def _synthetic_labs(rng: np.random.Generator, meld: int, ascites: bool) -> tuple[float, float, float]:
    """Baseline bilirubin (mg/dL), albumin (g/dL) and INR that rise/fall with MELD, as in the study's labs."""
    inr = float(np.clip(rng.normal(1.0 + 0.065 * (meld - 6), 0.08), 0.9, 3.0))
    bili = float(np.clip(rng.lognormal(np.log(0.6 + 0.17 * (meld - 6)), 0.25), 0.2, 20))
    albumin = float(np.clip(rng.normal(4.3 - 0.08 * (meld - 6) - 0.3 * ascites, 0.25), 2.0, 5.0))
    return round(bili, 1), round(albumin, 1), round(inr, 1)


_SLEEP_COLUMNS = ["type", "sleep_phase_5_min", "num_sleep_phase_5_min", "time_in_bed_s", "total_sleep_duration",
                  "awake_time", "latency", "rem_sleep_duration", "rem_sleep_dur_pct", "restless_periods",
                  "breathing_disturbance_index", "average_breath", "bedtime_start_h18", "bedtime_end_h18"]


def _hypnogram(rng: np.random.Generator, n: int, latency_slots: int, deep: float, rem: float,
               wake_per_h: float, sc: dict) -> str:
    """n slots of '1' deep, '2' light, '3' REM, '4' awake: sleep latency, ~90-minute cycles with deep sleep early
    and REM growing later, short wake bouts, and a little wake at the end."""
    end = min(_int_between(rng, sc["final_wake_slots"]), max(0, n - latency_slots - 1))
    latency_slots = min(latency_slots, n - end - 1)
    bouts = [_int_between(rng, sc["wake_bout_slots"]) for _ in range(rng.poisson(wake_per_h * n / 12))]
    while bouts and sum(bouts) > (n - latency_slots - end) // 2:
        bouts.pop()
    asleep = n - latency_slots - end - sum(bouts)
    cyc = sc["cycle_slots"]
    lengths = [cyc] * (asleep // cyc) + ([asleep % cyc] if asleep % cyc else [])
    k = len(lengths)
    deep_w = np.array((sc["deep_by_cycle"] + [0.0] * k)[:k], dtype=float)
    deep_w = deep_w / deep_w.sum() if deep_w.sum() else np.full(k, 1 / k)
    rem_w = np.linspace(*sc["rem_per_cycle"], k) if k > 1 else np.ones(1)
    deep_n = rng.multinomial(rng.binomial(asleep, min(1.0, max(0.0, deep))), deep_w)
    rem_n = rng.multinomial(rng.binomial(asleep, min(1.0, max(0.0, rem))), rem_w / rem_w.sum())
    parts = []
    for length, d, r in zip(lengths, deep_n, rem_n):
        d = min(d, length)
        r = min(r, length - d)
        light = length - d - r
        parts.append("2" * (light // 2) + "1" * d + "2" * (light - light // 2) + "3" * r)
    sleep = list("".join(parts))
    for pos, bout in sorted(zip(rng.integers(0, asleep + 1, len(bouts)), bouts), reverse=True):
        sleep[pos:pos] = ["4"] * bout
    return "4" * latency_slots + "".join(sleep) + "4" * end


def _sleep_record(rng: np.random.Generator, s: dict, sc: dict, p: dict, start_h: float, tib_h: float,
                  kind: str) -> dict:
    """One night's Oura sleep columns in the converted-cache format, all derived from the hypnogram so
    total_sleep + awake == time in bed and rem == 300 × count('3') hold exactly. start_h is the clock hour
    on the evening before the wake day (24.5 = 00:30 on the wake day)."""
    n = max(2, int(round(tib_h * 12)))
    latency_slots = int(rng.geometric(min(1.0, 5 / max(s["latency_min"], 5))))  # mean latency_min / 5 slots
    hyp = _hypnogram(rng, n, latency_slots, p["deep"], p["rem"], s["wake_bouts_per_h"], sc)
    tib_s = 300 * n + int(rng.integers(-150, 151))
    asleep = sum(hyp.count(c) for c in "123")
    rem = 300.0 * hyp.count("3")
    total = 300.0 * asleep
    start = round((start_h - 18) * 60) / 60
    return {"type": kind, "sleep_phase_5_min": hyp, "num_sleep_phase_5_min": float(n), "time_in_bed_s": float(tib_s),
            "total_sleep_duration": total, "awake_time": float(tib_s - total),
            "latency": float(300 * (len(hyp) - len(hyp.lstrip("4")))), "rem_sleep_duration": rem,
            "rem_sleep_dur_pct": round(rem / total, 2) if total else np.nan,
            "restless_periods": float(rng.poisson(s["restless_per_h"] * tib_s / 3600)),
            "breathing_disturbance_index": float(min(50, round(rng.gamma(1.2, s["bdi"] / 1.2))))
            if rng.random() < sc["bdi_share"] else np.nan,
            "average_breath": round(float(rng.normal(p["breath"], p["breath_sd"])), 1),
            "bedtime_start_h18": start, "bedtime_end_h18": round((start + tib_s / 3600) * 60) / 60}


def synthetic_cohort(n: int = 24, seed: int = 7, min_days: int = 90, max_days: int = 180,
                     config: dict | None = None) -> pd.DataFrame:
    """A fake cohort in the study cache's format: one row per participant-day, study days from 0.
    See docs/synthetic-data.md."""
    if n < 1 or min_days < 1 or max_days < min_days:
        raise ValueError("participants and days must be positive, with min_days <= max_days")
    cfg = synthetic_config(config)
    b, sd, day_sd, floor = cfg["baseline"], cfg["between_patient_sd"], cfg["day_to_day_sd"], cfg["floors"]
    act, wear, spo2, sc = cfg["activity"], cfg["wear"], cfg["spo2"], cfg["sleep"]
    met_by_class = np.array(act["met_by_class"], dtype=float)
    clock = DAY_STARTS_AT + np.arange(SLOTS) * 5 / 60
    rng = np.random.default_rng(seed)
    # Baseline extras and sleep records draw from their own stream, so adding them left the original columns
    # unchanged for a given seed.
    srng = np.random.default_rng([seed, 1])
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
        ascites = _chance(rng, s["ascites"])  # the study file has no `ascites`; load() derives it from these two
        base["current_ascites"] = _pick(srng, b["current_ascites_if_ascites"]) if ascites else "Absent"
        base["previous_ascites"] = "Yes" if ascites else "No"
        base["ctp_class"] = s["ctp_class"]
        base["ctp_cat"] = "Child A" if s["ctp_class"] == "Child A" else "Child B/C"
        base["meld"] = _int_between(rng, s["meld"])
        base["cirrhosis_etiology"] = _pick(rng, b["cirrhosis_etiology"])
        base["variceal_bleeding_history"] = _pick(rng, b["variceal_bleeding_history"])
        base["phes_score"] = _int_between(rng, b["phes_score"])
        base["psqiglobal"] = _int_between(rng, b["psqiglobal"])
        base["cldq_total"] = round(float(rng.uniform(*b["cldq_total"])), 1)
        base["liver_frailty_index"] = np.nan
        bili, albumin, inr = _synthetic_labs(srng, base["meld"], ascites)
        prior_he = bool(base["anyOHE"])
        base.update({"total_bilirubin_t0": bili, "albumin_t0": albumin, "inr_t0": inr,
                     "ctp_score": int(np.clip(_ctp_points(bili, albumin, inr, base["current_ascites"], prior_he),
                                              *s["ctp_score"])),
                     "previous_encephalopathy": "Yes" if prior_he else "No",
                     "medication_for_hepatic_enc": base["he_meds"],
                     "bb_use_t0": "Yes" if _chance(srng, s["bb_use"]) else "No",
                     "whichsite": _pick(srng, b["whichsite"]), "whicharm": _pick(srng, b["whicharm"])})
        if base["ctp_class"] == "Child B" and base["ctp_score"] >= 10:  # no extra draw, so other columns keep
            base["ctp_class"] = "Child C"
        night = {"tib": srng.normal(s["time_in_bed_h"], sd["time_in_bed_h"]),
                 "deep": srng.normal(s["deep_share"], sd["stage_share"]),
                 "rem": srng.normal(s["rem_share"], sd["stage_share"]),
                 "breath": srng.normal(s["breath_rpm"], sd["breath_rpm"]), "breath_sd": day_sd["breath_rpm"]}
        bed = rng.normal(s["bedtime_h"], sd["bedtime_h"])
        length = rng.normal(s["rest_h"], sd["rest_h"])
        hrv0, hr0, steps0 = rng.normal(s["hrv_ms"], sd["hrv_ms"]), rng.normal(s["hr_bpm"], sd["hr_bpm"]), rng.normal(s["steps"], sd["steps"])
        prev_start = bed + srng.normal(0, s["bedtime_night_sd"])  # the night that ends on the morning of day 0
        for day in range(_int_between(rng, [min_days, max_days])):
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
            row = {**base, "met_5min": ",".join(f"{m:.2f}" for m in met), "days_since_start": day,
                   # Not in the current study file; kept because notebooks/oura_data_exploration.py needs it
                   "day_type": "Weekend" if day % 7 in (6, 0) else "Weekday",
                   "valid80": int(non_wear_pct <= 0.2), "non_wear_pct": non_wear_pct,
                   "average_hrv": max(floor["hrv_ms"], rng.normal(hrv0, day_sd["hrv_ms"])),
                   "average_heart_rate": rng.normal(hr0, day_sd["hr_bpm"]),
                   "steps": max(floor["steps"], rng.normal(steps0, day_sd["steps"])),
                   "spo2": rng.normal(spo2["mean"], spo2["sd"]) if rng.random() < spo2["day_share"] else np.nan,
                   "class_5_min": "".join(slots), **dict.fromkeys(_SLEEP_COLUMNS, np.nan)}
            # This row holds the night that ended this morning (the study files a night on its wake day).
            if srng.random() < s["no_sleep_record_share"]:
                for col in ("average_hrv", "average_heart_rate", "spo2"):  # Oura's nightly values need a night
                    row[col] = np.nan
            elif srng.random() < s["non_main_share"]:
                hours = float(srng.uniform(*sc["non_main_hours"]))
                row.update(_sleep_record(srng, s, sc, night, 24 + float(srng.uniform(*sc["non_main_start_hour"])),
                                         hours, str(_pick(srng, sc["non_main_types"]))))
            else:
                tib = max(floor["time_in_bed_h"], srng.normal(night["tib"], day_sd["time_in_bed_h"]))
                row.update(_sleep_record(srng, s, sc, night, prev_start, tib, "long_sleep"))
            rows.append(row)
            prev_start = start
    return pd.DataFrame(rows)


def synthetic_table() -> tuple[pd.DataFrame, str]:
    """Raw synthetic participant-days for the notebooks, and where they came from.

    SYNTHETIC_FILE if it exists, otherwise the default in-memory cohort (unlike load(), which needs
    the file when HE_DATA_SOURCE=synthetic). The public demo always uses the in-memory cohort.
    """
    if not PUBLIC_DEMO and SYNTHETIC_FILE.exists():
        return (harmonise_columns(pd.read_csv(SYNTHETIC_FILE, low_memory=False, dtype=TEXT_COLUMNS)),
                str(SYNTHETIC_FILE))
    return harmonise_columns(synthetic_cohort()), "data.synthetic_cohort() with the default settings (seed 7)"


MISSING_CODE = -999  # the study file's "not available" code in some baseline labs (e.g. total_bilirubin_t0)


def derive_ascites(df: pd.DataFrame) -> pd.Series:
    """Ascites yes/no from the study file's current_ascites (Absent/Slight/Moderate) and previous_ascites (Yes/No):
    1 if ascites now or before, 0 when both are known and neither says so, otherwise missing."""
    missing = pd.Series(np.nan, index=df.index, dtype=object)
    current, previous = df.get("current_ascites", missing), df.get("previous_ascites", missing)
    positive = current.isin(["Slight", "Moderate"]) | previous.eq("Yes")
    known = current.notna() & previous.notna()
    return pd.Series(np.where(positive, 1.0, np.where(known, 0.0, np.nan)), index=df.index)


def harmonise_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Map any source (study cache, synthetic, a user CSV) onto the columns the app reads: -999 → missing,
    `ascites` derived when the file only has current/previous ascites, and one row per (name, study day).
    Of duplicated days the valid-wear copy wins, then the first in file order."""
    key = ["name", "days_since_start"]
    if df.duplicated(key).any():
        invalid = ~pd.to_numeric(df["valid80"], errors="coerce").eq(1) if "valid80" in df else False
        df = df.assign(_invalid=invalid).sort_values([*key, "_invalid"], kind="stable").drop_duplicates(key)
        df = df.drop(columns="_invalid").sort_index()
    df = df.copy()
    for col in df.select_dtypes("number").columns:
        if df[col].eq(MISSING_CODE).any():
            df[col] = df[col].mask(df[col].eq(MISSING_CODE))
    if "ascites" not in df and {"current_ascites", "previous_ascites"} & set(df.columns):
        df["ascites"] = derive_ascites(df)
    return df.assign(**derive_groups(df))


def derive_groups(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Cohort grouping columns built from baseline fields: MELD band, ascites now (current_ascites), and etiology
    as Alcohol / MASLD (NAFLD in the file) / Other. Missing stays missing."""
    out = {}
    if "meld" in df:
        meld = pd.to_numeric(df["meld"], errors="coerce")
        out["meld_band"] = pd.Series(np.select([meld < 10, meld < 15, meld >= 15], ["<10", "10–14", "≥15"], ""),
                                     index=df.index).replace("", np.nan).where(meld.notna())
    if "current_ascites" in df:
        cur = df["current_ascites"]
        out["ascites_now"] = cur.map(lambda v: "Absent" if v == "Absent" else "Present" if v in ("Slight", "Moderate")
                                     else np.nan)
    if "cirrhosis_etiology" in df:
        out["etiology_group"] = df["cirrhosis_etiology"].map(
            lambda v: np.nan if not isinstance(v, str) or not v.strip() else "Alcohol" if v.strip().lower() == "alcohol"
            else "MASLD" if v.strip().upper() in ("NAFLD", "MASLD", "NASH", "MASH") else "Other")
    return out


HYPNOGRAM_CODES = {"1": "deep", "2": "light", "3": "rem", "4": "awake"}


def parse_hypnogram(s) -> dict[str, float]:
    """Minutes of deep, light, REM and awake in one night's `sleep_phase_5_min` string (one character per
    5 minutes). Empty, missing or malformed strings give all NaN.

    The code mapping (1 deep, 2 light, 3 REM, 4 awake) is the Oura API convention and is assumed here:
    it still has to be confirmed with the study team."""
    out = dict.fromkeys(HYPNOGRAM_CODES.values(), np.nan)
    if not isinstance(s, str) or not s or set(s) - set(HYPNOGRAM_CODES):
        return out
    return {stage: s.count(code) * 5.0 for code, stage in HYPNOGRAM_CODES.items()}


def sleep_nights(df: pd.DataFrame) -> pd.DataFrame:
    """Per-night Oura sleep measures, same index as df. Row d holds the night that ended on the morning of study
    day d. Only the main night counts: type long_sleep, 3–16 h in bed and 1–14 h asleep (not gated on valid80:
    ring wear during the day says nothing about the night). Other nights and days without a record are missing,
    never zero. Times are on the bedtime_*_h18 scale (hours after 18:00 the evening before), which keeps nights
    that cross midnight continuous."""
    def num(col: str) -> pd.Series:
        return pd.to_numeric(df[col], errors="coerce") if col in df else pd.Series(np.nan, index=df.index)

    def text(col: str) -> pd.Series:
        return df[col].astype(object) if col in df else pd.Series(np.nan, index=df.index, dtype=object)

    hyp = text("sleep_phase_5_min")
    hyp = hyp.where(hyp.str.fullmatch(r"[1-4]+", na=False))
    tib_s, tst_s = num("time_in_bed_s"), num("total_sleep_duration")
    start, end = num("bedtime_start_h18"), num("bedtime_end_h18")
    main = text("type").eq(MAIN_SLEEP_TYPE) & (tib_s / 3600).between(3, 16) & (tst_s / 3600).between(1, 14)
    # The stage record must cover the time in bed (one character per 5 minutes) for WASO to mean anything.
    hyp_ok = main & (hyp.str.len() - tib_s / 300).abs().le(2)
    return pd.DataFrame({
        "main_sleep": main,
        "sleep_bed_h18": start.where(main),
        "sleep_wake_h18": end.where(main),
        "sleep_mid_h18": ((start + end) / 2).where(main),
        "sleep_tst_h": (tst_s / 3600).where(main),
        "sleep_eff_pct": (100 * tst_s / tib_s).where(main),
        "sleep_waso_min": (hyp.str.replace(r"^4+|4+$", "", regex=True).str.count("4") * 5.0).where(hyp_ok),
    }, index=df.index)


def bedtime_sd_min(df: pd.DataFrame) -> pd.Series:
    """Bedtime variability per main night: SD (minutes) of sleep_bed_h18 over the main nights in the 14 study days
    up to and including that night, when there are at least 7. Needs sleep_nights() columns and one row per day."""
    def one(g: pd.DataFrame) -> pd.Series:
        bed = g.set_index("days_since_start")["sleep_bed_h18"]
        days = range(int(bed.index.min()), int(bed.index.max()) + 1)
        sd = bed.reindex(days).rolling(14, min_periods=7).std() * 60
        return pd.Series(sd.reindex(bed.index).to_numpy(), index=g.index).where(g["sleep_bed_h18"].notna())

    if df.empty:
        return pd.Series(np.nan, index=df.index)
    return pd.concat([one(g) for _, g in df.groupby("name", sort=False)]).reindex(df.index)


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
    global HOSTED_DATA_FILE
    if DATA_SOURCE not in {"auto", "study", "synthetic"}:
        raise ValueError("HE_DATA_SOURCE must be auto, study, or synthetic")
    if in_hosted_lite():
        df, source = lite_study_table()[0].copy(), "study"
    elif PUBLIC_DEMO:
        if DATA_SOURCE == "study" or os.environ.get("HE_MRN_MAP"):
            raise RuntimeError("HE_PUBLIC_DEMO=1 serves synthetic data only: unset HE_DATA_SOURCE=study and HE_MRN_MAP")
        # Not even HE_SYNTHETIC_FILE: on a host it could point at anything.
        df, source = synthetic_cohort(), "synthetic"
    elif HOSTED_STUDY:
        url = os.environ.get("HE_DATA_URL", "")
        if not url:
            raise RuntimeError("HE_HOSTED_STUDY=1 needs HE_DATA_URL, the study cache to download (docs/hosting.md)")
        if DATA_SOURCE == "synthetic" or os.environ.get("HE_MRN_MAP"):
            raise RuntimeError("HE_HOSTED_STUDY=1 serves the downloaded study data only: "
                               "unset HE_DATA_SOURCE=synthetic and HE_MRN_MAP")
        HOSTED_DATA_FILE = fetch_hosted_data(url, os.environ.get("HE_DATA_TOKEN", ""))
        df, source = _read_cache_csv(HOSTED_DATA_FILE), "study"
    elif DATA_SOURCE == "synthetic":
        if not SYNTHETIC_FILE.exists():
            raise FileNotFoundError(f"Synthetic data not found: {SYNTHETIC_FILE}. Run the generator first.")
        df = pd.read_csv(SYNTHETIC_FILE, low_memory=False, dtype=TEXT_COLUMNS)
        source = "synthetic"
    elif DATA_FILE and DATA_FILE.exists():
        df, source = _read_rds(DATA_FILE), "study"
    elif DATA_SOURCE == "study":
        if DATA_FILE is None:
            default_data_file(DEFAULT_DATA_DIR)  # raises, saying how many .rds files it found
        raise FileNotFoundError(f"Study data not found: {DATA_FILE}")
    else:
        df, source = synthetic_cohort(), "synthetic"
    df = harmonise_columns(df).sort_values(["name", "days_since_start"], kind="stable").reset_index(drop=True)
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
    df = df.join(sleep_nights(df))
    df["sleep_bed_sd_min"] = bedtime_sd_min(df)
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


def _amount(key: str, diff: float) -> str:
    """A change in a tile's measure in words, e.g. "1 h 20 min" or "8 points"."""
    unit = PATIENT_METRICS[key]["unit"]
    if unit == "%":
        return f"{abs(diff):.0f} points"
    minutes = round(abs(diff) * (60 if unit in ("clock", "h") else 1))
    h, m = divmod(minutes, 60)
    return f"{h} h {m} min" if h and m else f"{h} h" if h else f"{m} min"


def _attention(full: pd.DataFrame, last_day: int) -> list[str]:
    """Sleep measures whose recent nights (last RECENT_DAYS study days) are beyond the patient's own 10th/90th
    percentile in the worse direction, in words ("Sleep midpoint 1 h 20 min later than usual"). Same minimum nights
    as the tiles at the default window; stricter than their middle-50% range so an ordinary week doesn't trigger it. `full`: indexed by study day."""
    flags = []
    for key in SLEEP_TILES:
        spec = SLEEP_METRICS[key]
        vals, recent, need = full[key], None, MIN_RECENT
        if key == "sleep_bed_sd_min" and "sleep_bed_h18" in full:
            vals, recent = _bed_sd(full["sleep_bed_h18"].reindex(range(int(full.index.min()), last_day + 1)),
                                   RECENT_DAYS)
            need = BED_SD_MIN[RECENT_DAYS]
        status, last7, _, med, _, _ = _status(vals, last_day, 0.1, 0.9, min_recent=need, recent=recent)
        if _is_worse(status, spec["higher_is"]):
            word = spec["change"][status == "above"]
            flags.append(f"{spec['label']} {_amount(key, last7 - med)} {word} than usual")
    return flags


def _he_status(row: pd.Series) -> str | None:
    """Return the most specific supported HE status; leave incomplete/conflicting rows unknown."""
    if row.get("anyOHE") == 1:
        return "Overt"
    if row.get("che_phes") == "Yes":
        return "Covert"
    return "No" if row.get("anyhe") == 0 else None


def first_day(df: pd.DataFrame) -> int:
    """The data's first study day: 0 in the Nov 2026 study file, 1 in older files. Never assume either."""
    return int(df["days_since_start"].min()) if len(df) else 0


@lru_cache(maxsize=1)
def patient_list() -> list[dict]:
    df, _ = load()
    day0 = first_day(df)
    out = []
    for pid, g in df.groupby("name", sort=True):
        first = g.iloc[0]
        last_day = int(g["days_since_start"].max())
        full = g.set_index("days_since_start")
        mid = full["sleep_mid_h18"].median()
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
            "attention": _attention(full, last_day),
            "days": int(len(g)),
            "valid_pct": int(round(100 * (g["valid80"] == 1).mean())),
            "nights": int(g["main_sleep"].sum()),
            "sleep_mid": _fmt(mid, 2),
            "sleep_mid_clock": _clock(18 + mid) if pd.notna(mid) else None,
            "sleep_tst": _fmt(full["sleep_tst_h"].median()),
            "sleep_eff": _fmt(full["sleep_eff_pct"].median(), 0),
            "steps": _fmt(full["steps"].where(full["valid80"] == 1).median(), 0),
            "spark": [_fmt(x, 2) for x in full["sleep_mid_h18"].reindex(range(day0, last_day + 1))
                      .rolling(7, min_periods=3).mean().iloc[::3]],
        })
    return out


MRN_SYSTEM = os.environ.get("SMART_MRN_SYSTEM", "https://jupyterhealth.org/fhir/identifier/mrn")


def _met_rows(strings: pd.Series) -> np.ndarray:
    out = np.full((len(strings), SLOTS), np.nan)
    for i, s in enumerate(strings):
        if isinstance(s, str):
            vals = np.array(s.split(","), dtype=float)
            if len(vals) == SLOTS:
                out[i] = vals
    return out


def _status(vals: pd.Series, last_day: int, lo: float = 0.25, hi: float = 0.75, window: int = RECENT_DAYS,
            min_recent: int = MIN_RECENT, recent: tuple[float, int] | None = None):
    """Recent values vs. the patient's own usual range. `vals` is indexed by study day, missing = no value.
    Recent = the median of the values in the last `window` study days (or `recent` = (value, n) when the caller
    summarises the window itself); usual = the lo–hi quantiles of the values before that window, so a bad week
    doesn't pull the range toward itself. With fewer than `min_recent` recent or MIN_BASE earlier values the
    status is "insufficient". Returns (status, recent value, q_lo, median, q_hi, n recent)."""
    v = vals.dropna()
    in_window, base = v[v.index > last_day - window], v[v.index <= last_day - window]
    value, n = recent if recent is not None else (in_window.median() if len(in_window) else np.nan, len(in_window))
    q1, med, q3 = base.quantile([lo, 0.5, hi]) if len(base) else (np.nan, np.nan, np.nan)
    if n < min_recent or len(base) < MIN_BASE:
        return "insufficient", value, q1, med, q3, n
    return ("below" if value < q1 else "above" if value > q3 else "typical"), value, q1, med, q3, n


def _is_worse(status: str, higher_is: str | None) -> bool:
    return higher_is is not None and status in ("below", "above") and (status == "below") == (higher_is == "better")


def _tone(status: str, spec: dict) -> str:
    """Pill colour: worse direction, better direction, a change with no better side, or none."""
    if status not in ("below", "above"):
        return "neutral" if status == "typical" else "none"
    if _is_worse(status, spec["higher_is"]):
        return "worse"
    return "shift" if spec["higher_is"] is None or spec.get("both_ways") else "better"


def _trend(vals: pd.Series, spread: float, clock: bool = False) -> str:
    v = vals.dropna()
    if len(v) < 28 or not spread or np.isnan(spread):
        return "Not enough data for a trend"
    recent, before = v.iloc[-14:].median(), v.iloc[-42:-14].median()
    change = (recent - before) / spread
    if clock:
        return "Later" if change > 0.5 else "Earlier" if change < -0.5 else "Stable"
    return "Increasing" if change > 0.5 else "Decreasing" if change < -0.5 else "Stable"


def _bed_sd(bed: pd.Series, window: int) -> tuple[pd.Series, tuple[float, int]]:
    """Bedtime variability for the profile. `bed`: sleep_bed_h18 indexed by every study day. Per main night, the SD
    (minutes) over the main nights in the `window` days up to it (needs BED_SD_MIN[window]); and the window's
    own value: (SD over the main nights in the last `window` days, how many there were)."""
    need = BED_SD_MIN[window]
    per_night = (bed.rolling(window, min_periods=need).std() * 60).where(bed.notna())
    recent = bed.iloc[-window:].dropna()
    return per_night, (recent.std() * 60 if len(recent) >= need else np.nan, len(recent))


def _tile(key: str, vals: pd.Series, last_day: int, weeks: list[int], window: int = RECENT_DAYS,
          recent: tuple[float, int] | None = None) -> dict:
    spec = PATIENT_METRICS[key]
    clock = spec["unit"] == "clock"
    noun = "nights" if nightly(key) else "valid days"
    need = BED_SD_MIN[window] if key == "sleep_bed_sd_min" else MIN_RECENT
    status, last7, q1, med, q3, n_recent = _status(vals, last_day, window=window, min_recent=need, recent=recent)
    v = vals.dropna()
    n_base = int((v.index <= last_day - window).sum())
    show = (lambda x: _clock(18 + x)) if clock else (lambda x: _fmt(x, spec["digits"]))
    has_base = n_base >= MIN_BASE
    words = spec.get("words") or TILE_WORDS.get(key, ("Lower than usual", "Higher than usual"))
    if status == "insufficient":
        pill = f"Not enough {noun.split()[-1]}"
        why = (f"{n_recent} {noun} in the last {window} days (needs {need})" if n_recent < need
               else f"usual range needs {MIN_BASE} earlier {noun} (has {n_base})")
    else:
        pill = {"below": words[0], "above": words[1], "typical": "Usual range"}[status]
        why = f"{n_recent} {noun} in the last {window} days"
    return {"key": key, "label": spec["label"], "unit": "" if clock else spec["unit"], "help": spec["help"],
            "last7": show(last7) if n_recent >= need and pd.notna(last7) else None,
            "baseline": show(med) if has_base else None,
            "range": [show(q1), show(q3)] if has_base else None,
            "status": status, "pill": pill, "tone": _tone(status, spec), "higher_is": spec["higher_is"],
            "n_obs": int(len(v)), "n_recent": n_recent, "why": why,
            "trend": _trend(vals, q3 - q1, clock) if has_base else "",
            "spark": [_fmt(x, 2) for x in vals.groupby(weeks).median()],
            "band": [_fmt(q1, 2), _fmt(q3, 2)] if has_base else [None, None], "clock": clock}


def _night(day: int, r: pd.Series) -> dict:
    """One night for the sleep-timing chart and its hypnogram (row r = the record filed on wake day `day`)."""
    kind = r.get("type")
    if not isinstance(kind, str):
        return {"day": day, "type": None}
    hyp = r.get("sleep_phase_5_min")
    return {"day": day, "type": kind, "main": bool(r.get("main_sleep")),
            "start": _fmt(r.get("bedtime_start_h18"), 3), "end": _fmt(r.get("bedtime_end_h18"), 3),
            "hyp": hyp if isinstance(hyp, str) and re.fullmatch(r"[1-4]+", hyp) else "",
            "tst": _fmt(r.get("total_sleep_duration", np.nan) / 3600, 1),
            "eff": _fmt(r.get("sleep_eff_pct"), 0), "waso": _fmt(r.get("sleep_waso_min"), 0)}


TIMING_NIGHTS = 30  # the sleep-timing chart shows the last 30 study days
ACTIVITY_CLASSES = ["Ring off", "Rest", "Inactive", "Low", "Medium", "High"]  # Oura class_5_min codes 0-5
ACTOGRAM_DAYS = 60  # the actogram shows the last 60 study days


def activity_summary(full: pd.DataFrame, is_valid: pd.Series) -> dict | None:
    """The profile's Activity section from valid days (>=80% wear): the 24-hour MET profile (percentiles per 5-minute
    slot, with the share of days resting in each slot), time in each activity class, and actogram rows (every day of
    the last ACTOGRAM_DAYS, invalid days marked). None when there is no 5-minute activity record."""
    if "class_5_min" not in full:
        return None
    has_cls = full["class_5_min"].map(lambda x: isinstance(x, str) and len(x) == SLOTS)
    ok = full[is_valid & has_cls]
    if ok.empty:
        return None
    cls = np.array([list(x) for x in ok["class_5_min"]], dtype=int)
    met = _met_rows(ok["met_5min"]) if "met_5min" in ok else np.full(cls.shape, np.nan)
    pct = None
    if np.isfinite(met).any():
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            pct = np.nanpercentile(met, [10, 25, 50, 75, 90], axis=0)
    tail = full.iloc[-ACTOGRAM_DAYS:]
    return {
        "days": int(len(cls)),
        "hours": [round(DAY_STARTS_AT + i * 5 / 60, 3) for i in range(SLOTS)],
        "rest_share": [round(float(x), 3) for x in (cls == 1).mean(axis=0)],
        "met": None if pct is None else {k: [_fmt(x, 2) for x in row] for k, row in zip(["p10", "p25", "p50", "p75", "p90"], pct)},
        "classes": [{"label": ACTIVITY_CLASSES[k], "hours": round(float((cls == k).sum() * 5 / 60 / len(cls)), 1)}
                    for k in range(6)],
        "actogram": [{"day": int(d), "valid": bool(is_valid.get(d, False)),
                      "cls": r if isinstance(r, str) and len(r) == SLOTS else ""}
                     for d, r in zip(tail.index, tail["class_5_min"])],
    }


def patient_detail(pid: str, window: int = RECENT_DAYS) -> dict | None:
    """Profile payload; `window` (one of WINDOWS) is the recent window the tiles compare with the usual range."""
    if window not in WINDOWS:
        raise ValueError(f"window must be one of {WINDOWS}")
    df, source = load()
    g = df[df["name"] == pid]
    if g.empty:
        return None
    first = g.iloc[0]
    day0 = first_day(df)
    days = list(range(day0, int(g["days_since_start"].max()) + 1))
    last_day = days[-1]
    full = g.set_index("days_since_start").reindex(days)
    is_valid = full["valid80"].eq(1)
    weeks = [(d - day0) // 7 + 1 for d in days]
    main = full["main_sleep"].eq(True)
    series, tiles, more = {}, [], []
    metrics = {}
    for key in PATIENT_TILES + MORE_TILES:
        if key not in full:
            continue
        night = nightly(key)
        recent = None
        vals = full[key].where(main) if night else full[key].where(is_valid)
        metrics[key] = PATIENT_METRICS[key]
        if key == "sleep_bed_sd_min":
            vals, recent = _bed_sd(full["sleep_bed_h18"], window)
            metrics[key] = {**metrics[key], "help": "How much bedtime moves night to night: standard deviation of "
                            f"bedtime over the main nights in the {window} study days up to each night (needs "
                            f"{BED_SD_MIN[window]} nights)"}
        if vals.notna().sum() == 0:
            continue
        roll = vals.rolling(7, min_periods=3).mean()
        shade = ~main if night else ~is_valid
        series[key] = {"values": [_fmt(x, 2) for x in vals], "rolling": [_fmt(x, 2) for x in roll],
                       "shade": [bool(x) for x in shade], "noun": "night" if night else "day"}
        tile = _tile(key, vals, last_day, weeks, window, recent)
        tile["help"] = metrics[key]["help"]
        (tiles if key in PATIENT_TILES else more).append(tile)

    wear = (1 - g["non_wear_pct"]).median() * 100 if "non_wear_pct" in g else None
    nights_help = f"Median over all {int(main.sum())} main nights in the study"
    mid = full["sleep_mid_h18"].median()
    summary = [
        {"label": "Sleep midpoint", "value": _clock(18 + mid) if pd.notna(mid) else None, "unit": "",
         "help": nights_help},
        {"label": "Total sleep", "value": _fmt(full["sleep_tst_h"].median()), "unit": "h", "help": nights_help},
        {"label": "Sleep efficiency", "value": _fmt(full["sleep_eff_pct"].median(), 0), "unit": "%",
         "help": nights_help},
        {"label": "Ring wear coverage", "value": int(round(100 * is_valid.mean())), "unit": "% valid days",
         "help": f"Days with ≥80% wear; median wear {wear:.0f}% of each day" if wear is not None else "Days with ≥80% wear"},
    ]
    mid_tile = next((t for t in tiles if t["key"] == "sleep_mid_h18"), None)
    return {
        "id": pid,
        "synthetic": source == "synthetic",
        "mrn": synthetic_mrn(pid) if source == "synthetic" else None,
        "sex": _baseline_value(first.get("sex")),
        "age": _baseline_value(first.get("age_yrs")),
        "ctp": _baseline_value(first.get("ctp_class")),
        "headline": clinical_headline(first),
        "clinical": clinical_snapshot(first, g.columns),
        "extras": snapshot_extras(pid, first, last_day, source, g.columns),
        "activity": activity_summary(full, is_valid),
        "days": days,
        "valid": [bool(x) for x in is_valid],
        "valid_pct": int(round(100 * is_valid.mean())),
        "nights_pct": int(round(100 * main.mean())),
        "series": series,
        "tiles": tiles,
        "more_tiles": more,
        "summary": summary,
        "nights": [_night(d, full.loc[d]) for d in days[-TIMING_NIGHTS:]],
        "mid_band": mid_tile["band"] if mid_tile else [None, None],
        "metrics": {k: metrics[k] for k in series},
        "window": window, "windows": list(WINDOWS), "bed_sd_min": BED_SD_MIN[window],
        "min_recent": MIN_RECENT, "min_base": MIN_BASE,
    }


def intraday(pid: str, start: int, n_days: int) -> dict | None:
    """5-minute MET and activity class for a window of consecutive study days."""
    df, _ = load()
    day0 = first_day(df)
    g = df[df["name"] == pid].set_index("days_since_start")
    if g.empty:
        return None
    last = int(g.index.max())
    n_days = max(1, min(int(n_days), 14))
    start = max(day0, min(int(start), last - n_days + 1))
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
    if hosted():
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
    nights = df[df["main_sleep"].eq(True)].groupby("name")[["sleep_mid_h18", "sleep_tst_h"]].median()
    mid = nights["sleep_mid_h18"].median()
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
        "sleep_mid": _clock(18 + mid) if pd.notna(mid) else None,
        "sleep_tst": _fmt(nights["sleep_tst_h"].median()),
        "night_rest": _fmt(per_person["night_rest_h"].median()),
        "sri": _fmt(per_person["sri"].median(), 0),
        "groups": groups,
        "group_menu": {name: {k: groups[k] for k in keys if k in groups} for name, keys in GROUP_MENU.items()
                       if any(k in groups for k in keys)},
        "group_hints": {k: GROUPS[k]["hint"] for k in groups if k in GROUPS and "hint" in GROUPS[k]},
        "metrics": {k: {"label": v["label"], "unit": v["unit"]} for k, v in COHORT_METRICS.items()},
        "metric_menu": {name: {k: COHORT_METRICS[k]["label"] for k in keys} for name, keys in COHORT_MENU.items()},
        "default_metric": DEFAULT_COHORT_METRIC,
    }


def fmt_p(p: float) -> str:
    """A p-value as shown on the page: never "p = 0"."""
    return "p < 0.001" if p < 0.001 else f"p = {p:.3f}" if p < 0.1 else f"p = {p:.2f}"


def _direction_words(spec: dict) -> tuple[str, str]:
    """(word when the group tends lower, when higher): earlier/later, shorter/longer, else lower/higher."""
    change = spec.get("change")
    if change and change[0] in ("earlier", "shorter"):
        return change
    return ("earlier", "later") if spec["unit"] == "clock" else ("lower", "higher")


def _effect_r(ref: pd.Series, other: pd.Series) -> float:
    """Rank-biserial r = 1 − 2·U₁/(n₁n₂) with U₁ for `ref`: positive when `other` tends higher."""
    u = stats.mannwhitneyu(ref, other).statistic
    return 1 - 2 * float(u) / (len(ref) * len(other))


def cohort_compare(group: str, metric: str, ids: list[str] | None = None) -> dict:
    """Per-level weekly trajectories and per-person medians of one measure, plus a test between levels.
    Sleep measures and Oura nightly vitals use main nights; activity measures valid days. A person needs
    MIN_PERSON values of the measure (others are counted in "excluded"); a level needs MIN_TEST people to be tested.
    Two levels: Mann-Whitney + rank-biserial r; more: Kruskal-Wallis + descriptive r of each level vs the first."""
    df, _ = load()
    if metric not in COHORT_METRICS or (group not in GROUPS and not (group == SELECTION and ids is not None)):
        raise KeyError("unknown group or metric")
    spec = COHORT_METRICS[metric]
    night = nightly(metric)
    rows = df[(df["main_sleep"].eq(True) if night else df["valid80"].eq(1)) & df[metric].notna()].copy()
    if group == SELECTION:
        rows["grp"] = np.where(rows["name"].isin(ids), *SELECTION_LEVELS)
        levels, label = list(SELECTION_LEVELS), "This selection vs rest of cohort"
    else:
        if ids is not None:
            rows = rows[rows["name"].isin(ids)]
        rows["grp"] = rows[f"grp_{group}"]
        levels, label = list(dict.fromkeys(GROUPS[group]["levels"].values())), GROUPS[group]["label"]
    rows = rows[rows["grp"].notna()]
    count = rows.groupby("name")[metric].size()
    keep = count.index[count >= MIN_PERSON]
    left_out = rows.loc[~rows["name"].isin(keep)].groupby("grp")["name"].nunique()
    rows = rows[rows["name"].isin(keep)]
    rows["week"] = (rows["days_since_start"] - first_day(df)) // 7 + 1
    weekly = rows.groupby(["name", "week", "grp"])[metric].agg(["mean", "size"]).reset_index()
    weekly = weekly[weekly["size"] >= MIN_WEEK]
    per_person = rows.groupby(["name", "grp"])[metric].median().reset_index()
    noun = "nights" if night else "valid days"
    out = {"group": label, "metric": spec, "metric_key": metric, "noun": noun,
           "basis": "main nights" if night else "valid days (≥80% ring wear)",
           "min_person": MIN_PERSON, "min_test": MIN_TEST, "levels": []}
    samples = {}
    for level in levels:
        w = weekly[weekly["grp"] == level].groupby("week")["mean"]
        n = w.count()
        q = w.quantile([0.25, 0.5, 0.75]).unstack().reindex(n.index) if len(n) else pd.DataFrame(columns=[0.25, 0.5, 0.75])
        q = q[n >= MIN_PEOPLE_WEEK] if len(n) else q
        pp = per_person.loc[per_person["grp"] == level, metric].dropna()
        samples[level] = pp
        out["levels"].append({
            "name": level, "n": int(len(pp)), "nights": int((rows["grp"] == level).sum()),
            "excluded": int(left_out.get(level, 0)),
            "weeks": [int(x) for x in q.index],
            "median": [_fmt(x, 2) for x in q[0.5]], "q1": [_fmt(x, 2) for x in q[0.25]], "q3": [_fmt(x, 2) for x in q[0.75]],
            "people": [_fmt(x, 2) for x in pp], "overall_median": _fmt(pp.median(), 2),
        })
    out["excluded"] = int(left_out.sum())
    tested = [lv for lv in levels if len(samples[lv]) >= MIN_TEST]
    out["untested_levels"] = [lv for lv in levels if lv not in tested]
    lower, higher = _direction_words(spec)
    if len(tested) < 2:
        out["direction"] = f"Too few patients for a test (need {MIN_TEST} per group)"
        return out
    ref = samples[tested[0]]
    if len(tested) == 2:
        p = float(stats.mannwhitneyu(ref, samples[tested[1]]).pvalue)
        r = _effect_r(ref, samples[tested[1]])
        out.update(test="Mann-Whitney", effect_r=round(r, 2))
        out["direction"] = (f"No clear difference (r = {abs(r):.2f})" if p >= 0.05 or r == 0 else
                            f"{tested[1]} tend {higher if r > 0 else lower} (r = {abs(r):.2f})")
    else:
        h, p = stats.kruskal(*[samples[lv] for lv in tested])
        p = float(p)
        pairs = []
        for lv in tested[1:]:
            r = _effect_r(ref, samples[lv])
            pairs.append({"level": lv, "vs": tested[0], "effect_r": round(r, 2),
                          "text": f"{lv} vs {tested[0]}: r = {abs(r):.2f}"
                                  + ("" if r == 0 else f" ({higher if r > 0 else lower})")})
        out.update(test="Kruskal-Wallis", h=round(float(h), 2), pairwise=pairs)
        out["direction"] = ("No clear difference across groups" if p >= 0.05 else "Groups differ") + ": " \
            + "; ".join(x["text"] for x in pairs)
    out.update(p_value=p, p_text=fmt_p(p))
    return out
