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
# # Getting to Know the HE Study Oura Dataset
#
# **What this notebook is for:** a beginner-friendly tour of the HE study's
# Oura dataset. Each block of code has a plain-language note
# above it that says *what it does* and *why we care*.
#
# **What we want to learn:**
# 1. Who is in the data (how many people, men vs. women, ages)?
# 2. How much data each person has, and how complete it is.
# 3. What sleep looks like, and whether it differs between men and women.
# 4. Whether there is any menstrual-cycle information.
# 5. What real sleep (the Oura sleep record) looks like, how complete it is, and
#    how it differs between clinical groups (Steps 20–24, exploratory).
# 6. Detailed activity and vitals views for research (Steps 25–26).
# 7. Which parameters from the current Oura Clinical Workbench actually exist
#    in the data, so the next dashboard only shows what we really have.
#
# **How to run it in VS Code:** open this file and click **Run Cell** above
# each `# %%` block, or **Run All**. If VS Code asks for a kernel, pick your
# Python environment. The `.ipynb` copy works the same way.
#
# > 🔒 **Privacy:** study data stays on your computer. Keep this
# > notebook and all of its outputs in your JupyterHealth folder. **Do not
# > commit an `.ipynb` file with outputs to GitHub**, because the outputs
# > contain the data. Only this blank `.py` version is safe to share.

# %% [markdown]
# ## Data source
#
# Same switch as the dashboard: set **`HE_DATA_SOURCE`** before starting
# Jupyter.
#
# * `auto` (default): the study file if `JH_DATA_DIR` is set or the
#   dashboard's data folder (`data.DEFAULT_DATA_DIR`, outside the repo) holds
#   exactly one `.rds`, otherwise the synthetic cohort.
# * `study`: the study data folder only.
# * `synthetic`: the synthetic cohort from the dashboard's `data.py`. No R, no
#   study file.
#
# > ⚠️ **On synthetic data, every result in this notebook is an artefact of
# > the generator's settings, not a clinical finding.** Group differences,
# > p-values and correlations show what the generator was told to build. See
# > [docs/synthetic-data.md](../docs/synthetic-data.md).

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
import data as he_data  # not "data": later cells use that name for a column of values

# In the password-protected study site's JupyterLite (in the browser), the data comes from the site itself.
HOSTED_LITE = sys.platform == "emscripten" and any((p / "hosted_study.json").exists() for p in (Path.cwd(), *Path.cwd().parents))
if HOSTED_LITE:
    DATA_SOURCE = "study"
elif DATA_SOURCE == "auto":
    found_rds = sorted(he_data.DEFAULT_DATA_DIR.glob("*.rds"))
    DATA_SOURCE = "study" if os.environ.get("JH_DATA_DIR") or len(found_rds) == 1 else "synthetic"
SYNTHETIC = DATA_SOURCE == "synthetic"
print("Data source:", DATA_SOURCE + (" (from the study site)" if HOSTED_LITE else ""))
if SYNTHETIC:
    print("⚠️  SYNTHETIC DATA. Every result below is an artefact of the generator settings "
          "(docs/synthetic-data.md), not a clinical finding.")

# %% [markdown]
# ## Step 0 · Tools we need
#
# These are the standard Python libraries for tables (`pandas`), math
# (`numpy`), charts (`matplotlib`), and simple statistics (`scipy`).
# If you get `ModuleNotFoundError`, run this in a terminal:
# `pip install pandas numpy matplotlib scipy openpyxl`
#
# **About `.rds` files (R's format).** The study file contains R features that
# Python can't open, such as labelled values and columns that hold whole
# lists. Step 3 hands it to the dashboard's own loader (`data.py`), which asks
# **R** (`Rscript`, with the `haven` package) to convert the file to a CSV
# once and caches it **outside the repo** (`exploration_outputs/_cache/*.v3.csv`).
# That R step also turns the bedtime timestamps into **clock times only**
# (`bedtime_start_h18`, `bedtime_end_h18`: hours after 18:00 the evening
# before waking) and refuses to write any calendar date, as HIPAA Safe Harbor
# requires. R must be installed for this. Run `which Rscript` in a terminal to check.

# %%
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

try:
    from scipy import stats
    HAVE_SCIPY = True
except ImportError:
    HAVE_SCIPY = False
    print("scipy not installed: statistical tests will be skipped (charts still work).")

pd.set_option("display.max_columns", 80)
pd.set_option("display.width", 160)
plt.rcParams.update({"figure.dpi": 110, "axes.spines.top": False, "axes.spines.right": False})

SEX_COLORS = {"Female": "#c2185b", "Male": "#1565c0", "Unknown": "#9e9e9e"}

# %% [markdown]
# ## Step 1 · Tell the notebook where the data is
#
# **Change `DATA_DIR`** to the folder that holds the dataset files. In VS
# Code you can right-click the folder → **Copy Path** and paste it between
# the quotes.
#
# `MAX_FULL_LOAD_MB` protects your laptop. Some files, like the heart-rate
# and HRV files, are 20–30 GB. Anything bigger than this limit gets only a
# **peek** at its first rows instead of being fully loaded.
#
# On synthetic data there is no folder: Step 3 takes the table from the
# dashboard's `data.py`, and summary tables are saved to `notebook_outputs/`
# in the repo, which git ignores.

# %%
# Optional: if the auto-pick in Step 3 chooses the wrong files, type the file names here.
DAY_FILE = None          # e.g. "daily_summary.csv": one row per participant per day
PARTICIPANT_FILE = None  # e.g. "participants.csv": one row per participant

MAX_FULL_LOAD_MB = 500

if SYNTHETIC or HOSTED_LITE:
    OUTPUT_DIR = REPO / "notebook_outputs"   # git-ignored, inside the repo (in JupyterLite: this browser only)
    OUTPUT_DIR.mkdir(exist_ok=True)
else:
    # Default: the dashboard's data folder, next to the repo (data.DEFAULT_DATA_DIR)
    DATA_DIR = Path(os.environ.get("JH_DATA_DIR", he_data.DEFAULT_DATA_DIR)).expanduser().resolve()
    # Where summary tables get saved. This is local only and lives next to your data, NOT in the repo.
    OUTPUT_DIR = DATA_DIR.parent / "exploration_outputs"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("Looking for data in:", DATA_DIR, "| exists:", DATA_DIR.exists())
    # The study file: HE_DATA_FILE if set, otherwise the single .rds directly in DATA_DIR (older files in subfolders are skipped)
    STUDY_FILE = he_data.DATA_FILE if os.environ.get("HE_DATA_FILE") else he_data.default_data_file(DATA_DIR)
    print("Study file:", STUDY_FILE.name)

# %% [markdown]
# ## Step 2 · What files do we have?
#
# This lists every data file in the folder and its subfolders, with sizes.
# It is a quick inventory before we open anything.

# %%
DATA_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls", ".parquet", ".rds", ".json", ".sav", ".dta"}

if SYNTHETIC or HOSTED_LITE:
    print("No data folder here (" + ("synthetic data" if SYNTHETIC else "the study site sends the data in Step 3") + "), so there are no files to list.")
    files = []
else:
    files = sorted(p for p in DATA_DIR.rglob("*") if p.is_file() and p.suffix.lower() in DATA_EXTENSIONS)
inventory = pd.DataFrame({
    "file": [str(p.relative_to(DATA_DIR)) for p in files],
    "type": [p.suffix.lower() for p in files],
    "size_MB": [round(p.stat().st_size / 1e6, 1) for p in files],
})
inventory["will_load"] = np.where(inventory["size_MB"] <= MAX_FULL_LOAD_MB, "full", "peek only")
# Only the current study .rds is opened (through data.py); older .rds copies (e.g. in archive/) are listed, not read.
is_other_rds = [p.suffix.lower() == ".rds" and p.resolve() != STUDY_FILE.resolve() for p in files]
inventory.loc[is_other_rds, "will_load"] = "skip (not the current study file)"
inventory

# %% [markdown]
# ## Step 3 · Open the files
#
# `read_any` opens a file whatever its format. For big files it reads only the
# first rows. Afterwards we print each file's shape (rows × columns) and
# column names, so you can match them against the study's **data
# dictionary**.
#
# Then we guess which file is which:
# * **Day-level table**: has a participant ID *and* a date column. Each row is
#   one person-day, the main table we will use.
# * **Participant-level table**: has an ID but no date. Each row is one
#   person (demographics, baseline clinical info).

# %%
def read_rds(path: Path) -> pd.DataFrame:
    # The dashboard's loader: R converts the file once into data.CACHE_DIR (outside the repo, `*.v3.csv`),
    # bedtimes become clock times only, and the R step stops if any calendar-date column would be written.
    # harmonise_columns: -999 → missing, `ascites` from current/previous ascites, one row per person-day.
    return he_data.harmonise_columns(he_data._read_rds(path))


def read_any(path: Path, nrows: int | None = None) -> pd.DataFrame:
    """Open csv / excel / parquet / rds / json / spss / stata into a pandas table."""
    ext = path.suffix.lower()
    if ext in {".csv", ".tsv"}:
        return pd.read_csv(path, sep="\t" if ext == ".tsv" else ",", nrows=nrows, low_memory=False)
    if ext in {".xlsx", ".xls"}:
        return pd.read_excel(path, nrows=nrows)
    if ext == ".parquet":
        df = pd.read_parquet(path)
        return df.head(nrows) if nrows else df
    if ext == ".rds":
        df = read_rds(path)
        return df.head(nrows) if nrows else df
    if ext == ".json":
        df = pd.read_json(path, lines=path.stat().st_size > 50e6)
        return df.head(nrows) if nrows else df
    if ext == ".sav":
        return pd.read_spss(path)
    if ext == ".dta":
        return pd.read_stata(path)
    raise ValueError(f"Don't know how to read {ext}")


tables: dict[str, pd.DataFrame] = {}
for _, row in inventory.iterrows():
    if row["will_load"].startswith("skip"):
        continue
    path = DATA_DIR / row["file"]
    try:
        tables[row["file"]] = read_any(path, nrows=None if row["will_load"] == "full" else 5)
    except Exception as exc:  # keep going if one file fails
        print(f"⚠️  Could not read {row['file']}: {exc}")

if SYNTHETIC:
    tables["synthetic cohort"], origin = he_data.synthetic_table()
    print("Synthetic cohort:", origin)
elif HOSTED_LITE:
    raw, origin = he_data.lite_study_table()
    tables["study data"] = he_data.harmonise_columns(raw.copy())
    print("Study data from", origin)

for name, df in tables.items():
    print(f"\n📄 {name}: {df.shape[0]:,} rows × {df.shape[1]} columns")
    print("   columns:", ", ".join(map(str, df.columns)))

# %% [markdown]
# ## Step 4 · Teach the notebook what each column means
#
# Every study names its columns differently (`total_sleep_duration`,
# `tst_min`, `sleep_total`…). This block searches column names for keywords
# and assigns each one a **role**, such as "REM sleep" or "resting heart rate".
#
# **Check the printed table.** If a role is matched to the wrong column, or
# a column was missed, fix it in `COLUMN_OVERRIDES` (for example
# `"rem": "rem_sleep_minutes"`) and re-run from this block down.

