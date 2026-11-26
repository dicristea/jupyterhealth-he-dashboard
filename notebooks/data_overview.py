# ---
# jupyter:
#   jupytext:
#     formats: py:percent
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # What's in the Dataset
#
# **What this notebook is for:** a short, plain-language inventory of the
# HE study dataset: how big it is, who and which days it
# covers, what every column means (with units), how complete each column is,
# and what the file does **not** contain.
#
# Every result below is an **aggregate** (counts, percentages, percentiles).
# The notebook never shows a single participant's row.
#
# **How to run it:** open this file in VS Code or Jupyter and **Run All**.
# It takes a few seconds (on a study `.rds`, the first conversion with R takes
# about a minute).
#
# > 🔒 **Privacy:** keep this notebook's outputs on your computer. **Never
# > commit an `.ipynb` with outputs.** Only this blank `.py` version goes to
# > GitHub. The one file it saves goes to the git-ignored `notebook_outputs/`.

# %% [markdown]
# ## Data source
#
# Same switch as the dashboard: set **`HE_DATA_SOURCE`** before starting
# Jupyter.
#
# * `auto` (default): the study file if `HE_DATA_FILE` / `JH_DATA_FILE` is
#   set or the dashboard's data folder (`data.DEFAULT_DATA_DIR`, outside the
#   repo) holds a `.rds`, otherwise the synthetic cohort.
# * `study`: the study file only.
# * `synthetic`: the synthetic cohort from the dashboard's `data.py`. No R, no
#   study file.
#
# > ⚠️ On synthetic data the numbers describe the generator's settings
# > ([docs/synthetic-data.md](../docs/synthetic-data.md)), not the study.

# %%
import os
import sys
from pathlib import Path

DATA_SOURCE = os.environ.get("HE_DATA_SOURCE", "auto").strip().lower()
if DATA_SOURCE not in {"auto", "study", "synthetic"}:
    raise ValueError("HE_DATA_SOURCE must be auto, study or synthetic")
# The repo folder (it holds the dashboard's data.py), whether Jupyter started in notebooks/ or the repo.
# data.py is the only code that opens the study .rds: its R step keeps clock times only (no calendar dates).
REPO = next((p for p in (Path.cwd(), *Path.cwd().parents) if (p / "data.py").exists()), None)
if REPO is None:
    raise FileNotFoundError("Can't find the dashboard's data.py: run this notebook from the repo or its notebooks/ folder.")
sys.path.insert(0, str(REPO))
import data as he_data

# In the password-protected study site's JupyterLite (in the browser), the data comes from the site itself.
HOSTED_LITE = sys.platform == "emscripten" and any((p / "hosted_study.json").exists() for p in (Path.cwd(), *Path.cwd().parents))
ENV_FILE = os.environ.get("JH_DATA_FILE") or os.environ.get("HE_DATA_FILE")
FOUND_RDS = sorted((p for p in he_data.DEFAULT_DATA_DIR.glob("*.rds") if not p.name.startswith(".")),
                   key=lambda p: p.stat().st_mtime)
if HOSTED_LITE:
    DATA_SOURCE = "study"
elif DATA_SOURCE == "auto":
    DATA_SOURCE = "study" if ENV_FILE or FOUND_RDS else "synthetic"
SYNTHETIC = DATA_SOURCE == "synthetic"
print("Data source:", DATA_SOURCE + (" (from the study site)" if HOSTED_LITE else ""))
if SYNTHETIC:
    print("⚠️  SYNTHETIC DATA. The numbers below describe the generator settings, not the study.")

# %% [markdown]
# ## Step 1 · Load the data
#
# The dashboard's own loader (`data.py`) does the work. On study data, R
# converts the `.rds` once into a cache **outside the repo**, keeping the
# bedtimes as clock times only and refusing to write any calendar date.
# "Newest" means `HE_DATA_FILE` (or `JH_DATA_FILE`) if you set one, otherwise
# the most recently modified `.rds` in the dashboard's data folder.
#
# `harmonise_columns` then turns the `-999` "not available" code into missing,
# derives `ascites` from current/previous ascites, and keeps one row per
# participant-day.

# %%
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

pd.set_option("display.width", 160)
pd.set_option("display.max_rows", 120)
pd.set_option("display.max_colwidth", 90)
plt.rcParams.update({"figure.dpi": 110, "axes.spines.top": False, "axes.spines.right": False})

OUTPUT_DIR = REPO / "notebook_outputs"   # git-ignored (in JupyterLite: this browser only)
OUTPUT_DIR.mkdir(exist_ok=True)

if SYNTHETIC:
    raw, FILE_LABEL = he_data.synthetic_table()