# %%
# role → (plain-English label, list of regex patterns to look for in column names)
ROLE_PATTERNS: dict[str, tuple[str, list[str]]] = {
    # identity & time
    "id":            ("Participant ID",            [r"^(participant|subject|patient|record|study)_?id$", r"^pid$", r"^id$", r"participant", r"subject"]),
    "date":          ("Date",                      [r"^(summary_)?date$", r"^day$", r"calendar_date", r"date"]),
    "valid_day":     ("Valid day flag (≥80% data)", [r"valid_?day", r"valid"]),
    # demographics / baseline
    "sex":           ("Sex / gender",              [r"^sex$", r"gender", r"sex"]),
    "age":           ("Age",                       [r"^age", r"age_"]),
    "race":          ("Race / ethnicity",          [r"race", r"ethnic"]),
    "bmi":           ("BMI",                       [r"bmi"]),
    # sleep (nightly summary)
    "total_sleep":   ("Total sleep",               [r"total_sleep", r"sleep_total", r"\btst\b", r"sleep_duration", r"total.*sleep"]),
    "time_in_bed":   ("Time in bed",               [r"time_in_bed", r"in_bed", r"\btib\b"]),
    "rem":           ("REM sleep",                 [r"\brem", r"rem_"]),
    "deep":          ("Deep sleep",                [r"deep", r"\bsws\b", r"n3"]),
    "light":         ("Light sleep",               [r"light", r"n1|n2"]),
    "awake":         ("Awake time / WASO",         [r"awake", r"waso", r"wake_after"]),
    "efficiency":    ("Sleep efficiency",          [r"efficien"]),
    "latency":       ("Sleep latency",             [r"latency", r"onset_lat"]),
    "sleep_score":   ("Oura sleep score",          [r"sleep_score", r"score_sleep"]),
    "bedtime_start": ("Bedtime start (timestamp)", [r"bedtime_start", r"sleep_start", r"bed_start"]),
    "bedtime_end":   ("Bedtime end (timestamp)",   [r"bedtime_end", r"sleep_end", r"wake_time", r"bed_end"]),
    "hypnogram":     ("Sleep-stage string (5-min)", [r"hypnogram", r"sleep_phase", r"sleep_stage"]),
    # Oura's class_5_min is an ACTIVITY string (0 non-wear … 5 high), not sleep stages
    "activity_class":("Activity-class string (5-min)", [r"class_5_min", r"activity_class"]),
    "psqi":          ("PSQI sleep-quality questionnaire", [r"psqi"]),
    # physiology
    "hrv":           ("HRV (rMSSD)",               [r"hrv", r"rmssd"]),
    "resting_hr":    ("Resting / lowest HR",       [r"resting_h", r"lowest_h", r"rhr", r"min_h(eart_)?r"]),
    "avg_hr":        ("Average HR",                [r"average_h", r"avg_h", r"mean_h", r"heart_rate"]),
    "temp_dev":      ("Temperature deviation",     [r"temp.*dev", r"temperature", r"skin_temp"]),
    "resp_rate":     ("Respiratory rate",          [r"resp", r"breath"]),
    "spo2":          ("Blood oxygen (SpO₂)",       [r"spo2", r"oxygen", r"o2_sat"]),
    "readiness":     ("Oura readiness score",      [r"readiness"]),
    # activity
    "steps":         ("Steps",                     [r"step"]),
    "active_cal":    ("Calories (active or total)", [r"active_cal", r"cal_active", r"calorie"]),
    "inactivity":    ("Inactivity / sedentary",    [r"inactiv", r"sedentar"]),
    "activity_score":("Oura activity score",       [r"activity_score"]),
    "wear_time":     ("Wear time / non-wear",      [r"wear", r"non_wear"]),
    # clinical (liver)
    "meld":          ("MELD / MELD-Na",            [r"meld"]),
    "child_pugh":    ("Child-Pugh",                [r"child", r"pugh", r"\bctp\b", r"ctp_class"]),
    "ctp_group":     ("Child-Pugh group (A vs B/C)", [r"ctp_cat"]),
    "he":            ("Hepatic encephalopathy (HE)", [r"\bhe\b", r"^anyhe$", r"_he$", r"enceph", r"\bmhe\b"]),
    "overt_he":      ("Overt HE (ever)",           [r"anyohe", r"overt"]),
    "covert_he":     ("Covert HE by PHES",         [r"che_phes", r"\bche\b"]),
    "he_meds":       ("HE medication at enrollment", [r"^he_meds$"]),
    "decomp":        ("Decompensated cirrhosis",   [r"^decomp"]),
    "variceal":      ("Variceal bleeding history", [r"varic"]),
    "frailty":       ("Liver Frailty Index",       [r"frailty_index", r"\blfi\b"]),
    "ascites":       ("Ascites",                   [r"^ascites$", r"ascit"]),
    "ammonia":       ("Ammonia",                   [r"ammon", r"nh3"]),
    "albumin":       ("Albumin",                   [r"albumin"]),
    "bilirubin":     ("Bilirubin",                 [r"bili"]),
    "inr":           ("INR",                       [r"\binr\b"]),
    "sodium":        ("Sodium",                    [r"sodium", r"^na$"]),
    "creatinine":    ("Creatinine",                [r"creat"]),
    "etiology":      ("Liver disease cause",       [r"etiolog", r"cause", r"nash|masld|alcohol.*liver|hcv|hbv"]),
    "hospitalized":  ("Hospitalization",           [r"hospital", r"admi"]),
    # cognitive tests / PROs
    "phes":          ("PHES score",                [r"phes_score", r"phes"]),
    "stroop":        ("Stroop test",               [r"stroop"]),
    "animal_naming": ("Animal naming test",        [r"animal"]),
    "nct":           ("Number connection test",    [r"\bnct", r"connection"]),
    "pro":           ("Patient-reported outcome",  [r"\bpro\b", r"promis", r"cldq", r"qol", r"questionnaire", r"survey"]),
    # medications / confounders
    "lactulose":     ("Lactulose",                 [r"lactul"]),
    "rifaximin":     ("Rifaximin",                 [r"rifax"]),
    "alcohol":       ("Alcohol use",               [r"alcohol", r"audit", r"peth"]),
    "opioids":       ("Opioids / narcotics",       [r"opioid", r"narcot"]),
    "sleep_apnea":   ("Sleep apnea",               [r"apnea", r"\bosa\b", r"cpap"]),
    # menstrual cycle
    "menstrual":     ("Menstrual / cycle info",    [r"menstr", r"period", r"cycle", r"ovulat", r"luteal", r"follic", r"pregnan", r"menopaus"]),
}

COLUMN_OVERRIDES: dict[str, str] = {
    # Study file: "name" is the participant code, and there are
    # no calendar dates, only "days_since_start" (study day 0–180).
    "id": "name",
    "date": "days_since_start",
    # The current file has look-alikes the keyword search would pick first:
    "valid_day": "valid80",            # not valid90
    "rem": "rem_sleep_duration",       # seconds; rem_sleep_dur_pct is a 0–1 fraction
    "resp_rate": "average_breath",     # not breathing_disturbance_index
    "bedtime_start": "bedtime_start_h18",  # clock time only: hours after 18:00 the evening before waking
    "bedtime_end": "bedtime_end_h18",
}
# An override only applies when that column exists; otherwise the keyword search runs as usual.


def find_column(columns, patterns, taken):
    lowered = {c: str(c).lower() for c in columns}
    for pattern in patterns:  # earlier patterns are more specific, so they win
        for col, low in lowered.items():
            if col not in taken and re.search(pattern, low):
                return col
    return None


def map_roles(columns) -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    overrides = {role: c for role, c in COLUMN_OVERRIDES.items() if c in columns or role in {"id", "date"}}
    taken = set(overrides.values())
    for role, (_, patterns) in ROLE_PATTERNS.items():
        col = overrides.get(role) or find_column(columns, patterns, taken)
        found[role] = col
        if col:
            taken.add(col)
    return found


# Pick the day-level and participant-level tables (largest table with id+date / with id only)
def pick_tables():
    day_name, part_name = DAY_FILE, PARTICIPANT_FILE
    if day_name is None or part_name is None:
        candidates = []
        for name, df in tables.items():
            roles = map_roles(df.columns)
            candidates.append((name, len(df), roles["id"] is not None, roles["date"] is not None))
        if day_name is None:
            day_opts = sorted([c for c in candidates if c[2] and c[3]], key=lambda c: -c[1])
            day_name = day_opts[0][0] if day_opts else None
        if part_name is None:
            part_opts = sorted([c for c in candidates if c[2] and not c[3] and c[0] != day_name], key=lambda c: -c[1])
            part_name = part_opts[0][0] if part_opts else None
    return day_name, part_name


day_name, part_name = pick_tables()
print("Day-level table:        ", day_name)
print("Participant-level table:", part_name)

# %% [markdown]
# ### Step 4b · Build one combined table
#
# If demographics live in a separate participant file, we **merge** (join)
# them onto every day using the participant ID, the way you would do a
# VLOOKUP in Excel. The result is one table called `df`: **one row = one
# person on one day**, with that person's age and sex copied onto each of
# their rows.
#
# **No calendar dates.** The study file has `days_since_start` (study day 0–180;
# older files started at 1) instead of real dates. That's good for privacy, but
# it means we can't line days up across people by calendar, season or day of
# the week. Where the notebook would use a date, it uses **study day** instead.
# Without a weekday/weekend column (`day_type`), Step 15 just says so.

# %%
day = tables[day_name].copy()
day_roles = map_roles(day.columns)

if part_name:
    part = tables[part_name].copy()
    part_roles = map_roles(part.columns)
    part = part.rename(columns={part_roles["id"]: day_roles["id"]})
    extra_cols = [c for c in part.columns if c not in day.columns or c == day_roles["id"]]
    df = day.merge(part[extra_cols], on=day_roles["id"], how="left")
else:
    df = day

ROLES = map_roles(df.columns)
ID, DATE = ROLES["id"], ROLES["date"]
DAY_IS_NUMBER = pd.api.types.is_numeric_dtype(df[DATE])
if not DAY_IS_NUMBER:
    df[DATE] = pd.to_datetime(df[DATE], errors="coerce")
df = df.sort_values([ID, DATE]).reset_index(drop=True)

role_table = pd.DataFrame(
    [(role, label, ROLES[role] or "— not found —") for role, (label, _) in ROLE_PATTERNS.items()],
    columns=["role", "what it means", "matched column"],
)
print(f"Combined table: {len(df):,} rows × {df.shape[1]} columns")
role_table

# %% [markdown]
# ## Step 5 · An automatic data dictionary
#
# For every column this shows:
# * **type**: number, text or date.
# * **% missing**: how often it's blank.
# * **unique values**: 2 usually means yes/no; many means a measurement.
# * **level**: *participant* if the value never changes for a person (like
#   sex), or *day* if it changes day to day (like steps).
# * **example**: a sample value.
#
# Keep this next to the study's data dictionary. It's the quickest way to
# see what every variable is.

# %%
def column_level(series: pd.Series) -> str:
    per_person_unique = series.groupby(df[ID]).nunique(dropna=True)
    return "participant" if (per_person_unique <= 1).all() else "day"


dictionary = pd.DataFrame({
    "column": df.columns,
    "type": [str(t) for t in df.dtypes],
    "% missing": (df.isna().mean() * 100).round(1).values,
    "unique values": df.nunique().values,
    "level": [column_level(df[c]) if c != ID else "id" for c in df.columns],
    "example": [df[c].dropna().iloc[0] if df[c].notna().any() else None for c in df.columns],
})
dictionary["example"] = dictionary["example"].astype(str).str.slice(0, 40)
dictionary.to_csv(OUTPUT_DIR / "auto_data_dictionary.csv", index=False)
dictionary

# %% [markdown]
# ## Step 6 · Who is in the study? (cohort basics)
#
# Counts of people and days, the calendar range covered, and **how many days
# each person contributed**. The histogram shows whether most people have
# lots of data or whether a few heavy users dominate.

# %%
days_per_person = df.groupby(ID)[DATE].nunique()
print(f"Participants:            {df[ID].nunique():,}")
print(f"Participant-days (rows): {len(df):,}")
if DAY_IS_NUMBER:
    print(f"Study days covered:      day {df[DATE].min()} → day {df[DATE].max()} (no calendar dates in this file)")
else:
    print(f"Date range:              {df[DATE].min():%Y-%m-%d} → {df[DATE].max():%Y-%m-%d}")
print(f"Days per person: median {days_per_person.median():.0f}, "
      f"mean {days_per_person.mean():.0f}, min {days_per_person.min()}, max {days_per_person.max()}")

fig, ax = plt.subplots(figsize=(8, 3.5))
ax.hist(days_per_person, bins=30, color="#5c6bc0")
ax.axvline(days_per_person.median(), color="black", ls="--", label=f"median = {days_per_person.median():.0f} days")
ax.set(title="How many days of data does each person have?", xlabel="days of data", ylabel="number of people")
ax.legend()
plt.show()

# %% [markdown]
# ## Step 7 · Demographics: men, women and age
#
# The sex column can be coded in different ways (`M/F`, `Male/Female`,
# `1/2`). We translate it into **Female / Male / Unknown**. If your data uses
# numbers, check the data dictionary and fill in `SEX_MAP`. For example, if
# 1 = male and 2 = female, write `{1: "Male", 2: "Female"}`.

# %%
SEX_MAP: dict = {}  # e.g. {1: "Male", 2: "Female"}; leave empty to auto-detect text codes

SEX = ROLES["sex"]
if SEX:
    print("Raw values in the sex column:", df[SEX].dropna().unique()[:10])

    def normalise_sex(v):
        if v in SEX_MAP:
            return SEX_MAP[v]
        s = str(v).strip().lower()
        if s in {"f", "female", "woman", "w"}:
            return "Female"
        if s in {"m", "male", "man"}:
            return "Male"
        return "Unknown"

    df["sex_clean"] = df[SEX].map(normalise_sex)
else:
    df["sex_clean"] = "Unknown"
    print("⚠️  No sex/gender column found. Add it to COLUMN_OVERRIDES in Step 4 if it exists.")

# one row per person, used for demographics
people = df.groupby(ID).agg(sex=("sex_clean", "first"), days=(DATE, "nunique"))
AGE = ROLES["age"]
if AGE:
    people["age"] = pd.to_numeric(df.groupby(ID)[AGE].first(), errors="coerce")

sex_counts = people["sex"].value_counts()
print("\nPeople by sex:\n", sex_counts.to_string())
print("\nDays of data by sex:\n", people.groupby("sex")["days"].sum().to_string())

fig, axes = plt.subplots(1, 2, figsize=(11, 3.8))
axes[0].bar(sex_counts.index, sex_counts.values, color=[SEX_COLORS.get(s, "grey") for s in sex_counts.index])
axes[0].set(title="People by sex", ylabel="number of people")
for i, v in enumerate(sex_counts.values):
    axes[0].text(i, v, str(v), ha="center", va="bottom")
if AGE:
    for s, grp in people.groupby("sex"):
        axes[1].hist(grp["age"].dropna(), bins=15, alpha=0.6, label=s, color=SEX_COLORS.get(s, "grey"))
    axes[1].set(title="Age by sex", xlabel="age (years)", ylabel="number of people")
    axes[1].legend()
    print("\nAge summary by sex:\n", people.groupby("sex")["age"].describe().round(1).to_string())
else:
    axes[1].set_axis_off()
    axes[1].text(0.5, 0.5, "No age column found", ha="center")
plt.tight_layout()
plt.show()

# %% [markdown]
# ### Other baseline characteristics
#
# Any other **participant-level** column with only a few categories (race,
# liver disease cause, HE history, ascites, Child-Pugh class) is counted
# here, split by sex. This is the start of a classic **"Table 1"**, the
# demographics table that opens most clinical papers.

# %%
participant_cols = dictionary.query("level == 'participant' and `unique values` <= 10")["column"].tolist()
participant_cols = [c for c in participant_cols if c not in {SEX, "sex_clean", ID}]
for c in participant_cols:
    table = pd.crosstab(df.groupby(ID)[c].first(), people["sex"], margins=True, margins_name="Total")
    print(f"\n── {c} ──\n{table.to_string()}")
if not participant_cols:
    print("No other categorical participant-level columns found.")

# %% [markdown]
# ## Step 8 · How complete is the data? (data quality)
#
# Wearables miss data all the time: the ring is charging, the person isn't
# wearing it, or a sensor fails. Before trusting any chart, we check:
# * **% of days marked valid.** The dataset's statisticians made a
#   `valid_day` flag meaning "≥80% of the day has good data". It comes with
#   the dataset; we don't compute it.
# * **% missing for each measurement.** Some measurements, such as SpO₂,
#   aren't recorded every day.

# %%
VALID = ROLES["valid_day"]
if VALID:
    v = df[VALID]
    df["is_valid"] = v.astype(str).str.lower().isin({"1", "true", "yes", "y", "1.0"})
    print(f"Valid days: {df['is_valid'].mean():.1%} of all rows")
    valid_share = df.groupby(ID)["is_valid"].mean()
    print(f"People with ≥80% of their days valid: {(valid_share >= 0.8).mean():.0%}")
else:
    df["is_valid"] = True
    print("No valid_day column found, so every day is treated as valid.")

measure_roles = ["total_sleep", "rem", "deep", "light", "awake", "efficiency", "latency", "sleep_score",
                 "hrv", "resting_hr", "avg_hr", "temp_dev", "resp_rate", "spo2", "readiness",
                 "steps", "active_cal", "inactivity", "activity_score"]
present = {r: ROLES[r] for r in measure_roles if ROLES[r]}

missing = pd.DataFrame({
    "measure": [ROLE_PATTERNS[r][0] for r in present],
    "column": list(present.values()),
    "% days missing": [df[c].isna().mean() * 100 for c in present.values()],
    "% people with NO data": [(df.groupby(ID)[c].count() == 0).mean() * 100 for c in present.values()],
}).round(1).sort_values("% days missing")

fig, ax = plt.subplots(figsize=(8, 0.35 * len(missing) + 1))
ax.barh(missing["measure"], missing["% days missing"], color="#ef6c00")
ax.set(title="How often is each measure missing?", xlabel="% of days missing", xlim=(0, 100))
plt.tight_layout()
plt.show()
missing

# %% [markdown]
# ### Missing data over time, person by person
#
# Each row is a person, each column is a week, and the colour is how many of
# that week's nights have sleep data. Gaps show up as white stripes. This
# helps answer "can we trust a 7-day trend for this person?", which matters a
# lot for a clinician dashboard.

# %%
sleep_col = ROLES["total_sleep"] or ROLES["steps"] or next(iter(present.values()), None)
if sleep_col:
    tmp = df[[ID, DATE, sleep_col]].copy()
    days_in = tmp[DATE] - tmp.groupby(ID)[DATE].transform("min")
    tmp["study_week"] = (days_in if DAY_IS_NUMBER else days_in.dt.days) // 7
    grid = tmp.assign(has=tmp[sleep_col].notna()).pivot_table(index=ID, columns="study_week", values="has", aggfunc="sum")
    grid = grid.loc[grid.sum(axis=1).sort_values(ascending=False).index]
    fig, ax = plt.subplots(figsize=(10, 5))
    im = ax.imshow(grid.values, aspect="auto", cmap="Blues", vmin=0, vmax=7, interpolation="nearest")
    ax.set(title=f"Days with data per week ({sleep_col})", xlabel="week since that person's first day", ylabel="participants (sorted)")
    ax.set_yticks([])
    plt.colorbar(im, ax=ax, label="days with data (0–7)")
    plt.show()

# %% [markdown]
# ## Step 8b · The 5-minute activity string (and the rest-based sleep proxy)
#
# **Real sleep is in Steps 20–24.** The dataset has Oura's **sleep
# record**, one per night: total sleep, awake time, latency, REM, the
# 5-minute sleep-stage string (hypnogram) and bedtime/wake clock times. Those
# are analysed in Steps 20–24. This step builds a sleep *proxy* from activity,
# for comparison and for days with no sleep record.
#
# The file also has `class_5_min`, Oura's **activity class** for
# every 5 minutes of the day (288 digits = 24 h):
#
# | digit | meaning |
# |---|---|
# | 0 | non-wear (ring off) |
# | 1 | **rest** (includes sleep) |
# | 2 | inactive (sitting still, awake) |
# | 3 / 4 / 5 | low / medium / high activity |
#
# Oura's activity day runs **4 am → 4 am**, so the first digit is 04:00 to
# 04:05. The first chart below checks that assumption: if it's right, rest
# should peak in the middle of the night.
#
# From the string we build **sleep proxies**:
# * **Rest time (24 h)**: all the "1"s in the day.
# * **Main night rest bout**: the longest stretch of rest between 6 pm and
#   noon the next morning. Breaks of up to 10 minutes are allowed, so getting
#   up briefly doesn't split the night. This is our best stand-in for a night's
#   sleep, and it needs the *next* day's string too.
# * **Rest onset / midpoint**: when that bout starts and its middle point, a
#   rough **bedtime** and **circadian timing** marker.
#
# ⚠️ These are **proxies, not real sleep**. "Rest" can include lying awake.
# They're good for comparing people and spotting changes, but not for saying
# "this person slept 6.2 h". Where the Oura sleep record exists (Steps 20–24),
# prefer it.

# %%
ACTIVITY_CODES = {"0": "Non-wear", "1": "Rest", "2": "Inactive", "3": "Low", "4": "Medium", "5": "High"}
ACTIVITY_COLORS = ["#e0e0e0", "#3949ab", "#90caf9", "#ffcc80", "#ff9800", "#e65100"]
DAY_STARTS_AT = 4   # hour of day of the first 5-min slot (Oura activity day starts 04:00)
SLOTS = 288

ACT = ROLES["activity_class"]
if ACT:
    strings = df[ACT].astype("string")
    strings = strings.where(strings.str.fullmatch(rf"[0-5]{{{SLOTS}}}", na=False))
    print(f"Days with a complete {SLOTS}-slot activity string: {strings.notna().mean():.1%}")

    df["rest_h"] = strings.str.count("1") * 5 / 60
    df["inactive_h"] = strings.str.count("2") * 5 / 60
    df["active_h"] = strings.str.count("[345]") * 5 / 60

    # Average day: share of people in each class at each time of day
    slots = np.array([list(s) for s in strings.dropna()], dtype=np.int8)
    shares = np.stack([(slots == k).mean(axis=0) for k in range(6)])
    hours = DAY_STARTS_AT + np.arange(SLOTS) * 5 / 60
    fig, ax = plt.subplots(figsize=(11, 3.4))
    ax.stackplot(hours, shares, labels=list(ACTIVITY_CODES.values()), colors=ACTIVITY_COLORS)
    ticks = np.arange(DAY_STARTS_AT, DAY_STARTS_AT + 25, 2)
    ax.set_xticks(ticks, [f"{t % 24:02d}:00" for t in ticks])
    ax.set(xlim=(hours[0], hours[-1]), ylim=(0, 1), ylabel="share of all days",
           title="An average day: what the ring saw at each time of day")
    ax.legend(loc="upper left", bbox_to_anchor=(1, 1), fontsize=8)
    plt.tight_layout()
    plt.show()
    # the very last slot of each string is often "rest" padding, so skip it and smooth a little
    smooth = np.convolve(shares[1][:-1], np.ones(7) / 7, mode="same")
    peak = hours[3 + smooth[3:-3].argmax()] % 24
    print(f"Rest peaks at about {int(peak):02d}:{int(peak % 1 * 60):02d}. "
          f"{'✅ middle of the night, so the 4 am start looks right' if peak < 6 or peak > 23 else '❌ check DAY_STARTS_AT'}")

    # Main night rest bout: from 6 pm on this day to noon on the next day
    win_start = (18 - DAY_STARTS_AT) * 12
    win_end = SLOTS + (12 - DAY_STARTS_AT) * 12
    next_day_step = 1 if DAY_IS_NUMBER else pd.Timedelta(days=1)
    next_string = strings.groupby(df[ID]).shift(-1)
    is_next_day = df.groupby(ID)[DATE].shift(-1) == df[DATE] + next_day_step
    night_pair_ok = is_next_day & df["is_valid"] & df["is_valid"].shift(-1, fill_value=False)
    bout_re = re.compile(r"1(?:[0-5]{0,2}1)*")

    def clock(hours_after_6pm):
        minutes = int(round((18 + hours_after_6pm) * 60)) % 1440
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    def main_rest_bout(today, tomorrow):
        if pd.isna(today) or pd.isna(tomorrow):
            return (np.nan, np.nan, np.nan)
        window = (today + tomorrow)[win_start:win_end]
        best = max(bout_re.finditer(window), key=lambda m: m.end() - m.start(), default=None)
        if best is None or best.end() - best.start() < 24:   # under 2 h is not a night
            return (np.nan, np.nan, np.nan)
        return ((best.end() - best.start()) * 5 / 60, best.start() * 5 / 60, (best.start() + best.end()) / 2 * 5 / 60)

    bouts = [main_rest_bout(t, n) for t, n in zip(strings.where(night_pair_ok), next_string.where(night_pair_ok))]
    df[["night_rest_h", "rest_onset_6pm", "rest_mid_6pm"]] = pd.DataFrame(bouts, index=df.index)
    print(f"Nights with a main rest bout: {df['night_rest_h'].notna().mean():.1%} of rows; "
          f"median {df['night_rest_h'].median():.1f} h, typical onset {clock(df['rest_onset_6pm'].median())}, "
          f"typical midpoint {clock(df['rest_mid_6pm'].median())}")
else:
    print("No 5-minute activity string found.")

# %% [markdown]
# ## Step 9 · Cleaning: units, impossible values and outliers
#
# This is the usual "get the data analysis-ready" step, done **carefully**
# because this is clinical data.
#
# 1. **Units.** Oura exports sleep durations in **seconds**, while other
#    tools use minutes or hours. We detect this from typical values and
#    convert everything to **hours**.
# 2. **Impossible values** (for example 20 h of sleep, or a heart rate of 5)
#    are almost certainly errors. We **blank them out** (set to NaN) instead
#    of deleting the whole day.
# 3. **Statistical outliers** are unusual but possible values. We **flag**
#    them using the IQR rule (more than 1.5 × IQR beyond the middle 50%) but
#    **don't remove them**. In a sick population an extreme night may be the
#    exact signal we're looking for, such as an HE episode.
# 4. **Stage percentages.** If the data only has REM *minutes*, we compute
#    REM % = REM ÷ total sleep, which is what the workbench shows.

# %%
def to_hours(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    med = s.median()
    if pd.isna(med):
        return s
    if med > 1000:   # seconds (e.g. 25,000 s ≈ 7 h)
        return s / 3600
    if med > 24:     # minutes (e.g. 420 min = 7 h)
        return s / 60
    return s          # already hours


clean = df.copy()
for role in ["total_sleep", "time_in_bed", "rem", "deep", "light", "awake"]:
    if ROLES[role]:
        clean[f"{role}_h"] = to_hours(clean[ROLES[role]])

# Latency: convert to minutes
if ROLES["latency"]:
    lat = pd.to_numeric(clean[ROLES["latency"]], errors="coerce")
    clean["latency_min"] = lat / 60 if lat.median() > 120 else lat

# Stage percentages (of total sleep)
if "total_sleep_h" in clean:
    for stage in ["rem", "deep", "light"]:
        if f"{stage}_h" in clean:
            clean[f"{stage}_pct"] = 100 * clean[f"{stage}_h"] / clean["total_sleep_h"]

# Efficiency sometimes comes as 0–1 instead of 0–100
if ROLES["efficiency"]:
    eff = pd.to_numeric(clean[ROLES["efficiency"]], errors="coerce")
    clean["efficiency_pct"] = eff * 100 if eff.median() <= 1 else eff
elif {"total_sleep_h", "time_in_bed_h"} <= set(clean.columns):
    # No Oura efficiency column: total sleep ÷ time in bed
    clean["efficiency_pct"] = 100 * clean["total_sleep_h"] / clean["time_in_bed_h"]

# No deep/light columns either: their share of the asleep 5-minute slots in the hypnogram
# (assumed Oura codes 1 deep, 2 light, 3 REM, 4 awake; to confirm with the study team)
if ROLES["hypnogram"]:
    hyp_s = clean[ROLES["hypnogram"]]
    hyp_s = hyp_s.where(hyp_s.astype(str).str.fullmatch(r"[1-4]+"))
    asleep_slots = pd.to_numeric(hyp_s.str.count("[123]"), errors="coerce")
    for stage, code in [("deep", "1"), ("light", "2"), ("rem", "3")]:
        if f"{stage}_pct" not in clean:
            share = pd.to_numeric(hyp_s.str.count(code), errors="coerce") / asleep_slots.where(asleep_slots > 0)
            clean[f"{stage}_pct"] = 100 * share

# Copy other numeric measures under simple names
for role in ["hrv", "resting_hr", "avg_hr", "temp_dev", "resp_rate", "spo2", "steps",
             "sleep_score", "readiness", "activity_score", "active_cal", "inactivity"]:
    if ROLES[role]:
        clean[role] = pd.to_numeric(clean[ROLES[role]], errors="coerce")

# Physiologically impossible ranges → blank out
PLAUSIBLE = {
    "total_sleep_h": (0.5, 16), "rem_pct": (0, 60), "deep_pct": (0, 60), "light_pct": (0, 100),
    "efficiency_pct": (20, 100), "latency_min": (0, 240), "hrv": (2, 250), "resting_hr": (25, 140),
    "avg_hr": (30, 180), "temp_dev": (-5, 5), "resp_rate": (5, 40), "spo2": (70, 100), "steps": (0, 80_000),
    "night_rest_h": (2, 16),
}
cleaning_log = []
for col, (lo, hi) in PLAUSIBLE.items():
    if col in clean:
        bad = clean[col].notna() & ~clean[col].between(lo, hi)
        cleaning_log.append((col, f"{lo}–{hi}", int(bad.sum()), "set to blank"))
        clean.loc[bad, col] = np.nan

# Friendly display names for the metrics we'll use from here on
METRICS = {
    "total_sleep_h": "Total sleep (h)", "rem_pct": "REM sleep (%)", "deep_pct": "Deep sleep (%)",
    "light_pct": "Light sleep (%)", "awake_h": "Awake in bed (h)", "efficiency_pct": "Sleep efficiency (%)",
    "latency_min": "Sleep latency (min)", "sleep_score": "Oura sleep score", "hrv": "HRV (ms)",
    "resting_hr": "Resting HR (bpm)", "avg_hr": "Average HR (bpm)", "temp_dev": "Temp deviation (°C)",
    "resp_rate": "Respiratory rate (/min)", "spo2": "SpO₂ (%)", "readiness": "Readiness score",
    "steps": "Steps / day", "activity_score": "Activity score", "active_cal": "Calories / day",
    "inactivity": "Inactivity",
    "night_rest_h": "Night rest bout (h, sleep proxy)", "rest_onset_6pm": "Rest onset (h after 6 pm)",
    "rest_mid_6pm": "Rest midpoint (h after 6 pm)", "rest_h": "Rest time, 24 h (h)",
    "inactive_h": "Inactive time (h)", "active_h": "Active time (h)",
}
METRICS = {k: v for k, v in METRICS.items() if k in clean and clean[k].notna().any()}

# IQR outlier flags (kept, not removed)
for col in METRICS:
    q1, q3 = clean[col].quantile([0.25, 0.75])
    iqr = q3 - q1
    flagged = (clean[col] < q1 - 1.5 * iqr) | (clean[col] > q3 + 1.5 * iqr)
    clean[f"{col}_outlier"] = flagged
    cleaning_log.append((col, "IQR rule", int(flagged.sum()), "flagged only (kept)"))

# Use valid days only from here on
valid = clean[clean["is_valid"]].copy()
print(f"Using {len(valid):,} valid days out of {len(clean):,}. Metrics available: {list(METRICS.values())}")
pd.DataFrame(cleaning_log, columns=["measure", "rule", "rows affected", "action"])

# %% [markdown]
# ## Step 10 · What does sleep look like overall?
#
# One histogram per measure. The dashed line is the **median**, the middle
# value, which is safer than the average when data is skewed. These charts
# show what "normal" looks like in this cohort, which helps when setting
# dashboard thresholds. The current workbench uses guesses such as 7 h sleep
# and 50 ms HRV.

# %%
cols = list(METRICS)
n = len(cols)
ncols = 3
fig, axes = plt.subplots(int(np.ceil(n / ncols)), ncols, figsize=(13, 3 * int(np.ceil(n / ncols))))
for ax, col in zip(axes.flat, cols):
    data = valid[col].dropna()
    ax.hist(data, bins=40, color="#26a69a")
    ax.axvline(data.median(), color="black", ls="--", lw=1)
    ax.set_title(f"{METRICS[col]}\nmedian {data.median():.1f}", fontsize=10)
for ax in list(axes.flat)[n:]:
    ax.set_axis_off()
plt.tight_layout()
plt.show()

valid[cols].describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9]).T.round(1).rename(index=METRICS)