elif HOSTED_LITE:
    raw, FILE_LABEL = he_data.lite_study_table()
else:
    if ENV_FILE:
        DATA_FILE = Path(ENV_FILE).expanduser().resolve()
    elif FOUND_RDS:
        DATA_FILE = FOUND_RDS[-1]
    else:
        raise FileNotFoundError(f"No .rds file in {he_data.DEFAULT_DATA_DIR}: set HE_DATA_FILE to the study file.")
    FILE_LABEL = DATA_FILE.name
    raw = he_data._read_rds(DATA_FILE)

# How many -999 codes each column had before harmonising (they become missing below).
CODED_999 = {c: int(raw[c].eq(he_data.MISSING_CODE).sum()) for c in raw.select_dtypes("number").columns}
RAW_SHAPE = raw.shape
df = he_data.harmonise_columns(raw).sort_values(["name", "days_since_start"], kind="stable").reset_index(drop=True)
del raw

# %% [markdown]
# ## Step 2 · Size and coverage at a glance
#
# One row = one participant on one study day. `days_since_start` is the study
# day (no calendar dates). A *valid day* is `valid80 == 1` (the ring was worn
# for at least 80% of the day).

# %%
days = pd.to_numeric(df["days_since_start"], errors="coerce")
valid = pd.to_numeric(df.get("valid80"), errors="coerce").eq(1) if "valid80" in df else pd.Series(False, index=df.index)
overview = pd.Series({
    "File": FILE_LABEL,
    "Rows × columns in the file": f"{RAW_SHAPE[0]:,} × {RAW_SHAPE[1]}",
    "Duplicate participant-days dropped": f"{RAW_SHAPE[0] - len(df):,}",
    "Participants": f"{df['name'].nunique():,}",
    "Participant-days": f"{len(df):,}",
    "Valid days (valid80 == 1)": f"{int(valid.sum()):,} ({valid.mean():.0%})",
    "Study-day range": f"{int(days.min())} to {int(days.max())}",
}, name="")
overview.to_frame()

# %% [markdown]
# ## Step 3 · Column dictionary
#
# Every column, with its group, type, how much is missing, how many distinct
# values it has, and what it means. Notes:
#
# * **Sleep durations are in seconds** (Oura convention). Row *d* holds the
#   sleep record of the night that ended on the morning of study day *d*.
# * **`bedtime_start_h18` / `bedtime_end_h18`** are clock times written as
#   *hours after 18:00 the evening before the wake day*: 5.0 = 23:00,
#   7.5 = 01:30. The real date-times never leave the R step.
# * **`sleep_phase_5_min`** is the hypnogram, one character per 5 minutes:
#   **1 deep, 2 light, 3 REM, 4 awake** (Oura API convention, *still to
#   confirm with the study team*).
# * **`-999` means missing** in the baseline labs; it is already counted as
#   missing in `% missing` (the `-999 codes` column shows how many there were).
# * Baseline clinical columns are measured once at enrollment and copied onto
#   every day, so their `% missing` is over participant-days.