# %% [markdown]
# ## Step 11 · Do men and women sleep differently?
#
# **Average each person first, then compare.** Someone with 400 days would
# otherwise count 10× more than someone with 40 days, and thousands of
# participant-days aren't thousands of independent people. The real sample size is the number of
# **participants**, so each dot below is one person's average.
#
# **The Mann-Whitney test** answers "could a difference this big happen by
# chance if men and women were really the same?" A **p-value below 0.05**
# is the usual cut-off for "probably a real difference". However:
# * We test many measures at once, so a few will cross 0.05 by luck. A
#   stricter cut-off, **0.05 ÷ number of tests** (the Bonferroni
#   correction), is shown as well.
# * A difference between men and women may be caused by something else,
#   such as age, liver disease severity or alcohol use. These are
#   **confounders**. Treat this as "worth a closer look", not a conclusion.
#
# **Effect size** (rank-biserial r, from −1 to 1) says *how big* the
# difference is: about 0.1 is small, 0.3 medium and 0.5 large.

# %%
person_avg = valid.groupby(ID)[cols].mean()
person_avg["sex"] = people["sex"]
if "age" in people:
    person_avg["age"] = people["age"]


def fmt_p(p: float) -> str:
    """p-values for reading: never "p = 0"."""
    if p is None or pd.isna(p):
        return "n/a"
    return "< 0.001" if p < 0.001 else f"{p:.3f}" if p < 0.01 else f"{p:.2f}"


def compare_two_groups(table: pd.DataFrame, group_col: str, a, b, metrics: list[str]) -> pd.DataFrame:
    rows = []
    for col in metrics:
        x = table.loc[table[group_col] == a, col].dropna()
        y = table.loc[table[group_col] == b, col].dropna()
        row = {"measure": METRICS.get(col, col), f"n {a}": len(x), f"n {b}": len(y),
               f"median {a}": x.median(), f"median {b}": y.median(), "difference": x.median() - y.median()}
        if HAVE_SCIPY and len(x) >= 3 and len(y) >= 3:
            u, p = stats.mannwhitneyu(x, y, alternative="two-sided")
            row["p-value"] = p
            row["p"] = fmt_p(p)
            row["effect size r"] = 1 - 2 * u / (len(x) * len(y))  # > 0: group a tends lower
        rows.append(row)
    out = pd.DataFrame(rows)
    out = out.round({c: 3 for c in out.select_dtypes("number").columns if c != "p-value"})
    if "p-value" in out:
        cutoff = 0.05 / len(metrics)
        out["significant (strict)"] = np.where(out["p-value"] < cutoff, "yes", "no")
    return out


sex_table = person_avg[person_avg["sex"].isin(["Female", "Male"])]
fig, axes = plt.subplots(int(np.ceil(n / ncols)), ncols, figsize=(13, 3.2 * int(np.ceil(n / ncols))))
for ax, col in zip(axes.flat, cols):
    groups = [sex_table.loc[sex_table["sex"] == s, col].dropna() for s in ["Female", "Male"]]
    ax.boxplot(groups, showfliers=False, widths=0.5)
    ax.set_xticks([1, 2], ["Female", "Male"])
    for i, (g, s) in enumerate(zip(groups, ["Female", "Male"]), start=1):
        ax.scatter(np.random.normal(i, 0.06, len(g)), g, s=10, alpha=0.6, color=SEX_COLORS[s])
    ax.set_title(METRICS[col], fontsize=10)
for ax in list(axes.flat)[n:]:
    ax.set_axis_off()
fig.suptitle("Each dot = one person's average on valid days", y=1.0)
plt.tight_layout()
plt.show()

sex_results = compare_two_groups(sex_table, "sex", "Female", "Male", cols)
sex_results.to_csv(OUTPUT_DIR / "sex_comparison.csv", index=False)
sex_results

# %% [markdown]
# ### Same comparison, split by age group
#
# Women's sleep changes a lot around menopause (roughly age 45–55), and
# most people with cirrhosis are older. Splitting by age keeps an age
# difference from looking like a sex difference.
#
# 👀 **Check the y-axis.** Each small chart zooms in on its own range, so a
# line that looks steep may be a difference of only 0.1 h. Read the numbers in
# the table, not just the shape of the lines.

# %%
if "age" in person_avg and person_avg["age"].notna().any():
    person_avg["age_group"] = pd.cut(person_avg["age"], bins=[0, 49, 64, 200], labels=["<50", "50–64", "65+"])
    key_metrics = [c for c in ["total_sleep_h", "night_rest_h", "rest_mid_6pm", "rem_pct", "deep_pct", "efficiency_pct",
                               "hrv", "resting_hr", "avg_hr", "steps"] if c in cols]
    summary = person_avg.groupby(["age_group", "sex"], observed=True)[key_metrics].median().round(1)
    summary["people"] = person_avg.groupby(["age_group", "sex"], observed=True).size()
    display(summary.rename(columns=METRICS))

    fig, axes = plt.subplots(1, len(key_metrics), figsize=(3.2 * len(key_metrics), 3.4))
    for ax, col in zip(np.atleast_1d(axes), key_metrics):
        for s in ["Female", "Male"]:
            med = person_avg[person_avg["sex"] == s].groupby("age_group", observed=False)[col].median()
            ax.plot(med.index.astype(str), med.values, marker="o", label=s, color=SEX_COLORS[s])
        ax.set_title(METRICS[col], fontsize=10)
    np.atleast_1d(axes)[0].legend()
    plt.tight_layout()
    plt.show()
else:
    print("No age column, so we can't split by age.")

# %% [markdown]
# ## Step 12 · Is there menstrual-cycle information?
#
# First we look for any column about periods, cycles, ovulation, pregnancy
# or menopause.
#
# **If there isn't one** (likely, because standard Oura exports don't
# include it), there's an *indirect hint*. Oura's **temperature
# deviation** rises about 0.3 °C after ovulation and drops when a period
# starts, so in pre-menopausal women it often moves in a **~28-day wave**.
# Below we:
# 1. plot temperature deviation for a few women under 50, and
# 2. measure how much each woman's temperature "repeats itself" every
#    21–35 days (autocorrelation). Higher means more cycle-like.
#
# ⚠️ This is a **hint, not a diagnosis**. Illness, alcohol, medications and
# liver disease all move temperature too. Real cycle data would need to come
# from the Oura app's cycle features or a questionnaire.

# %%
menstrual_cols = [c for c in df.columns if re.search("|".join(ROLE_PATTERNS["menstrual"][1]), str(c).lower())]
print("Menstrual-related columns found:", menstrual_cols or "none")
for c in menstrual_cols:
    print(f"\n{c}:\n", df[c].value_counts(dropna=False).head(10).to_string())

if "temp_dev" in valid:
    women = people[(people["sex"] == "Female")]
    if "age" in people:
        women = women[women["age"] < 50]
    print(f"\nWomen{' under 50' if 'age' in people else ''} with temperature data: {len(women)}")

    def cycle_score(series: pd.Series) -> float:
        s = series.asfreq("D").interpolate(limit=3)
        s = s - s.rolling(60, center=True, min_periods=20).mean()  # remove slow drift
        if s.notna().sum() < 60:
            return np.nan
        return max(s.autocorr(lag) for lag in range(21, 36))

    scores = {}
    for pid in women.index:
        s = valid.loc[valid[ID] == pid].set_index(DATE)["temp_dev"].dropna()
        s = s[~s.index.duplicated()]
        if len(s) >= 60:
            scores[pid] = cycle_score(s)
    scores = pd.Series(scores, dtype=float).dropna().sort_values(ascending=False)
    if len(scores):
        print("Cycle-likeness score (0 = none, >0.3 = clear repeating pattern):")
        print(scores.round(2).describe().to_string())
        show = scores.index[:4]
        fig, axes = plt.subplots(len(show), 1, figsize=(11, 2.2 * len(show)), sharex=False)
        for ax, pid in zip(np.atleast_1d(axes), show):
            s = valid.loc[valid[ID] == pid].set_index(DATE)["temp_dev"]
            ax.plot(s.index, s.values, color=SEX_COLORS["Female"], lw=0.8)
            ax.axhline(0, color="grey", lw=0.5)
            ax.set_title(f"Participant {pid}: cycle-likeness {scores[pid]:.2f}", fontsize=9)
            ax.set_ylabel("°C")
        plt.tight_layout()
        plt.show()
    else:
        print("Not enough continuous temperature data in this group for the check.")
else:
    print("No temperature column, so the cycle check is skipped.")

# %% [markdown]
# ## Step 13 · One person over time
#
# This is the view a clinician would see: one participant's measures day by
# day. Thin lines are the daily values, the thick line is the **7-day
# rolling average** (it smooths out noise), and red dots are the outlier
# days flagged in Step 9.
#
# Change `PERSON` to look at someone else. By default it's the person with
# the most data.

# %%
PERSON = days_per_person.idxmax()
timeline_metrics = [c for c in ["total_sleep_h", "night_rest_h", "rest_mid_6pm", "rem_pct", "deep_pct", "hrv",
                                 "resting_hr", "avg_hr", "resp_rate", "temp_dev", "steps", "spo2"] if c in cols]

p = clean[clean[ID] == PERSON].set_index(DATE)
if DAY_IS_NUMBER:  # fill in missing study days as blanks so gaps show and the 7-day window is really 7 days
    p = p.reindex(range(int(p.index.min()), int(p.index.max()) + 1))
fig, axes = plt.subplots(len(timeline_metrics), 1, figsize=(11, 1.9 * len(timeline_metrics)), sharex=True)
for ax, col in zip(np.atleast_1d(axes), timeline_metrics):
    ax.plot(p.index, p[col], lw=0.6, color="#90a4ae")
    ax.plot(p.index, p[col].rolling(7 if DAY_IS_NUMBER else "7D", min_periods=3).mean(), lw=2, color="#283593")
    out = p[p[f"{col}_outlier"].eq(True)]
    ax.scatter(out.index, out[col], color="red", s=8, zorder=3)
    ax.set_ylabel(METRICS[col], fontsize=8)
np.atleast_1d(axes)[-1].set_xlabel("study day" if DAY_IS_NUMBER else "date")
fig.suptitle(f"Participant {PERSON}: daily values (grey) and 7-day average (blue)")
plt.tight_layout()
plt.show()

# %% [markdown]
# ## Step 14 · How stable is each person from night to night?
#
# Two people can have the same *average* sleep while one is steady and the
# other swings between 3 h and 10 h. **Night-to-night variability** (the
# standard deviation of each person's own values) is often more useful
# clinically than the average. Irregular sleep is a known feature of
# hepatic encephalopathy.

# %%
var_metrics = [c for c in ["total_sleep_h", "night_rest_h", "rest_mid_6pm", "rem_pct", "hrv", "resting_hr", "avg_hr"] if c in cols]
person_sd = valid.groupby(ID)[var_metrics].std()
person_sd["sex"] = people["sex"]
fig, axes = plt.subplots(1, len(var_metrics), figsize=(3.3 * len(var_metrics), 3.2))
for ax, col in zip(np.atleast_1d(axes), var_metrics):
    for s in ["Female", "Male"]:
        ax.hist(person_sd.loc[person_sd["sex"] == s, col].dropna(), bins=20, alpha=0.6, color=SEX_COLORS[s], label=s)
    ax.set_title(f"Night-to-night SD\n{METRICS[col]}", fontsize=9)
np.atleast_1d(axes)[0].legend()
plt.tight_layout()
plt.show()
person_sd.groupby("sex")[var_metrics].median().round(2).rename(columns=METRICS)

# %% [markdown]
# ## Step 15 · Weekly rhythm: weekdays vs. weekends
#
# Do people sleep longer or move less on weekends? Clear weekly patterns mean
# a dashboard should compare like-with-like (this Monday vs. last Monday)
# rather than raw day-to-day changes.
#
# **Needs a weekday column (`day_type`).** Calendar dates are never loaded,
# so without that column this step just says so.

# %%
week_metrics = [c for c in ["total_sleep_h", "night_rest_h", "steps", "hrv"] if c in cols]
order = None
if DAY_IS_NUMBER and "day_type" in valid and valid["day_type"].notna().any():  # no dates, but the file says weekday vs weekend
    valid["weekday"] = valid["day_type"].astype(str)
    order = ["Weekday", "Weekend"]
elif not DAY_IS_NUMBER:
    valid["weekday"] = valid[DATE].dt.day_name().str[:3]
    order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
if order is None or not week_metrics:
    print("No weekday/weekend information in this file (no day_type column, no dates), so this step is skipped.")
else:
    fig, axes = plt.subplots(1, len(week_metrics), figsize=(4 * len(week_metrics), 3.2))
    for ax, col in zip(np.atleast_1d(axes), week_metrics):
        for s in ["Female", "Male"]:
            m = valid[valid["sex_clean"] == s].groupby("weekday")[col].median().reindex(order)
            ax.plot(order, m.values, marker="o", color=SEX_COLORS[s], label=s)
        ax.set_title(METRICS[col], fontsize=10)
    np.atleast_1d(axes)[0].legend()
    plt.tight_layout()
    plt.show()

# %% [markdown]
# ## Step 16 · Bedtime and circadian rhythm (timezone fix)
#
# Oura export **timestamps are in GMT/UTC**. Midnight GMT is 7 or 8 pm in
# New York, depending on daylight saving time, so we have to convert to
# local time (here **America/New_York**) before looking at bedtimes. If we
# didn't, a 10 pm bedtime would look like 2–3 am.
#
# Bedtime is a **circadian** marker. Late, drifting bedtimes may be a sign
# of covert HE.
#
# **The loader already did this:**
# `bedtime_start_h18` is the bedtime in **hours after 18:00 local** on the
# evening before waking (5 = 23:00, 7 = 01:00), with no date. We use it
# directly (Step 22 looks at it in more detail).
#
# **If there's no bedtime column at all**, we use
# the **rest onset** from Step 8b instead. The activity string is already in
# local time (Oura's day starts at 4 am local), so no timezone fix is needed.

# %%
TIMEZONE = "America/New_York"
BS = ROLES["bedtime_start"]
if BS and BS.endswith("_h18"):
    bed = pd.to_numeric(clean[BS], errors="coerce")
    clean["bedtime_hrs_after_6pm"] = bed.where(clean["type"].astype(str).eq("long_sleep")) if "type" in clean else bed
    BEDTIME_SOURCE = "Oura sleep record, long_sleep nights, clock time"
elif BS:
    ts = pd.to_datetime(df[BS], errors="coerce", utc=True)
    local = ts.dt.tz_convert(TIMEZONE)
    # hours after 6 pm, so 11 pm = 5 and 1 am = 7 (keeps nights continuous)
    clean["bedtime_hrs_after_6pm"] = ((local.dt.hour + local.dt.minute / 60) - 18) % 24
    clean["bedtime_local"] = local
    BEDTIME_SOURCE = "bedtime timestamp, New York time"
elif "rest_onset_6pm" in clean:
    clean["bedtime_hrs_after_6pm"] = clean["rest_onset_6pm"]
    BEDTIME_SOURCE = "rest onset from the activity string (proxy)"
if "bedtime_hrs_after_6pm" in clean:
    b = clean.loc[clean["is_valid"]]
    fig, ax = plt.subplots(figsize=(8, 3.3))
    for s in ["Female", "Male"]:
        ax.hist(b.loc[b["sex_clean"] == s, "bedtime_hrs_after_6pm"].dropna(), bins=48, range=(0, 18),
                alpha=0.6, color=SEX_COLORS[s], label=s)
    ticks = np.arange(0, 19, 2)
    ax.set_xticks(ticks, [f"{(18 + t) % 24:02d}:00" for t in ticks])
    ax.set(title=f"Bedtime ({BEDTIME_SOURCE})", xlabel="time of night", ylabel="nights")
    ax.legend()
    plt.show()
    per_person_bedtime_sd = b.groupby(ID)["bedtime_hrs_after_6pm"].std()
    print(f"Typical person's bedtime varies by ± {per_person_bedtime_sd.median():.1f} h night to night")
else:
    print("No bedtime timestamp or activity string found. Check the sleep files from the Oura export.")

# %% [markdown]
# ## Step 17 · Reading the 5-minute sleep-stage string (hypnogram)
#
# Oura stores each night's stages as one long string of digits, **one digit
# per 5 minutes**, e.g. `"4422211233..."`. Here we decode one night into a
# staircase chart (a **hypnogram**).
#
# ⚠️ **Check the digit code.** Oura's standard export uses
# 1 = deep, 2 = light, 3 = REM, 4 = awake, but other exports may differ.
# The **sanity check** at the end recomputes REM % from the string and
# compares it with the nightly REM % column. If they disagree a lot, the
# code is wrong, so fix `HYPNOGRAM_CODES`. The coding is still listed as
# "to confirm with the study team".
#
# A night is filed on its **wake day**: the row for study day *d* holds the
# night that ended on the morning of day *d*.

# %%
HYPNOGRAM_CODES = {"1": "Deep", "2": "Light", "3": "REM", "4": "Awake"}
STAGE_ORDER = ["Deep", "Light", "REM", "Awake"]

HYP = ROLES["hypnogram"]
if HYP and df[HYP].astype(str).str.fullmatch(r"\d{12,}").mean() > 0.3:
    nights = clean[clean[HYP].astype(str).str.fullmatch(r"\d{12,}")]
    example = nights.loc[nights[ID] == PERSON].iloc[len(nights.loc[nights[ID] == PERSON]) // 2] \
        if (nights[ID] == PERSON).any() else nights.iloc[0]
    stages = [HYPNOGRAM_CODES.get(ch, "?") for ch in str(example[HYP])]
    y = [STAGE_ORDER.index(s) if s in STAGE_ORDER else np.nan for s in stages]
    hours = np.arange(len(y)) * 5 / 60
    fig, ax = plt.subplots(figsize=(10, 2.6))
    ax.step(hours, y, where="post", color="#4527a0")
    ax.set_yticks(range(len(STAGE_ORDER)), STAGE_ORDER)
    when = f"day {int(example[DATE])}" if DAY_IS_NUMBER else f"{example[DATE]:%Y-%m-%d}"
    ax.set(title=f"Hypnogram: participant {example[ID]}, night ending on {when}", xlabel="hours since bedtime")
    plt.show()

    def rem_pct_from_string(s: str) -> float:
        asleep = [HYPNOGRAM_CODES.get(ch) for ch in s if HYPNOGRAM_CODES.get(ch) in {"Deep", "Light", "REM"}]
        return 100 * asleep.count("REM") / len(asleep) if asleep else np.nan

    if "rem_pct" in nights:
        check = nights[[HYP, "rem_pct"]].dropna().head(500).copy()
        check["rem_pct_from_string"] = check[HYP].astype(str).map(rem_pct_from_string)
        diff = (check["rem_pct_from_string"] - check["rem_pct"]).abs().median()
        print(f"Sanity check: median gap between string-based and column REM% = {diff:.1f} points "
              f"({'✅ code looks right' if diff < 5 else '❌ code is probably wrong, so edit HYPNOGRAM_CODES'})")
else:
    print("No 5-minute sleep-stage string column found in the loaded tables "
          "(it may be in a separate large sleep file; see Step 2).")

# %% [markdown]
# ## Step 18 · Which measures move together? (correlation)
#
# A **correlation** runs from −1 to +1. +1 means "when one goes up, the other
# goes up", −1 means "one goes up, the other goes down", and 0 means no
# relationship. We use **Spearman** correlation, which is robust to
# outliers, on each person's average.
#
# Measures that are highly correlated (above 0.8) carry the same
# information, so **a simpler dashboard only needs one of them**.
# Correlation is not causation.

# %%
corr = person_avg[cols].corr(method="spearman")
fig, ax = plt.subplots(figsize=(0.65 * len(cols) + 2, 0.55 * len(cols) + 1.5))
im = ax.imshow(corr, cmap="RdBu_r", vmin=-1, vmax=1)
labels = [METRICS[c] for c in cols]
ax.set_xticks(range(len(cols)), labels, rotation=60, ha="right", fontsize=8)
ax.set_yticks(range(len(cols)), labels, fontsize=8)
for i in range(len(cols)):
    for j in range(len(cols)):
        if not np.isnan(corr.iat[i, j]):
            ax.text(j, i, f"{corr.iat[i, j]:.1f}", ha="center", va="center", fontsize=7,
                    color="white" if abs(corr.iat[i, j]) > 0.6 else "black")
plt.colorbar(im, ax=ax, shrink=0.8)
ax.set_title("Spearman correlation between people's averages")
plt.tight_layout()
plt.show()

pairs = corr.where(np.triu(np.ones(corr.shape, dtype=bool), k=1)).stack().sort_values(key=abs, ascending=False)
print("Strongest pairs:\n", pairs.head(8).round(2).rename(lambda t: t).to_string())

# %% [markdown]
# ## Step 19 · Clinical groups: HE, ascites, disease severity
#
# A key clinical question: **what are the differences between patients with
# encephalopathy, or with a history of ascites?** This block finds any yes/no
# clinical column (HE, ascites, hospitalization, medications) and compares
# people's average sleep between the two groups, the same way we compared
# men and women.
#
# For number-type severity scores (MELD, Child-Pugh) we show correlations with
# sleep instead.

# %%
clinical_roles = ["he", "overt_he", "covert_he", "he_meds", "ctp_group", "decomp", "ascites", "variceal",
                  "hospitalized", "lactulose", "rifaximin", "alcohol", "opioids", "sleep_apnea"]
person_clin = df.groupby(ID).first()
for role in clinical_roles:
    c = ROLES[role]
    if not c:
        continue
    vals = person_clin[c].dropna().unique()
    if len(vals) != 2:
        print(f"{ROLE_PATTERNS[role][0]} ({c}): {len(vals)} distinct values, so it's not a simple yes/no. Look at it by hand.")
        continue
    a, b = sorted(vals, key=str)
    tbl = person_avg.join(person_clin[c])
    print(f"\n── {ROLE_PATTERNS[role][0]} ({c}): {a} vs {b} ──")
    display(compare_two_groups(tbl, c, a, b, [m for m in ["total_sleep_h", "night_rest_h", "rest_mid_6pm", "rem_pct", "deep_pct", "efficiency_pct",
                                                    "latency_min", "hrv", "resting_hr", "avg_hr", "resp_rate", "steps"] if m in cols]))

for role in ["meld", "child_pugh", "ammonia", "phes", "stroop", "animal_naming", "psqi", "pro", "frailty", "age"]:
    c = ROLES[role]
    if c and pd.api.types.is_numeric_dtype(df[c]):
        score = pd.to_numeric(df.groupby(ID)[c].mean(), errors="coerce")
        r = person_avg[cols].corrwith(score, method="spearman").sort_values(key=abs, ascending=False)
        print(f"\n── Spearman correlation of {ROLE_PATTERNS[role][0]} ({c}) with sleep/vitals ──")
        print(r.round(2).rename(METRICS).head(8).to_string())

if not any(ROLES[r] for r in clinical_roles + ["meld", "child_pugh"]):
    print("No clinical columns found in the loaded tables. They may be in a separate clinical file; check Step 2.")

# %% [markdown]
# # Real sleep from the Oura sleep record
#
# Everything above works on any version of the file. From here on we use the
# **Oura sleep record**: one record per day,
# filed on the **wake day** (the row for study day *d* is the night that ended
# on the morning of day *d*).
#
# What we still **don't** have: dates of HE episodes, admissions, lab changes
# or medication changes. All clinical columns are **from enrollment only**, so
# nothing here can say a sleep change happened "before" or "after" an event.
#
# ## Step 20 · One row per night: the sleep measures
#
# **Which nights count ("main nights").** Oura labels each record with a
# `type`: `long_sleep` (a normal night), `sleep` (a short sleep), `late_nap`
# or `rest`. Following the study statistician, a **main night** is a
# `long_sleep` record with 3–16 h in bed, 1–14 h asleep, and a hypnogram
# whose length matches the time in bed. Every measure below is computed **on
# main nights only**. Any other day is **missing, never zero**, and nothing
# is filled in. Sleep is **not** limited to `valid80` days: the ring's sleep
# record does not depend on daytime wear.
#
# | Measure | How it's computed | Unit |
# |---|---|---|
# | Total sleep | `total_sleep_duration` (seconds → hours) | h |
# | Time in bed | `time_in_bed_s` (real elapsed time, made by the R step) | h |
# | Sleep efficiency | total sleep ÷ time in bed | % |
# | Sleep latency | `latency`, only when ≤ 4 h (Oura's latency is often unreliable) | min |
# | Awake after sleep onset (WASO) | awake 5-minute slots in the hypnogram, **not** counting the awake run at the start (latency) or at the end | min |
# | Deep / light / REM | share of the asleep hypnogram slots (codes 1 / 2 / 3), and that share × total sleep | % and min |
# | Bedtime, wake time | `bedtime_start_h18`, `bedtime_end_h18`: **hours after 18:00** the evening before waking (5 = 23:00, 9 = 03:00, 14 = 08:00). No dates. | h after 18:00 |
# | Sleep midpoint | bedtime + time in bed ÷ 2 | h after 18:00 |
# | Restlessness | `restless_periods` ÷ hours in bed | per h |
#
# The 18:00 scale keeps nights continuous across midnight (no jump from 23:59
# to 00:00), so medians and SDs of clock times are meaningful. `clock18()`
# turns them back into clock times for reading.

# %%
SLEEP_COLS = ["type", "sleep_phase_5_min", "num_sleep_phase_5_min", "time_in_bed_s", "total_sleep_duration",
              "awake_time", "latency", "bedtime_start_h18", "bedtime_end_h18"]
HAVE_SLEEP = DAY_IS_NUMBER and all(c in df for c in SLEEP_COLS)
MAIN_TYPE = "long_sleep"


def clock18(h) -> str:
    """Hours after 18:00 → a clock time, e.g. 9.5 → 03:30."""
    if h is None or pd.isna(h):
        return "–"
    m = int(round((18 + float(h)) * 60)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def nightly_sleep(d: pd.DataFrame) -> pd.DataFrame:
    """Per-night sleep measures, the statistician's definitions; NaN on days without a main night."""
    def num(c):
        return pd.to_numeric(d[c], errors="coerce")

    raw = d["sleep_phase_5_min"]
    s = raw.where(raw.astype(str).str.fullmatch(r"[1-4]+"))

    def count(pattern):
        return pd.to_numeric(s.str.count(pattern), errors="coerce")

    tib = num("time_in_bed_s") / 3600
    tst = num("total_sleep_duration") / 3600
    n = pd.to_numeric(s.str.len(), errors="coerce")
    ok_hyp = (n - num("num_sleep_phase_5_min")).abs().le(0) & (n - tib * 12).abs().le(2)
    main = d["type"].astype(str).eq(MAIN_TYPE) & tib.between(3, 16) & tst.between(1, 14) & ok_hyp
    asleep = count("[123]")
    out = pd.DataFrame({ID: d[ID], DATE: d[DATE]}, index=d.index)
    out["record_type"] = d["type"].astype(object).where(d["type"].notna(), "no record")
    out["main_sleep"] = main
    out["tst_h"] = tst.where(main)
    out["tib_h"] = tib.where(main)
    out["efficiency"] = (100 * num("total_sleep_duration") / num("time_in_bed_s")).where(main)
    out["latency_min"] = (num("latency") / 60).where(main & num("latency").le(4 * 3600))
    inner = s.str.replace(r"^4+|4+$", "", regex=True)
    out["waso_min"] = (pd.to_numeric(inner.str.count("4"), errors="coerce") * 5).where(main)
    out["awake_min"] = (num("awake_time") / 60).where(main)
    for code, name in [("1", "deep"), ("2", "light"), ("3", "rem")]:
        share = count(code) / asleep.where(asleep > 0)
        out[f"{name}_pct"] = (100 * share).where(main)
        out[f"{name}_min"] = (share * tst * 60).where(main)
    out["bed_h18"] = num("bedtime_start_h18").where(main)
    out["wake_h18"] = num("bedtime_end_h18").where(main)
    out["mid_h18"] = (num("bedtime_start_h18") + tib / 2).where(main)
    if "restless_periods" in d:
        out["restless_per_h"] = (num("restless_periods") / tib).where(main)
    return out


SLEEP_LABELS = {
    "tst_h": "Total sleep (h)", "tib_h": "Time in bed (h)", "efficiency": "Sleep efficiency (%)",
    "latency_min": "Sleep latency (min)", "waso_min": "Awake after sleep onset (min)",
    "deep_pct": "Deep sleep (% of sleep)", "light_pct": "Light sleep (% of sleep)", "rem_pct": "REM sleep (% of sleep)",
    "deep_min": "Deep sleep (min)", "rem_min": "REM sleep (min)", "bed_h18": "Bedtime (h after 18:00)",
    "wake_h18": "Wake time (h after 18:00)", "mid_h18": "Sleep midpoint (h after 18:00)",
    "restless_per_h": "Restless periods per hour", "bed_sd_14_min": "Bedtime variability, last 14 days (SD, min)",
    "mid_shift_h": "Midpoint shift from the night before (h)",
}

if HAVE_SLEEP:
    nights = nightly_sleep(df)
    # Night-to-night regularity, only between consecutive main nights
    prev_day_ok = nights.groupby(ID)[DATE].shift(1).eq(nights[DATE] - 1)
    nights["mid_shift_h"] = (nights["mid_h18"] - nights.groupby(ID)["mid_h18"].shift(1)).abs().where(prev_day_ok)
    # Bedtime variability as on the patient page: SD of bedtime over the last 14 study days, at least 7 main nights
    parts = []
    for _, g in nights.groupby(ID, sort=False):
        s = g.set_index(DATE)["bed_h18"]
        full = s.reindex(range(int(s.index.min()), int(s.index.max()) + 1))
        parts.append(pd.Series(full.rolling(14, min_periods=7).std().reindex(s.index).to_numpy(), index=g.index))
    nights["bed_sd_14_min"] = 60 * pd.concat(parts)
    main_nights = nights[nights["main_sleep"]]
    print(f"Main nights: {len(main_nights):,} of {len(nights):,} person-days ({nights['main_sleep'].mean():.0%})")
    show = [c for c in SLEEP_LABELS if c in nights]
    table = main_nights[show].describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9]).T.round(2)
    for c in ["bed_h18", "wake_h18", "mid_h18"]:
        print(f"{SLEEP_LABELS[c].split(' (')[0]}: median {clock18(main_nights[c].median())}, "
              f"middle 50% {clock18(main_nights[c].quantile(0.25))}–{clock18(main_nights[c].quantile(0.75))}")
    display(table.rename(index=SLEEP_LABELS))