# %%
GROUPS = {
    "IDs & days": ["name", "days_since_start", "day_type"],
    "Wear & activity": ["valid80", "valid90", "non_wear_pct", "adher_pct", "steps", "class_5_min", "met_5min"],
    "Sleep": ["type", "time_in_bed_s", "total_sleep_duration", "awake_time", "latency", "rem_sleep_duration",
              "rem_sleep_dur_pct", "restless_periods", "bedtime_start_h18", "bedtime_end_h18",
              "sleep_phase_5_min", "num_sleep_phase_5_min"],
    "Physiology": ["average_heart_rate", "average_hrv", "spo2", "average_breath", "breathing_disturbance_index"],
}
DESCRIPTIONS = {
    "name": "Participant code",
    "days_since_start": "Study day (0 = first day; no calendar date)",
    "day_type": "Weekday / weekend (older files only)",
    "valid80": "1 = ring worn ≥ 80% of the day (a valid day)",
    "valid90": "1 = ring worn ≥ 90% of the day",
    "non_wear_pct": "Share of the day the ring was not worn, %",
    "adher_pct": "Participant's overall wear adherence, %",
    "steps": "Steps that day",
    "class_5_min": "Oura activity class per 5 min (288 characters from 04:00; 0 non-wear, 1 rest, 2 inactive, 3-5 low/medium/high)",
    "met_5min": "Mean MET per 5 min (288 values, made by data.py from the per-minute MET)",
    "type": "Oura sleep record type: long_sleep (main night), sleep, late_nap, rest",
    "time_in_bed_s": "Time in bed, seconds (bedtime end − start; made in the R step)",
    "total_sleep_duration": "Total sleep, seconds",
    "awake_time": "Time awake in bed, seconds",
    "latency": "Time to fall asleep, seconds",
    "rem_sleep_duration": "REM sleep, seconds",
    "rem_sleep_dur_pct": "REM sleep as a share of total sleep",
    "restless_periods": "Number of restless periods in the night",
    "bedtime_start_h18": "Bedtime, hours after 18:00 the evening before the wake day",
    "bedtime_end_h18": "Wake time, hours after 18:00 the evening before the wake day",
    "sleep_phase_5_min": "Hypnogram per 5 min: 1 deep, 2 light, 3 REM, 4 awake (confirm with study team)",
    "num_sleep_phase_5_min": "Number of 5-minute slots in the hypnogram",
    "average_heart_rate": "Average heart rate, beats/min (0 is an error)",
    "average_hrv": "Average heart-rate variability (RMSSD), ms",
    "spo2": "Average blood-oxygen saturation, %",
    "average_breath": "Average breathing rate during sleep, breaths/min",
    "breathing_disturbance_index": "Oura breathing disturbance index during sleep",
    "sex": "Sex", "age_yrs": "Age at enrollment, years",
    "decomp": "Decompensated cirrhosis at enrollment (1 yes, 0 no)",
    "anyhe": "Any hepatic encephalopathy (1 yes, 0 no)", "anyOHE": "Any overt HE (1 yes, 0 no)",
    "che_phes": "Covert HE by PHES", "phes_score": "PHES score (lower = worse)",
    "he_meds": "On HE medication", "ever_he_meds": "Ever on HE medication",
    "medication_for_hepatic_enc": "Medication for hepatic encephalopathy",
    "ascites": "Ascites now or before (derived by data.py from current/previous ascites)",
    "current_ascites": "Ascites at enrollment: Absent / Slight / Moderate", "previous_ascites": "Ascites before enrollment",
    "medication_for_ascites": "Medication for ascites",
    "previous_encephalopathy": "HE before enrollment", "variceal_bleeding_history": "History of variceal bleeding",
    "cirrhosis_etiology": "Cause of cirrhosis",
    "ctp_class": "Child-Turcotte-Pugh class", "ctp_cat": "Child-Turcotte-Pugh category", "ctp_score": "Child-Turcotte-Pugh score",
    "meld": "MELD score",
    "total_bilirubin_t0": "Total bilirubin at enrollment (usually mg/dL; -999 = missing)",
    "albumin_t0": "Albumin at enrollment (usually g/dL; -999 = missing)",
    "inr_t0": "INR at enrollment (no unit; -999 = missing)",
    "bb_use_t0": "Beta-blocker use at enrollment",
    "liver_frailty_index": "Liver Frailty Index", "liver_frailty_index_90d": "Liver Frailty Index, within 90 days",
    "liver_frailty_index_90d_180d": "Liver Frailty Index, 90-180 days", "fli_cat": "Liver Frailty Index category",
    "psqiglobal": "Pittsburgh Sleep Quality Index, global (older files)", "cldq_total": "CLDQ quality of life, total (older files)",
    "whichsite": "Study site (WCM / CUMC)", "whicharm": "Study arm (Pragmatic / Frailty)",
    "prior_fitness_track": "Used a fitness tracker before the study", "still_use_fitness_tracker": "Still uses a fitness tracker",
}
GROUP_OF = {c: g for g, cols in GROUPS.items() for c in cols}
CLINICAL = [c for c in df.columns if c not in GROUP_OF and c in DESCRIPTIONS]
GROUP_OF |= dict.fromkeys(CLINICAL, "Baseline clinical")
GROUP_ORDER = [*GROUPS, "Baseline clinical", "Other"]

dictionary = pd.DataFrame({
    "column": df.columns,
    "group": [GROUP_OF.get(c, "Other") for c in df.columns],
    "dtype": [str(df[c].dtype) for c in df.columns],
    "% missing": (100 * df.isna().mean()).round(1).to_numpy(),
    "n distinct": df.nunique().to_numpy(),
    "-999 codes": [CODED_999.get(c, 0) for c in df.columns],
    "description": [DESCRIPTIONS.get(c, "(not described yet: ask the study team)") for c in df.columns],
})
dictionary["group"] = pd.Categorical(dictionary["group"], GROUP_ORDER, ordered=True)
dictionary = dictionary.sort_values(["group", "column"], kind="stable").reset_index(drop=True)
dictionary.to_csv(OUTPUT_DIR / "data_overview_columns.csv", index=False)  # aggregates only
print(f"Saved to {OUTPUT_DIR / 'data_overview_columns.csv'}")
dictionary

# %% [markdown]
# ### Columns per group

# %%
dictionary.groupby("group", observed=True).agg(columns=("column", "size"), median_pct_missing=("% missing", "median"))