else:
    print("No Oura sleep record in this file (older export), so Steps 20–24 are skipped.")

# %% [markdown]
# ## Step 21 · Sleep data quality: record types, missing nights, nights per person
#
# Before comparing anyone we check **how much sleep data there is**:
# * **Record types.** What share of days have a `long_sleep` record, a short
#   `sleep`, a `late_nap`/`rest`, or **no record** at all. The file holds
#   **one record per day**, so daytime naps on a day that also has a main
#   night are not in it: **napping can't be measured from this file**. The
#   share of days whose only record is *not* a main night is the closest
#   fragmentation signal it supports.
# * **Nights per person.** Comparisons later need **at least 14 main
#   nights** per person.
# * **Sleep vs. `valid80`.** Sleep records also exist on days that fail the
#   daytime-wear rule, which is why sleep isn't limited to valid days.

# %%
if HAVE_SLEEP:
    shares = nights["record_type"].value_counts(normalize=True).mul(100).round(1)
    per_person = nights.groupby(ID).agg(days=(DATE, "size"), main=("main_sleep", "sum"),
                                        records=("record_type", lambda t: int((t != "no record").sum())))
    per_person["% days with no sleep record"] = 100 * (1 - per_person["records"] / per_person["days"])
    per_person["% records not a main night"] = 100 * (per_person["records"] - per_person["main"]) / per_person["records"].where(per_person["records"] > 0)

    fig, axes = plt.subplots(1, 3, figsize=(13, 3.4))
    axes[0].barh(shares.index.astype(str), shares.values, color="#5c6bc0")
    axes[0].set(title="Sleep record type (% of person-days)", xlabel="%")
    axes[1].hist(per_person["main"], bins=30, color="#26a69a")
    axes[1].axvline(14, color="black", ls="--", lw=1, label="14 nights (minimum for comparisons)")
    axes[1].set(title="Main nights per person", xlabel="main nights", ylabel="people")
    axes[1].legend(fontsize=8)
    axes[2].hist(per_person["% days with no sleep record"], bins=20, range=(0, 100), color="#ef6c00")
    axes[2].set(title="Days with no sleep record, per person", xlabel="% of that person's days", ylabel="people")
    plt.tight_layout()
    plt.show()

    has_record = nights["record_type"].ne("no record")
    print(f"People with no main night at all: {(per_person['main'] == 0).sum()}; "
          f"with fewer than 14: {(per_person['main'] < 14).sum()} of {len(per_person)}")
    print(f"Main nights per person: median {per_person['main'].median():.0f}, "
          f"10th percentile {per_person['main'].quantile(0.1):.0f}")
    print(f"Sleep records on days that fail valid80: {int((has_record & ~df['is_valid']).sum()):,}; "
          f"valid80 days with no sleep record: {int((~has_record & df['is_valid']).sum()):,}")
    print(f"People whose records are more than 20% non-main: {(per_person['% records not a main night'] > 20).sum()}")
    display(shares.rename("% of person-days").to_frame())

# %% [markdown]
# ## Step 22 · Sleep timing and regularity
#
# In cirrhosis and HE the most reproducible sleep signal is **later and more
# irregular sleep timing**. Three views:
# * **When** people go to bed, wake and sleep (bedtime, wake time, midpoint),
#   all on the 18:00 scale and labelled as clock times.
# * **Bedtime variability**: the SD of bedtime over the last 14 study days
#   (at least 7 main nights), the same rule the patient page uses.
# * **Midpoint shift**: how far the midpoint moves from one night to the
#   next (only between consecutive main nights).
#
# The **activity-based SRI** (Step 26) stays as the 24-hour regularity
# measure: the hypnogram only covers time in bed, so it can't give a 24-hour
# index.

# %%
if HAVE_SLEEP:
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.3), sharey=True)
    for ax, (c, color) in zip(axes, [("bed_h18", "#3949ab"), ("mid_h18", "#00897b"), ("wake_h18", "#f9a825")]):
        ax.hist(main_nights[c].dropna(), bins=60, range=(-2, 22), color=color)
        ax.axvline(main_nights[c].median(), color="black", ls="--", lw=1)
        ticks = np.arange(-2, 23, 4)
        ax.set_xticks(ticks, [clock18(t) for t in ticks])
        ax.set(title=f"{SLEEP_LABELS[c].split(' (')[0]}: median {clock18(main_nights[c].median())}", xlabel="clock time")
    axes[0].set_ylabel("main nights")
    plt.tight_layout()
    plt.show()

    regularity = main_nights.groupby(ID).agg(midpoint_sd_h=("mid_h18", "std"), bedtime_sd_14_min=("bed_sd_14_min", "median"),
                                              midpoint_shift_h=("mid_shift_h", "median"), nights=("mid_h18", "count"))
    regularity = regularity[regularity["nights"] >= 14]
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.2))
    for ax, (c, title) in zip(axes, [("midpoint_sd_h", "Midpoint SD over the study (h)"),
                                      ("bedtime_sd_14_min", "Typical 14-day bedtime SD (min)"),
                                      ("midpoint_shift_h", "Typical night-to-night midpoint shift (h)")]):
        ax.hist(regularity[c].dropna(), bins=25, color="#8e24aa")
        ax.axvline(regularity[c].median(), color="black", ls="--", lw=1)
        ax.set(title=title, ylabel="people")
    plt.tight_layout()
    plt.show()
    print(f"{len(regularity)} people with ≥ 14 main nights. Median midpoint SD {regularity['midpoint_sd_h'].median():.1f} h; "
          f"median 14-day bedtime SD {regularity['bedtime_sd_14_min'].median():.0f} min.")

# %% [markdown]
# ## Step 23 · Compensated vs. decompensated, HE vs. no HE
#
# **Same recipe as Step 11:** one number per person (the **median over their
# main nights**), only people with **at least 14 main nights**, then a
# **Mann-Whitney** test and the **rank-biserial r** (−1 to 1; about 0.1 small,
# 0.3 medium, 0.5 large). The "direction" column says which group tends
# higher. p-values are shown as "< 0.001" rather than rounded to 0.
#
# > ⚠️ **Exploratory and unadjusted.** Many group × measure combinations
# > are compared here, so about 1 in 20 comparisons with no real difference
# > will still show p < 0.05. The groups also differ in age, sex,
# > beta-blocker use (which lowers heart rate and changes HRV), site, arm and
# > liver-disease cause. **These are not causal differences.** Step 24
# > sketches how to adjust for some of them.

# %%
MIN_MAIN_NIGHTS = 14
COMPARE_SLEEP = ["tst_h", "efficiency", "waso_min", "latency_min", "deep_pct", "light_pct", "rem_pct",
                 "bed_h18", "mid_h18", "wake_h18", "bed_sd_14_min", "mid_shift_h", "restless_per_h"]
CAVEAT = ("Exploratory and unadjusted: many comparisons, about 1 in 20 null ones reach p < 0.05; groups differ in "
          "age, sex, beta-blocker use, site, arm and etiology. Not causal.")


def show_value(col: str, v):
    return clock18(v) if col.endswith("_h18") else (round(float(v), 2) if pd.notna(v) else np.nan)


def compare_groups(table: pd.DataFrame, group: str, a: str, b: str, labels: dict) -> pd.DataFrame:
    """Per-person values, group a vs b: n, medians, Mann-Whitney p and rank-biserial r with its direction."""
    rows = []
    for col, label in labels.items():
        if col not in table:
            continue
        x, y = table.loc[table[group].eq(a), col].dropna(), table.loc[table[group].eq(b), col].dropna()
        row = {"measure": label, f"n {a}": len(x), f"n {b}": len(y),
               f"median {a}": show_value(col, x.median()), f"median {b}": show_value(col, y.median())}
        if HAVE_SCIPY and len(x) >= 3 and len(y) >= 3:
            u, p = stats.mannwhitneyu(x, y, alternative="two-sided")
            r = 1 - 2 * u / (len(x) * len(y))  # > 0 when group a tends lower
            row.update({"p": fmt_p(p), "p (raw)": p, "rank-biserial r": round(r, 2),
                        "direction": "no difference" if r == 0 else f"{b if r > 0 else a} tend higher"})
        rows.append(row)
    return pd.DataFrame(rows)


person_clin = df.groupby(ID).first()
GROUPINGS = {}  # name → (per-person label Series, first level, second level)
if "decomp" in person_clin:
    GROUPINGS["Decompensation"] = (pd.to_numeric(person_clin["decomp"], errors="coerce").map({0: "Compensated", 1: "Decompensated"}),
                                   "Compensated", "Decompensated")
if "anyhe" in person_clin:
    GROUPINGS["Hepatic encephalopathy (any, at enrollment)"] = (
        pd.to_numeric(person_clin["anyhe"], errors="coerce").map({0: "No HE", 1: "HE"}), "No HE", "HE")
elif "previous_encephalopathy" in person_clin:
    GROUPINGS["Previous overt HE"] = (person_clin["previous_encephalopathy"].astype(str).str.strip().str.title()
                                      .map({"No": "No HE", "Yes": "HE"}), "No HE", "HE")

group_results = {}
if HAVE_SLEEP:
    person_sleep = main_nights.groupby(ID)[[c for c in COMPARE_SLEEP if c in main_nights]].median()
    person_sleep["main nights"] = main_nights.groupby(ID).size()
    person_sleep = person_sleep[person_sleep["main nights"] >= MIN_MAIN_NIGHTS]
    print(f"{len(person_sleep)} people with ≥ {MIN_MAIN_NIGHTS} main nights enter the comparisons.\n{CAVEAT}")
    labels = {c: SLEEP_LABELS[c] for c in COMPARE_SLEEP if c in person_sleep}
    for name, (grp, a, b) in GROUPINGS.items():
        tbl = person_sleep.join(grp.rename("group"))
        if tbl["group"].nunique() < 2:
            print(f"{name}: only one group present, skipped.")
            continue
        res = compare_groups(tbl, "group", a, b, labels)
        group_results[name] = res
        print(f"\n── {name}: {a} vs {b} (per-person medians) ──")
        display(res.drop(columns="p (raw)", errors="ignore"))
    if group_results:
        pd.concat(group_results, names=["grouping"]).to_csv(OUTPUT_DIR / "sleep_group_comparisons.csv")

    if "Decompensation" in group_results:
        grp, a, b = GROUPINGS["Decompensation"]
        tbl = person_sleep.join(grp.rename("group"))
        strip = [c for c in ["tst_h", "efficiency", "waso_min", "mid_h18"] if c in tbl]
        fig, axes = plt.subplots(1, len(strip), figsize=(3.4 * len(strip), 3.4))
        for ax, c in zip(np.atleast_1d(axes), strip):
            vals = [tbl.loc[tbl["group"] == lvl, c].dropna() for lvl in (a, b)]
            ax.boxplot(vals, showfliers=False, widths=0.5)
            for i, v in enumerate(vals, start=1):
                ax.scatter(np.random.default_rng(i).normal(i, 0.06, len(v)), v, s=10, alpha=0.6,
                           color=["#2563eb", "#d97706"][i - 1])
            ax.set_xticks([1, 2], [a, b], fontsize=8)
            ax.set_title(SLEEP_LABELS[c], fontsize=9)
            if c.endswith("_h18"):
                ticks = ax.get_yticks()
                ax.set_yticks(ticks, [clock18(t) for t in ticks])
        fig.suptitle("Each dot = one person's median over their main nights (exploratory, unadjusted)", y=1.02)
        plt.tight_layout()
        plt.show()

# %% [markdown]
# ## Step 24 · Nights within people: a mixed model (adjusted, still exploratory)
#
# Step 23 squeezes each person to one number. A **mixed model** keeps every
# night and says "nights belong to people": each person gets their own
# baseline (a **random intercept**), and the group difference is estimated
# **adjusted** for other baseline factors. In formula form:
#
# ```
# total sleep (h)  ~  decompensated + age + sex + beta-blocker + site + arm  + (1 | person)
# ```
#
# Read the **decompensated** coefficient as "average difference in hours of
# sleep per night between decompensated and compensated people of the same
# age, sex, beta-blocker use, site and arm". It is still **not causal**:
# disease cause, alcohol, medications and many other things are not in the
# model, and the 95% interval matters more than the p-value.
#
# This needs **`statsmodels`**, which isn't in the project's requirements (to
# keep JupyterLite and the Docker image small). If it isn't installed, the
# cell just says so: `pip install statsmodels` to run it locally.
#
# Ideas for later (statistician's list): trajectories and change points (the
# trajectories notebook), clustering into phenotypes, concordance between the
# hypnogram and the activity-based rest proxy, and whether missed nights
# relate to disease severity.

# %%
import warnings

try:
    import statsmodels.formula.api as smf
    HAVE_STATSMODELS = True
except ImportError:
    HAVE_STATSMODELS = False
    print("statsmodels is not installed, so the mixed model is skipped (pip install statsmodels to run it).")

if HAVE_SLEEP and HAVE_STATSMODELS and "Decompensation" in GROUPINGS:
    covariates = {"age": ROLES["age"], "sex": "sex_clean", "beta_blocker": "bb_use_t0", "site": "whichsite", "arm": "whicharm"}
    base = pd.DataFrame({"group": GROUPINGS["Decompensation"][0].eq("Decompensated").astype(float)
                         .where(GROUPINGS["Decompensation"][0].notna())})
    for name, col in covariates.items():
        if col and col in df:
            base[name] = df.groupby(ID)[col].first()
    if "age" in base:
        base["age"] = pd.to_numeric(base["age"], errors="coerce")
    keep = [c for c in base if c in ("group", "age") or base[c].nunique() >= 2]
    base = base[keep]
    eligible = person_sleep.index  # people with ≥ 14 main nights
    model_rows = []
    for outcome in ["tst_h", "efficiency", "mid_h18", "waso_min"]:
        long = main_nights.loc[main_nights[ID].isin(eligible), [ID, outcome]].join(base, on=ID).dropna()
        long = long.rename(columns={ID: "person", outcome: "y"})
        terms = ["group"] + [("C(" + c + ")" if c != "age" else c) for c in base if c != "group"]
        if long["person"].nunique() < 10 or long["group"].nunique() < 2:
            continue
        with warnings.catch_warnings():  # convergence is reported in the table instead
            warnings.simplefilter("ignore")
            fit = smf.mixedlm("y ~ " + " + ".join(terms), long, groups=long["person"]).fit(reml=True)
        lo, hi = fit.conf_int().loc["group"]
        model_rows.append({"outcome": SLEEP_LABELS[outcome], "people": long["person"].nunique(), "nights": len(long),
                           "decompensated − compensated": round(fit.params["group"], 2),
                           "95% CI": f"{lo:.2f} to {hi:.2f}", "p": fmt_p(fit.pvalues["group"]),
                           "adjusted for": ", ".join(c for c in base if c != "group") or "nothing",
                           "converged": "yes" if fit.converged else "no: treat with extra caution"})
    print("Mixed models (random intercept per person), exploratory:")
    display(pd.DataFrame(model_rows))

# %% [markdown]
# # Research views
#
# Detailed views for one person at a time (`PERSON`, set in Step 13: change
# it to look at someone else). The dashboard's patient page shows simpler
# versions of the actogram and 24-hour profile.
#
# ## Step 25 · Actogram, 24-hour activity profile and intraday MET
#
# * **Actogram**: one row per study day, one column per 5 minutes, coloured
#   by Oura's activity class (grey = ring off, dark blue = rest, light blue =
#   inactive, orange = active). Black lines mark the **main night** from the
#   sleep record. A shift of the dark band to the right over weeks is a later
#   sleep schedule.
# * **24-hour activity profile**: this person's typical **MET** (metabolic
#   equivalent: 1 = resting, 3+ = moderate activity) at each time of day
#   (median and middle 50% of their valid days), next to the cohort median.
# * **Intraday MET**: three consecutive days, minute by minute (5-minute
#   means), with main nights shaded.
#
# MET comes from `met_5min` (288 five-minute means per day, made by the R
# step). Oura's activity day starts at **04:00**.

# %%
from matplotlib.colors import ListedColormap

ACT_CMAP = ListedColormap(ACTIVITY_COLORS)
ACT_CMAP.set_bad("white")
x_ticks = np.arange(DAY_STARTS_AT, DAY_STARTS_AT + 25, 4)
x_labels = [f"{t % 24:02d}:00" for t in x_ticks]

g = df[df[ID] == PERSON].set_index(DATE)
g_nights = nights[nights[ID] == PERSON].set_index(DATE) if HAVE_SLEEP else None
person_days = list(range(int(g.index.min()), int(g.index.max()) + 1))

if "class_5_min" in g:
    cls = g["class_5_min"].reindex(person_days)
    mat = np.full((len(person_days), SLOTS), np.nan)
    for i, s in enumerate(cls):
        if isinstance(s, str) and re.fullmatch(rf"[0-5]{{{SLOTS}}}", s):
            mat[i] = np.frombuffer(s.encode(), dtype=np.uint8) - 48
    fig, ax = plt.subplots(figsize=(10, max(3, 0.045 * len(person_days) + 1.2)))
    ax.imshow(mat, aspect="auto", cmap=ACT_CMAP, vmin=-0.5, vmax=5.5, interpolation="nearest",
              extent=[DAY_STARTS_AT, DAY_STARTS_AT + 24, person_days[-1] + 0.5, person_days[0] - 0.5])
    if g_nights is not None:
        end_x = DAY_STARTS_AT + 24
        for d, row in g_nights[g_nights["main_sleep"]].iterrows():
            start, stop = 18 + row["bed_h18"], 18 + row["wake_h18"]  # hours after midnight of day d - 1
            if start < end_x:
                ax.hlines(d - 1, max(start, DAY_STARTS_AT), min(stop, end_x), color="black", lw=0.8)
            if stop > end_x:
                ax.hlines(d, max(start, end_x) - 24, stop - 24, color="black", lw=0.8)
    ax.set_xticks(x_ticks, x_labels)
    ax.set(xlabel="time of day (row = one study day, 04:00 → 04:00)", ylabel="study day",
           title=f"Actogram: participant {PERSON} (black = main night from the sleep record)")
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in ACTIVITY_COLORS]
    ax.legend(handles, list(ACTIVITY_CODES.values()), loc="upper left", bbox_to_anchor=(1, 1), fontsize=8)
    plt.tight_layout()
    plt.show()