# %% [markdown]
# ## Step 4 · How much data each person has
#
# Three counts per participant: study days in the file, valid days
# (`valid80 == 1`), and **main sleep nights** (an Oura `long_sleep` record with
# 3-16 h in bed and 1-14 h asleep, the same rule the dashboard uses). The
# histograms show how these are spread across participants.

# %%
nights = he_data.sleep_nights(df)
per_person = pd.DataFrame({"days": df.groupby("name").size(),
                           "valid days": valid.groupby(df["name"]).sum(),
                           "main sleep nights": nights["main_sleep"].groupby(df["name"]).sum()})
fig, axes = plt.subplots(1, 3, figsize=(12, 3.2), sharey=True)
for ax, col in zip(axes, per_person.columns):
    ax.hist(per_person[col], bins=20, color="#1565c0", alpha=0.8)
    ax.set_title(col)
    ax.set_xlabel("per participant")
axes[0].set_ylabel("participants")
plt.tight_layout()
plt.show()
per_person.describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9]).round(1).drop(index=["min", "max"])

# %% [markdown]
# ## Step 5 · Sleep records
#
# Each day has at most one Oura sleep record. `long_sleep` is the main night;
# `sleep`, `late_nap` and `rest` are shorter or daytime periods. "No record"
# means there was no sleep record that day. The second table checks the
# units: if durations are in seconds, median total sleep on main nights should
# be roughly 5-8 hours.

# %%
types = df["type"].astype(object).fillna("(no record)") if "type" in df else pd.Series("(no record)", index=df.index)
type_shares = types.value_counts().to_frame("participant-days")
type_shares["share"] = (type_shares["participant-days"] / len(df)).map("{:.1%}".format)
print(f"Main nights: {int(nights['main_sleep'].sum()):,} of {len(df):,} participant-days")
type_shares

# %%
SLEEP_SECONDS = [c for c in ["time_in_bed_s", "total_sleep_duration", "awake_time", "latency", "rem_sleep_duration"] if c in df]
main = nights["main_sleep"]
(df.loc[main, SLEEP_SECONDS].apply(pd.to_numeric, errors="coerce") / 3600).quantile([0.1, 0.25, 0.5, 0.75, 0.9]) \
    .round(2).T.rename_axis("hours, main nights")

# %% [markdown]
# ## Step 6 · Daily numeric columns
#
# Percentiles over all participant-days that have a value (10th to 90th, so a
# single extreme day never shows).

# %%
DAILY = [c for c in dictionary.loc[dictionary["group"].isin(["Wear & activity", "Sleep", "Physiology"]), "column"]
         if pd.api.types.is_numeric_dtype(df[c])]
daily_summary = df[DAILY].describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9]).T.drop(columns=["min", "max", "std"])
daily_summary.round(2)

# %% [markdown]
# ## Step 7 · Baseline clinical columns, one value per participant
#
# Baseline columns repeat the enrollment value on every day, so they are
# summarised **per participant** (each person counted once). Columns with a
# few categories show counts per level; numeric columns show percentiles
# (10th to 90th, no minimum or maximum).

# %%
baseline = df.groupby("name")[CLINICAL].first()
is_numeric = {c: pd.api.types.is_numeric_dtype(baseline[c]) and baseline[c].nunique() > 5 for c in CLINICAL}

numeric_baseline = baseline[[c for c in CLINICAL if is_numeric[c]]]
numeric_baseline.describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9]).T.drop(columns=["min", "max", "std"]).round(2)

# %%
level_counts = pd.concat(
    {c: baseline[c].astype(object).fillna("(missing)").value_counts() for c in CLINICAL if not is_numeric[c]},
    names=["column", "level"]).to_frame("participants")
level_counts

# %% [markdown]
# ## Step 8 · What's *not* in this file
#
# * **No calendar dates.** Only the study day (`days_since_start`) and clock
#   times (`bedtime_*_h18`). We don't know which weekday or season a row is.
# * **No event dates.** Decompensation, HE episodes, hospitalisations and
#   medication changes are one value per person at enrollment, so nothing can
#   be lined up "before vs. after" an event.
# * **No longitudinal labs.** Bilirubin, albumin, INR, CTP and MELD are
#   enrollment values only.
# * **No temperature** (Oura temperature deviation), **no lowest heart rate**,
#   no Oura sleep score or efficiency, and no deep or light sleep durations
#   (they can be derived from the hypnogram, see Step 3).
# * **One sleep record per day**: other sleep periods that day (for example a
#   daytime nap next to the main night) are not in the file.
#
# For these, see Part 1 of `he_trajectories_phenotypes.py` (where event data
# could come from).