if "met_5min" in df:
    hours = DAY_STARTS_AT + np.arange(SLOTS) * 5 / 60
    met_person = he_data._met_rows(g.loc[g["is_valid"], "met_5min"])
    met_cohort = he_data._met_rows(df.loc[df["is_valid"], "met_5min"])
    if np.isfinite(met_person).any():
        fig, ax = plt.subplots(figsize=(10, 3.2))
        q = np.nanpercentile(met_person, [25, 50, 75], axis=0)
        ax.fill_between(hours, q[0], q[2], color="#ff9800", alpha=0.25, label=f"{PERSON}: middle 50% of days")
        ax.plot(hours, q[1], color="#e65100", lw=1.5, label=f"{PERSON}: median")
        ax.plot(hours, np.nanmedian(met_cohort, axis=0), color="#455a64", lw=1, ls="--", label="cohort median")
        ax.set_xticks(x_ticks, x_labels)
        ax.set(xlim=(hours[0], hours[-1]), ylabel="MET", title="24-hour activity profile (valid days)")
        ax.legend(fontsize=8)
        plt.tight_layout()
        plt.show()

    WINDOW_DAYS = 3
    have_met = [d for d in person_days if d in g.index and isinstance(g.at[d, "met_5min"], str)]
    if len(have_met) >= WINDOW_DAYS:
        start_day = have_met[len(have_met) // 2]  # middle of this person's data; change to look elsewhere
        window = list(range(start_day, start_day + WINDOW_DAYS))
        met = he_data._met_rows(g["met_5min"].reindex(window)).ravel()
        x = np.arange(len(met)) * 5 / 60  # hours since 04:00 on the first day
        fig, ax = plt.subplots(figsize=(11, 3))
        ax.plot(x, met, color="#e65100", lw=0.7)
        if g_nights is not None:
            for d in range(start_day + 1, start_day + WINDOW_DAYS + 1):  # nights that end inside the window
                if d in g_nights.index and g_nights.at[d, "main_sleep"]:
                    off = (d - 1 - start_day) * 24 + 18 - DAY_STARTS_AT
                    ax.axvspan(off + g_nights.at[d, "bed_h18"], off + g_nights.at[d, "wake_h18"], color="#3949ab", alpha=0.15)
        ticks = np.arange(0, WINDOW_DAYS * 24 + 1, 6)
        ax.set_xticks(ticks, [f"{(t + DAY_STARTS_AT) % 24:02d}:00" for t in ticks])
        ax.set(xlim=(0, WINDOW_DAYS * 24), ylabel="MET",
               title=f"Intraday MET: participant {PERSON}, study days {window[0]}–{window[-1]} (blue = main night)")
        plt.tight_layout()
        plt.show()
else:
    print("No met_5min column (older cache), so the MET views are skipped.")

# %% [markdown]
# ## Step 26 · HRV, heart rate, SpO₂ and rest regularity (SRI) over time
#
# On the patient page these sit under "More measures". HRV,
# average heart rate and SpO₂ are Oura's **nightly** values (they're missing
# when there's no sleep record; SpO₂ is missing on most nights). **Rest
# regularity (SRI)** compares each pair of consecutive days slot by slot
# (rest vs. not rest at the same clock time): 100 = identical days, 0 =
# random. It uses the dashboard's own function (`_sri` in data.py).
#
# For one person: daily values (dots), 7-day rolling median (line) and their
# usual middle 50% (band). Then per-person medians by decompensation, with
# the same caveats as Step 23. **Beta-blockers lower heart rate and change
# HRV**, so heart-rate comparisons are especially confounded.

# %%
act_ok = df["class_5_min"].where(df["class_5_min"].astype(str).str.fullmatch(rf"[0-5]{{{SLOTS}}}")) \
    if "class_5_min" in df else pd.Series(np.nan, index=df.index)
next_ok = df.groupby(ID)[DATE].shift(-1).eq(df[DATE] + 1)
df["sri"] = [he_data._sri(t, n) for t, n in zip(act_ok, act_ok.groupby(df[ID]).shift(-1).where(next_ok))]
VITALS = {"average_hrv": "HRV (ms)", "average_heart_rate": "Average HR (bpm)", "spo2": "SpO₂ (%)",
          "sri": "Rest regularity (SRI)"}
vitals = df[[ID, DATE]].copy()
for c in VITALS:
    if c in df:
        v = pd.to_numeric(df[c], errors="coerce")
        vitals[c] = v.where(v > 0) if c == "average_heart_rate" else v
VITALS = {c: l for c, l in VITALS.items() if c in vitals and vitals[c].notna().any()}

p = vitals[vitals[ID] == PERSON].set_index(DATE).reindex(person_days)
fig, axes = plt.subplots(len(VITALS), 1, figsize=(11, 1.9 * len(VITALS)), sharex=True)
for ax, (c, label) in zip(np.atleast_1d(axes), VITALS.items()):
    q1, q3 = p[c].quantile([0.25, 0.75])
    ax.axhspan(q1, q3, color="#cfd8dc", alpha=0.5, lw=0)
    ax.scatter(p.index, p[c], s=5, color="#90a4ae")
    ax.plot(p.index, p[c].rolling(7, min_periods=3).median(), color="#283593", lw=1.8)
    ax.set_ylabel(label, fontsize=8)
np.atleast_1d(axes)[-1].set_xlabel("study day")
fig.suptitle(f"Participant {PERSON}: daily values, 7-day median and usual middle 50%")
plt.tight_layout()
plt.show()

person_vitals = vitals.groupby(ID)[list(VITALS)].median()
person_vitals = person_vitals[vitals.groupby(ID)[list(VITALS)].count().max(axis=1) >= MIN_MAIN_NIGHTS]
if "Decompensation" in GROUPINGS:
    grp, a, b = GROUPINGS["Decompensation"]
    print(CAVEAT)
    display(compare_groups(person_vitals.join(grp.rename("group")), "group", a, b, VITALS).drop(columns="p (raw)", errors="ignore"))

# %% [markdown]
# ## Step 27 · Workbench check: what we built vs. what we really have
#
# The current Oura Clinical Workbench shows many parameters (REM %, HRV,
# Ammonia, NCT-A, Lactulose compliance…), mostly filled with **synthetic**
# data. This block checks, for each one:
# * **found?**: is there a matching column?
# * **% of valid days with a value**
# * **% of people with any value**
#
# This produces `workbench_parameter_coverage.csv` (saved locally, outside the
# repo), which helps decide what stays in the simpler dashboard.

# %%
WORKBENCH_PARAMETERS = [
    # (workbench label, workbench tab(s), cleaned column or role)
    ("REM Sleep %", "Biometrics, Risk Lab", "rem_pct"),
    ("Deep Sleep %", "Biometrics, Risk Lab", "deep_pct"),
    ("Light Sleep %", "Risk Lab", "light_pct"),
    ("Sleep Latency", "Biometrics, Risk Lab", "latency_min"),
    ("Sleep Efficiency", "Dashboard, Biometrics, Risk Lab", "efficiency_pct"),
    ("Total Sleep Duration", "Biometrics, Risk Lab", "total_sleep_h"),
    ("Wake After Sleep Onset (WASO)", "Biometrics, Risk Lab", "awake_h"),
    ("REM Fragmentation", "Risk Lab", "hypnogram"),
    ("HRV (rMSSD / balance)", "Biometrics, Risk Lab", "hrv"),
    ("Nighttime HRV (2–6 am)", "Risk Lab", "hypnogram"),
    ("Body Temp Deviation", "Biometrics, Risk Lab", "temp_dev"),
    ("Resting Heart Rate", "Biometrics, Risk Lab", "resting_hr"),
    ("Respiratory Rate", "Biometrics, Risk Lab", "resp_rate"),
    ("SpO₂", "Dashboard, Biometrics", "spo2"),
    ("Daily Steps", "Biometrics", "steps"),
    ("Inactivity Alerts", "Feature registry", "inactivity"),
    ("Activity Score", "Risk Lab", "activity_score"),
    ("Ammonia (NH₃)", "Risk Lab", "ammonia"),
    ("Albumin", "Risk Lab", "albumin"),
    ("Bilirubin", "Risk Lab", "bilirubin"),
    ("INR", "Risk Lab", "inr"),
    ("MELD Score", "Risk Lab, Data Explorer", "meld"),
    ("Child-Pugh Score", "Risk Lab, Data Explorer", "child_pugh"),
    ("HE Grade / HE status", "Risk Lab", "he"),
    ("NCT-A / NCT-B time", "Risk Lab", "nct"),
    ("Lactulose Compliance", "Risk Lab", "lactulose"),
    ("Rifaximin Compliance", "Risk Lab", "rifaximin"),
    ("Confounder: sleep apnea", "Confounder flags", "sleep_apnea"),
    ("Confounder: alcohol", "Confounder flags", "alcohol"),
    ("Confounder: narcotics", "Confounder flags", "opioids"),
    ("Age", "Patient header", "age"),
    ("Sex", "Patient header", "sex"),
]

rows = []
for label, where, key in WORKBENCH_PARAMETERS:
    col = key if key in clean.columns else ROLES.get(key)
    if col and col in clean:
        on_valid = clean.loc[clean["is_valid"], col]
        rows.append((label, where, col, "yes", round(on_valid.notna().mean() * 100, 1),
                     round((clean.groupby(ID)[col].count() > 0).mean() * 100, 1)))
    else:
        rows.append((label, where, "—", "no", 0.0, 0.0))
coverage = pd.DataFrame(rows, columns=["workbench parameter", "where it appears", "column in this data",
                                       "found?", "% valid days with value", "% people with any value"])

# Things in this data that the workbench doesn't show yet
CLEANED_TO_ROLE = {"rem_pct": "rem", "deep_pct": "deep", "light_pct": "light", "latency_min": "latency",
                   "efficiency_pct": "efficiency", "total_sleep_h": "total_sleep", "awake_h": "awake"}
used = {ROLES.get(CLEANED_TO_ROLE.get(k, k)) for _, _, k in WORKBENCH_PARAMETERS} | {ID, DATE, VALID}
not_in_workbench = [c for c in df.columns if c not in used and c not in {"sex_clean", "is_valid"}]

coverage.to_csv(OUTPUT_DIR / "workbench_parameter_coverage.csv", index=False)
print(f"Columns in the {'synthetic' if SYNTHETIC else 'real'} data the workbench doesn't use yet:\n ", ", ".join(map(str, not_in_workbench)) or "none")
coverage

# %% [markdown]
# ## Step 28 · A one-page summary
#
# This prints the key numbers in plain English. It only contains **counts and
# averages**, no individual rows, so it can be shared within the study team.

# %%
def pct(x):
    return f"{x:.0f}%"

lines = [
    "- **SYNTHETIC DATA.** Generator artefacts, not clinical findings (docs/synthetic-data.md)."
] if SYNTHETIC else []
lines += [
    f"- **{df[ID].nunique()} participants**, {len(df):,} participant-days "
    + (f"(study days {df[DATE].min()}–{df[DATE].max()}, no calendar dates); " if DAY_IS_NUMBER
       else f"({df[DATE].min():%b %Y} – {df[DATE].max():%b %Y}); ")
    + f"median {days_per_person.median():.0f} days per person.",
    f"- Sex: " + ", ".join(f"{k} {v}" for k, v in sex_counts.items()) + ".",
]
if "age" in people:
    lines.append(f"- Age: median {people['age'].median():.0f} (range {people['age'].min():.0f}–{people['age'].max():.0f}).")
lines.append(f"- Valid days (≥80% data): {pct(df['is_valid'].mean() * 100)}.")
if HAVE_SLEEP:
    lines.append(f"- Oura sleep record: main nights on {nights['main_sleep'].mean():.0%} of person-days; "
                 f"median total sleep {main_nights['tst_h'].median():.1f} h, efficiency {main_nights['efficiency'].median():.0f}%, "
                 f"midpoint {clock18(main_nights['mid_h18'].median())} (Steps 20–22).")
    for name, res in group_results.items():
        if "p (raw)" in res:
            hits = res.loc[res["p (raw)"] < 0.05, "measure"].tolist()
            lines.append(f"- {name} (exploratory, unadjusted): p < 0.05 for {', '.join(hits) if hits else 'no sleep measure'}.")
    lines.append("- Still no dates of clinical events, labs or medication changes: clinical columns are from enrollment only.")
elif not ROLES["total_sleep"]:
    lines.append("- Sleep: no sleep-stage or sleep-duration columns in this file. A rest-based proxy from the "
                 "5-min activity string is used instead (see Step 8b).")
for col in [c for c in ["total_sleep_h", "night_rest_h", "rem_pct", "deep_pct", "hrv", "resting_hr", "avg_hr", "steps"] if c in cols]:
    lines.append(f"- {METRICS[col]}: cohort median {valid[col].median():.1f}.")
if "p-value" in sex_results:
    sig = sex_results.loc[sex_results["significant (strict)"] == "yes", "measure"].tolist()
    lines.append(f"- Men vs women (per-person averages, strict cut-off): "
                 f"{'differ on ' + ', '.join(sig) if sig else 'no clear differences'}.")
lines.append(f"- Menstrual-cycle columns: {', '.join(menstrual_cols) if menstrual_cols else 'none found'}.")
found = coverage[coverage['found?'] == 'yes']['workbench parameter'].tolist()
lines.append(f"- Workbench parameters present in {'synthetic' if SYNTHETIC else 'real'} data: {len(found)}/{len(coverage)}.")
summary_md = "\n".join(lines)
(OUTPUT_DIR / "summary_for_team.md").write_text(summary_md)
print(summary_md)

# %% [markdown]
# ## Glossary
#
# | Term | Plain meaning |
# |---|---|
# | **Participant-day** | One row: one person on one day. |
# | **Median** | The middle value when sorted. Less affected by extreme values than the mean. |
# | **IQR** | Interquartile range, the spread of the middle 50% of values. Used to flag outliers. |
# | **Outlier** | An unusually high or low value. It can be an error *or* a real clinical event. |
# | **NaN** | "Not a Number": pandas' word for a blank/missing value. |
# | **Rolling average** | Average of the last N days, recalculated each day, which smooths noise. |
# | **p-value** | How surprising the result would be if there were truly no difference. Below 0.05 is the usual cut-off. |
# | **Bonferroni** | Dividing 0.05 by the number of tests, to avoid false alarms when testing many things. |
# | **Effect size** | How *big* a difference is, separate from whether it's "significant". |
# | **Confounder** | A third factor (age, alcohol, sleep apnea) that can create a fake link between two others. |
# | **Spearman correlation** | How strongly two measures rise and fall together, from −1 to +1. |
# | **HRV (rMSSD)** | Heart rate variability, beat-to-beat variation. Higher usually means a healthier, more relaxed nervous system. |
# | **Temperature deviation** | Difference from the person's own normal skin temperature at night. |
# | **Hypnogram** | A staircase chart of sleep stages through the night. |
# | **UTC/GMT** | World reference time. New York is 4–5 hours behind. |
# | **valid_day** | Dataset flag: at least 80% of that day has usable ring data. |
# | **Main night** | A `long_sleep` record with 3–16 h in bed and 1–14 h asleep; the nights every sleep measure uses. |
# | **WASO** | Wake after sleep onset: minutes awake between falling asleep and the final awakening. |
# | **Sleep midpoint** | Halfway between bedtime and wake time; a marker of circadian timing. |
# | **h after 18:00** | Clock times stored as hours after 6 pm the evening before waking (9 = 03:00), so nights don't wrap at midnight. No dates. |
# | **Rank-biserial r** | Effect size for the Mann-Whitney test, from −1 to 1. |
# | **Mixed model** | A regression that keeps every night but knows which person it belongs to (each person gets their own baseline). |
# | **SRI** | Sleep (here: rest) Regularity Index: how alike consecutive days are, slot by slot. 100 = identical. |
# | **Actogram** | Days stacked as rows, time of day across, coloured by activity: shows the daily rhythm at a glance. |
# | **MET** | Metabolic equivalent: energy use relative to rest (1 = sitting quietly, 3+ = moderate activity). |
