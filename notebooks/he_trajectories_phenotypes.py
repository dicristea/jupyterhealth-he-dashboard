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
# # HE Study: Trajectories and Phenotypes
#
# **What this notebook is for:** the original goal was to plot Oura measures
# **before and after clinical events** (for example decompensation). This
# notebook explains why that isn't possible yet (the dataset has nightly
# sleep but no event dates), then does the two analyses we *can* do now:
#
# 1. **Trajectories (population level).** How do groups of patients (for
#    example decompensated vs. compensated) look across their 180 study days?
# 2. **Phenotypes (individual level).** Do patients fall into a few
#    recognisable "types" based on their own Oura patterns, and do those types
#    line up with their clinical labels?
#
# It builds on the data exploration notebook: same data file, same loader,
# the same **main-night** sleep measures (its Step 20) and the activity-based
# **night rest bout** proxy for files without a sleep record.
#
# > 🔒 **Privacy:** the data stays on this computer. No participant IDs are
# > shown in charts. **Never commit an `.ipynb` with outputs.** Only this blank
# > `.py` version goes to GitHub.

# %% [markdown]
# ## Data source
#
# Same switch as the dashboard: set **`HE_DATA_SOURCE`** before starting
# Jupyter.
#
# * `auto` (default): the study file if `JH_DATA_FILE` is set or the
#   dashboard's data folder (`data.DEFAULT_DATA_DIR`, outside the repo) holds
#   exactly one `.rds`, otherwise the synthetic cohort.
# * `study`: the study file only.
# * `synthetic`: the synthetic cohort from the dashboard's `data.py`. No R, no
#   study file.
#
# > ⚠️ **On synthetic data, every result in this notebook is an artifact of
# > the generator's settings, not a clinical finding.** Group differences,
# > p-values and phenotypes show what the generator was told to build. See
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
import data as he_data

# In the password-protected study site's JupyterLite (in the browser), the data comes from the site itself.
HOSTED_LITE = sys.platform == "emscripten" and any((p / "hosted_study.json").exists() for p in (Path.cwd(), *Path.cwd().parents))
if HOSTED_LITE:
    DATA_SOURCE = "study"
elif DATA_SOURCE == "auto":
    found_rds = sorted(he_data.DEFAULT_DATA_DIR.glob("*.rds"))
    DATA_SOURCE = "study" if os.environ.get("JH_DATA_FILE") or len(found_rds) == 1 else "synthetic"
SYNTHETIC = DATA_SOURCE == "synthetic"
print("Data source:", DATA_SOURCE + (" (from the study site)" if HOSTED_LITE else ""))
if SYNTHETIC:
    print("⚠️  SYNTHETIC DATA. Every result below is an artifact of the generator settings "
          "(docs/synthetic-data.md), not a clinical finding.")

# %% [markdown]
# ## Part 1 · Why "before and after clinical events" isn't possible yet
#
# **The goal:** line up every patient's resting HR, HRV, sleep and activity on
# the day of an event such as decompensation, an HE episode or a medication
# change, then compare the weeks before vs. after. That needs **when** each
# event happened.
#
# **What the dataset has:**
#
# | Need | In the file? |
# |---|---|
# | Event **dates** (decompensation, HE episode, hospitalisation, med change) | ❌ No. `decomp`, `anyhe`, `anyOHE`, `ascites`, `he_meds`… are **one value per person**, measured at enrollment and copied onto every day. The check below confirms none of them ever changes within a person. |
# | A real calendar | ❌ No, by design (HIPAA Safe Harbor). Only `days_since_start` (study day 0–180). We don't know what day 0 is (enrollment? ring activation?). |
# | Sleep data (stages, total sleep, bedtime) | ✅ Yes: one Oura sleep record per night (total sleep, awake time, latency, REM, the 5-minute hypnogram, and bedtime/wake as **clock times only**). Used below. |
# | Repeated labs (MELD, bilirubin, albumin, INR over time) | ❌ No. Labs, MELD and Child-Pugh are at enrollment only. |
# | Resting HR, temperature deviation | ❌ No. Only daily *average* HR. |
#
# **Where event data could come from:**
#
# 1. **A clinician-provided event table**, keyed by `name` + `days_since_start`,
#    with columns such as event type and study day. That needs no calendar
#    dates and drops straight into Part 4.
# 2. **The EHR.** Admissions, paracentesis (ascites), variceal bleeds, HE
#    admissions, lactulose/rifaximin start and stop dates, and repeated MELD
#    labs all live there. A **SMART on FHIR** app launched from the patient's
#    chart only needs the **MRN** to match wearable data held outside the EHR
#    (for example in JupyterHealth Exchange). This needs the appropriate
#    ethics approval and authentication.
# 3. **More complete Oura data.** The dataset has the nightly sleep
#    record; a full Oura export would add temperature deviation, lowest HR,
#    raw HR/HRV files and every sleep period of the day (the file keeps one
#    per day, so daytime naps are missing). The Oura API
#    could also feed JupyterHealth Exchange directly, once minute-level and
#    daily heart-rate values are tagged apart (Open mHealth qualifiers).
#
# **What we can do meanwhile:** compare groups by their **baseline** labels
# over study time (Part 3), find people whose measures **shift suddenly**
# (candidate events to check against real dates later, Part 4), and look for
# **phenotypes** (Part 5).

# %% [markdown]
# ## Part 2 · Load and prepare the data
#
# Same loader as the dashboard and the exploration notebook (`data.py`): R
# converts the `.rds` file to a CSV once (cached outside the repo as
# `*.v3.csv`), keeping bedtimes as **clock times only** and refusing to write
# any calendar date. By default it uses the single `.rds` file in the
# dashboard's data folder; set **`JH_DATA_FILE`** (or `HE_DATA_FILE`) if your
# copy lives somewhere else.
#
# On synthetic data the table comes from the dashboard's `data.py` instead
# (`synthetic_data/synthetic_he.csv.gz` if you generated one, otherwise the
# default in-memory cohort), and files are saved to `notebook_outputs/` in the
# repo, which git ignores.

# %%
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from scipy import stats
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

pd.set_option("display.width", 160)
plt.rcParams.update({"figure.dpi": 110, "axes.spines.top": False, "axes.spines.right": False})

def fmt_p(p: float) -> str:
    """p-values for reading: never "p = 0"."""
    if p is None or pd.isna(p):
        return "n/a"
    return "< 0.001" if p < 0.001 else f"{p:.3f}" if p < 0.01 else f"{p:.2f}"


if SYNTHETIC or HOSTED_LITE:
    OUTPUT_DIR = REPO / "notebook_outputs"   # git-ignored, inside the repo (in JupyterLite: this browser only)
    OUTPUT_DIR.mkdir(exist_ok=True)
else:
    if os.environ.get("JH_DATA_FILE"):
        DATA_FILE = Path(os.environ["JH_DATA_FILE"])
    else:  # HE_DATA_FILE, or the single .rds in data.DEFAULT_DATA_DIR (data.py raises if there are none or several)
        DATA_FILE = he_data.DATA_FILE or he_data.default_data_file()
    DATA_FILE = DATA_FILE.expanduser().resolve()
    OUTPUT_DIR = DATA_FILE.parent.parent / "exploration_outputs"   # outside the repo
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("Data file:", DATA_FILE.name, "| exists:", DATA_FILE.exists())

if SYNTHETIC:
    raw, origin = he_data.synthetic_table()
    print("Synthetic cohort:", origin)
elif HOSTED_LITE:
    raw, origin = he_data.lite_study_table()
    print("Study data from", origin)
else:
    # The dashboard's loader: R converts once into a cache outside the repo, clock times only, no calendar dates
    raw = he_data._read_rds(DATA_FILE)
# -999 → missing, `ascites` from current/previous ascites, one row per person-day (the valid-wear copy wins)
df = he_data.harmonise_columns(raw.copy()).sort_values(["name", "days_since_start"], kind="stable").reset_index(drop=True)
del raw
DAY0 = he_data.first_day(df)  # the first study day (0 or 1, depending on the file)
print(f"{df['name'].nunique()} participants, {len(df):,} participant-days")

# %% [markdown]
# ### Check: do the clinical columns ever change within a person?
#
# If a column had an event *date* built in, its value would switch partway
# through someone's timeline (e.g. `decomp` going 0 → 1). **0 people changing
# = baseline-only**, which confirms we can't place events in time.

# %%
CLINICAL_COLS = ["decomp", "anyhe", "anyOHE", "che_phes", "he_meds", "ever_he_meds", "ascites", "current_ascites",
                 "previous_ascites", "previous_encephalopathy", "medication_for_ascites", "variceal_bleeding_history",
                 "ctp_class", "ctp_score", "meld", "phes_score", "total_bilirubin_t0", "albumin_t0", "inr_t0",
                 "bb_use_t0", "psqiglobal", "cldq_total", "liver_frailty_index"]
if absent := [c for c in CLINICAL_COLS if c not in df]:
    print("Not in this data, so not checked:", ", ".join(absent))
    CLINICAL_COLS = [c for c in CLINICAL_COLS if c in df]
changes = pd.DataFrame({
    "column": CLINICAL_COLS,
    "people with a value": [df.groupby("name")[c].apply(lambda s: s.notna().any()).sum() for c in CLINICAL_COLS],
    "people whose value changes over time": [(df.groupby("name")[c].nunique() > 1).sum() for c in CLINICAL_COLS],
})
changes

# %% [markdown]
# ### Build the daily measures
#
# **Activity measures** (from the 5-minute activity string, steps, HRV, HR):
# * Only **valid days** (`valid80 == 1`, ≥80% wear) count; night-rest measures
#   require both days spanned by the overnight window to be valid. Other days
#   are blanked, not dropped, so the sleep measures below keep them.
# * An average HR of **0 bpm** is a recording error and is blanked out.
# * **Night rest bout:** the longest stretch of "rest" in the activity string
#   between 6 pm and noon the next day. Breaks of up to 10 minutes are allowed.
#   This is the activity-based sleep *proxy* (exploration notebook, Step 8b).
# * **Rest regularity (SRI)**: for each pair of consecutive days, we check every 5-minute
#   slot: was the person in the same state (resting vs. not resting) at the
#   same clock time both days? SRI runs from **100** (identical days) through
#   0 (random) to −100 (exactly flipped). It follows the published Sleep
#   Regularity Index, applied here to Oura's rest/not-rest classes.
#
# Night rest and SRI use the dashboard's own functions (`_night_rest`, `_sri`
# in `data.py`), so the numbers match the app.
#
# **Sleep measures** (from the Oura sleep record): computed on
# **main nights** only (`long_sleep`, 3–16 h in bed, 1–14 h asleep, hypnogram
# length matching time in bed), the same rule as the exploration notebook's
# Step 20. Not limited to `valid80` days. Bedtime and midpoint are in **hours
# after 18:00** the evening before waking (9 = 03:00), so nights don't wrap at
# midnight. A night is filed on its **wake day**.

# %%
SLOTS = he_data.SLOTS
strings = df["class_5_min"].where(df["class_5_min"].astype(str).str.fullmatch(rf"[0-5]{{{SLOTS}}}"))
same_person_next = (df["name"].shift(-1) == df["name"]) & (df["days_since_start"].shift(-1) == df["days_since_start"] + 1)
next_string = strings.shift(-1).where(same_person_next)
valid_day = pd.to_numeric(df["valid80"], errors="coerce").eq(1)
night_pair_ok = same_person_next & valid_day & valid_day.shift(-1, fill_value=False)

df[["night_rest_h", "rest_mid_6pm"]] = pd.DataFrame(
    [he_data._night_rest(t, n) for t, n in zip(strings.where(night_pair_ok), next_string.where(night_pair_ok))],
    index=df.index)
df["sri"] = [he_data._sri(t, n) for t, n in zip(strings, next_string)]
df["active_h"] = strings.str.count("[345]") * 5 / 60
df["average_heart_rate"] = pd.to_numeric(df["average_heart_rate"], errors="coerce").where(lambda v: v > 0)
df.loc[~df["night_rest_h"].between(2, 16), "night_rest_h"] = np.nan

SLEEP_COLS = ["type", "sleep_phase_5_min", "num_sleep_phase_5_min", "time_in_bed_s", "total_sleep_duration",
              "bedtime_start_h18"]
HAVE_SLEEP = all(c in df for c in SLEEP_COLS)
if HAVE_SLEEP:
    num = lambda c: pd.to_numeric(df[c], errors="coerce")
    hyp = df["sleep_phase_5_min"].where(df["sleep_phase_5_min"].astype(str).str.fullmatch(r"[1-4]+"))
    tib, tst = num("time_in_bed_s") / 3600, num("total_sleep_duration") / 3600
    n_slots = pd.to_numeric(hyp.str.len(), errors="coerce")
    main = (df["type"].astype(str).eq("long_sleep") & tib.between(3, 16) & tst.between(1, 14)
            & (n_slots - num("num_sleep_phase_5_min")).abs().le(0) & (n_slots - tib * 12).abs().le(2))
    df["main_sleep"] = main
    df["tst_h"] = tst.where(main)
    df["efficiency"] = (100 * tst / tib).where(main)
    df["waso_min"] = (pd.to_numeric(hyp.str.replace(r"^4+|4+$", "", regex=True).str.count("4"), errors="coerce") * 5).where(main)
    df["mid_h18"] = (num("bedtime_start_h18") + tib / 2).where(main)
    print(f"Main nights: {int(main.sum()):,} of {len(df):,} person-days")
else:
    df["main_sleep"] = False
    print("No Oura sleep record in this file (older export): only the activity-based measures are used.")

ACTIVITY_METRICS = {
    "night_rest_h": "Night rest bout (h, proxy)",
    "rest_mid_6pm": "Rest midpoint (h after 6 pm, proxy)",
    "sri": "Rest regularity (SRI)",
    "average_hrv": "HRV (ms)",
    "average_heart_rate": "Average HR (bpm)",
    "steps": "Steps / day",
}
SLEEP_METRICS = {
    "tst_h": "Total sleep (h)",
    "efficiency": "Sleep efficiency (%)",
    "waso_min": "Awake after sleep onset (min)",
    "mid_h18": "Sleep midpoint (h after 18:00)",
} if HAVE_SLEEP else {}
METRICS = {**SLEEP_METRICS, **ACTIVITY_METRICS}

# One row per person-day: activity measures blanked on non-valid days, sleep measures blanked on non-main nights
valid = df.copy()
valid[list(ACTIVITY_METRICS) + ["active_h"]] = valid[list(ACTIVITY_METRICS) + ["active_h"]].where(valid_day, axis=0)

# Baseline groups worth comparing (column, label for 0/No, label for 1/Yes)
GROUPS = {
    "decomp": ("Decompensated?", {0: "Compensated", 1: "Decompensated", "0": "Compensated", "1": "Decompensated"}),
    "che_phes": ("Covert HE by PHES?", {"No": "No covert HE", "Yes": "Covert HE"}),
    "ctp_cat": ("Child-Pugh group", {"Child A": "Child A", "Child B/C": "Child B/C"}),
    "anyOHE": ("Overt HE ever?", {0: "No overt HE", 1: "Overt HE", "0": "No overt HE", "1": "Overt HE"}),
    "he_meds": ("HE meds at enrollment?", {"No": "No HE meds", "Yes": "On HE meds"}),
}
GROUPS = {c: g for c, g in GROUPS.items() if c in valid}
for col, (_, mapping) in GROUPS.items():
    raw = valid[col].map(lambda v: int(v) if isinstance(v, float) and not np.isnan(v) else v)
    valid[f"grp_{col}"] = raw.map(mapping)

print(f"{int(valid_day.sum()):,} valid days. Median night rest {valid['night_rest_h'].median():.1f} h, "
      f"median SRI {valid['sri'].median():.0f}"
      + (f"; median total sleep {valid['tst_h'].median():.1f} h on main nights" if HAVE_SLEEP else ""))

# %% [markdown]
# ## Part 3 · Trajectories: how do groups look over the 180 study days?
#
# Each grouping gets two rows of charts: **real sleep**
# (total sleep, efficiency, WASO, sleep midpoint; main nights) and the
# **activity-based** measures. A sleep midpoint of 9 means 03:00.
#
# **How to read these charts.** Each line is a group's **median** across
# people, one point per study week. The shaded band is the middle 50% of
# people (the IQR). We average each person's week first, so someone with lots
# of data doesn't count more than anyone else.
#
# * **Lines that stay apart all the way** mean the groups differ at baseline,
#   not that something changed over time.
# * **Lines that drift** may mean the group changes over the study. Check the
#   **number of people per week** (thin grey bars). Towards week 25 fewer
#   people remain, so the right end is less certain.
# * Because `days_since_start` isn't anchored to any clinical event, these
#   are **study-time** trajectories, not "before vs. after an event".

# %%
valid["week"] = (valid["days_since_start"] - DAY0) // 7 + 1
# Weekly mean per person for activity, weekly median for sleep (a night or two of naps-only shouldn't dominate)
weekly = valid.groupby(["name", "week"])[list(ACTIVITY_METRICS)].mean()
if SLEEP_METRICS:
    weekly = weekly.join(valid.groupby(["name", "week"])[list(SLEEP_METRICS)].median())
weekly = weekly.reset_index()
people = valid.groupby("name")[[f"grp_{c}" for c in GROUPS]].first()
weekly = weekly.join(people, on="name")
PALETTE = ["#2563eb", "#d97706"]


def plot_trajectories(group_col: str, metrics=("night_rest_h", "sri", "average_hrv", "average_heart_rate", "steps")):
    if group_col not in GROUPS:
        print(f"No {group_col} column in this data, so this grouping is skipped.")
        return
    title, mapping = GROUPS[group_col]
    levels = list(dict.fromkeys(mapping.values()))
    fig, axes = plt.subplots(1, len(metrics), figsize=(3.6 * len(metrics), 3.3), sharex=True)
    for ax, m in zip(axes, metrics):
        for color, level in zip(PALETTE, levels):
            g = weekly[weekly[f"grp_{group_col}"] == level].groupby("week")[m]
            q = g.quantile([0.25, 0.5, 0.75]).unstack()
            n = g.count()
            q = q[n >= 5]  # hide weeks with fewer than 5 people in the group
            ax.plot(q.index, q[0.5], color=color, lw=2, label=f"{level} (n={people[f'grp_{group_col}'].eq(level).sum()})")
            ax.fill_between(q.index, q[0.25], q[0.75], color=color, alpha=0.15)
        ax.set_title(METRICS[m], fontsize=10)
        ax.set_xlabel("study week")
    n_week = weekly[weekly[f"grp_{group_col}"].isin(levels)].groupby("week")["name"].nunique()
    twin = axes[0].twinx()
    twin.bar(n_week.index, n_week.values, color="#cbd5e1", alpha=0.35, width=0.8)
    twin.set_ylim(0, n_week.max() * 4)
    twin.set_yticks([])
    axes[0].legend(fontsize=8, loc="lower left")
    fig.suptitle(f"Trajectories by {title.lower().rstrip('?')} (median and middle 50% per study week)", y=1.02)
    plt.tight_layout()
    plt.show()


for g in ["decomp", "che_phes", "ctp_cat"]:
    if SLEEP_METRICS:
        plot_trajectories(g, metrics=tuple(SLEEP_METRICS))  # real sleep, main nights
    plot_trajectories(g)

# %% [markdown]
# ### Is anyone changing over time? (per-person trend)
#
# For each person and measure we fit a straight line through their valid
# days and keep the **slope per 30 days**. For example −0.3 h night rest per
# 30 days means "rest is getting 18 minutes shorter each month". Then we
# compare slopes between groups with the same Mann-Whitney test as the exploration notebook.
# Slopes near 0 for everyone mean the groups are **stable** over 6 months, so
# the differences in Part 3 are baseline differences.

# %%
def slope_per_30d(sub: pd.DataFrame, col: str) -> float:
    s = sub[["days_since_start", col]].dropna()
    if len(s) < 30 or s["days_since_start"].nunique() < 30:
        return np.nan
    return stats.linregress(s["days_since_start"], s[col]).slope * 30


slopes = pd.DataFrame({m: valid.groupby("name").apply(slope_per_30d, m, include_groups=False) for m in METRICS})
slopes = slopes.join(people)

rows = []
for g in [g for g in ["decomp", "che_phes", "ctp_cat"] if g in GROUPS]:
    levels = list(dict.fromkeys(GROUPS[g][1].values()))
    for m in METRICS:
        a = slopes.loc[slopes[f"grp_{g}"] == levels[0], m].dropna()
        b = slopes.loc[slopes[f"grp_{g}"] == levels[1], m].dropna()
        p = stats.mannwhitneyu(a, b).pvalue if len(a) >= 3 and len(b) >= 3 else np.nan
        rows.append((GROUPS[g][0], METRICS[m], f"{a.median():+.2f}", f"{b.median():+.2f}", fmt_p(p),
                     f"{levels[0]} vs {levels[1]}"))
print("Exploratory and unadjusted: many comparisons, so about 1 in 20 with no real difference reach p < 0.05.")
pd.DataFrame(rows, columns=["grouping", "measure", "median slope group 1", "median slope group 2", "p", "groups"])

# %% [markdown]
# ## Part 4 · Candidate events: sudden shifts within a person
#
# Without event dates, we can still ask the data **where something seems to
# change**. For each person and measure we slide a window along their
# timeline and compare the **14 days before** with the **14 days after** each
# day. The day with the biggest jump (in units of that person's own normal
# day-to-day variation) is their **candidate change point**.
#
# These are **not** clinical events. They're a list to hold up against real
# event dates once an event table (or the EHR) provides them: *"Did something happen
# to this patient around study day 87?"* When we get an event table, this is
# exactly where the pre/post plot goes. Use the `pre_post_plot()` function
# at the end of this part.

# %%
WINDOW = 14


def biggest_shift(sub: pd.DataFrame, col: str):
    s = sub.set_index("days_since_start")[col].reindex(range(DAY0, int(df["days_since_start"].max()) + 1))
    if s.notna().sum() < 3 * WINDOW:
        return (np.nan, np.nan)
    before = s.rolling(WINDOW, min_periods=WINDOW // 2).mean()
    after = s[::-1].rolling(WINDOW, min_periods=WINDOW // 2).mean()[::-1].shift(-1)
    z = (after - before) / s.std()
    if z.abs().max() != z.abs().max():  # all NaN
        return (np.nan, np.nan)
    day = z.abs().idxmax()
    return (day, z[day])


shift_rows = []
for m in [*(["tst_h", "mid_h18"] if HAVE_SLEEP else []), "night_rest_h", "average_hrv", "average_heart_rate", "steps"]:
    res = valid.groupby("name").apply(biggest_shift, m, include_groups=False)
    for pid, (day, z) in res.items():
        shift_rows.append((pid, METRICS[m], day, z))
shifts = pd.DataFrame(shift_rows, columns=["name", "measure", "study_day", "z"]).dropna()

big = shifts[shifts["z"].abs() >= 1.5]
print(f"People with at least one large shift (|z| ≥ 1.5) in any measure: "
      f"{big['name'].nunique()} of {valid['name'].nunique()}")
print(big.groupby("measure")["z"].agg(["count", lambda z: (z < 0).sum()]).rename(
    columns={"count": "large shifts", "<lambda_0>": "of which drops"}).to_string())

# Save the candidate list locally (outside the repo, or notebook_outputs/ on synthetic data) to compare with event dates later
big.sort_values(["name", "study_day"]).to_csv(OUTPUT_DIR / "candidate_change_points.csv", index=False)

if big.empty:
    print("No large shifts, so there is nothing to place on the study timeline."
          + (" Expected on synthetic data: the generator has no events or trends." if SYNTHETIC else ""))
else:
    fig, ax = plt.subplots(figsize=(9, 3))
    for m, color in zip(big["measure"].unique(), ["#2563eb", "#d97706", "#16a34a", "#9333ea", "#0891b2", "#be123c"]):
        ax.hist(big.loc[big["measure"] == m, "study_day"], bins=18, range=(0, 180), alpha=0.5, label=m, color=color)
    ax.set(title="When do the large shifts happen?", xlabel="study day", ylabel="number of shifts")
    ax.legend(fontsize=8)
    plt.show()


# %% [markdown]
# ### Pre/post plot, ready for real event dates
#
# `pre_post_plot(events)` takes a table with one row per event, with columns
# `name` and `study_day`. Today we feed it the **candidate** night-rest drops
# from above. When real events arrive (for example decompensation days),
# pass those instead and the chart becomes the pre/post-event view. Each thin
# line is one person, re-centred on their event (day 0). The thick line is the
# median of everyone.

# %%
def pre_post_plot(events: pd.DataFrame, metrics=(*(["tst_h"] if HAVE_SLEEP else []), "night_rest_h", "average_hrv",
                                                 "average_heart_rate", "steps"),
                  days: int = 28, title: str = "Around each event"):
    fig, axes = plt.subplots(1, len(metrics), figsize=(3.6 * len(metrics), 3.2), sharex=True)
    for ax, m in zip(axes, metrics):
        aligned = []
        for _, ev in events.iterrows():
            s = valid[valid["name"] == ev["name"]].set_index("days_since_start")[m]
            rel = s.reindex(range(int(ev["study_day"]) - days, int(ev["study_day"]) + days + 1))
            rel.index = range(-days, days + 1)
            rel = rel - rel.loc[-days:-1].mean()   # change vs. that person's pre-event average
            aligned.append(rel.rolling(7, min_periods=3, center=True).mean())
        if not aligned:
            continue
        mat = pd.concat(aligned, axis=1)
        for col in mat:
            ax.plot(mat.index, mat[col], color="#94a3b8", lw=0.5, alpha=0.5)
        ax.plot(mat.index, mat.median(axis=1), color="#1d4ed8", lw=2.5)
        ax.axvline(0, color="#dc2626", ls="--", lw=1)
        ax.axhline(0, color="black", lw=0.5)
        ax.set_title(f"Change in {METRICS[m]}", fontsize=9)
        ax.set_xlabel("days from event")
    fig.suptitle(f"{title} (n = {len(events)})", y=1.02)
    plt.tight_layout()
    plt.show()


drops = big[(big["measure"] == METRICS["night_rest_h"]) & (big["z"] < 0)]
if drops.empty:
    print("No candidate night-rest drops, so the pre/post plot is skipped.")
else:
    pre_post_plot(drops, title="Around candidate night-rest drops (NOT clinical events)")

# When real events arrive:
# events = pd.read_csv("path/to/event_table.csv")   # columns: name, study_day, event_type
# pre_post_plot(events[events.event_type == "decompensation"], title="Around decompensation")

# %% [markdown]
# ## Part 5 · Phenotypes: do patients fall into "types"?
#
# **Idea:** describe each person with a few numbers from their own Oura data,
# then let an algorithm group people who look alike. This is the
# *"individual phenotype identification"* approach.
#
# **The numbers per person:**
# * with the Oura sleep record, from **main nights**: typical
#   total sleep (median) and how much it varies (SD), sleep efficiency, and the
#   sleep midpoint (circadian timing) with its night-to-night spread. People
#   need at least **14 main nights**.
# * without it, the activity-based proxies instead: night rest
#   and rest midpoint, median and SD.
# * from valid days: rest regularity (median SRI), HRV, average HR and steps
#   (medians). People need at least **30 valid days**.
#
# **The algorithm** is **k-means**. It puts people into *k* groups so that
# people within a group are as similar as possible. We first put every
# number on the same scale (z-scores, via `StandardScaler`) so steps (in the
# thousands) don't drown out SRI. To choose *k*, we try 2 to 5 groups and keep
# the one with the best **silhouette score** (how cleanly separated the
# groups are; closer to 1 is better).
#
# ⚠️ Clusters always "work". The algorithm will split any data. What matters
# is whether the groups **make clinical sense** and **line up with the
# clinical labels**, which is the table at the end.

# %%
if HAVE_SLEEP:
    FEATURE_LABELS = {
        "sleep_median": "Total sleep (h)", "sleep_sd": "Total sleep variability", "efficiency_median": "Sleep efficiency",
        "midpoint_median": "Sleep midpoint", "midpoint_sd": "Midpoint variability",
    }
    features = valid.groupby("name").agg(
        sleep_median=("tst_h", "median"), sleep_sd=("tst_h", "std"), efficiency_median=("efficiency", "median"),
        midpoint_median=("mid_h18", "median"), midpoint_sd=("mid_h18", "std"), main_nights=("tst_h", "count"))
    enough_sleep = features["main_nights"] >= 14
else:
    FEATURE_LABELS = {
        "rest_median": "Night rest (h)", "rest_sd": "Night rest variability", "midpoint_median": "Rest midpoint",
        "midpoint_sd": "Midpoint variability",
    }
    features = valid.groupby("name").agg(
        rest_median=("night_rest_h", "median"), rest_sd=("night_rest_h", "std"),
        midpoint_median=("rest_mid_6pm", "median"), midpoint_sd=("rest_mid_6pm", "std"))
    enough_sleep = pd.Series(True, index=features.index)
FEATURE_LABELS.update({"sri_median": "Rest regularity (SRI)", "hrv_median": "HRV", "hr_median": "Average HR",
                       "steps_median": "Steps"})
features = features.join(valid.groupby("name").agg(
    sri_median=("sri", "median"), hrv_median=("average_hrv", "median"),
    hr_median=("average_heart_rate", "median"), steps_median=("steps", "median")))
valid_days_per_person = valid_day.groupby(df["name"]).sum()
features = features[enough_sleep & (valid_days_per_person.reindex(features.index) >= 30)]
features = features[list(FEATURE_LABELS)].dropna()
print(f"{len(features)} people with enough data and all {len(FEATURE_LABELS)} features")

X = StandardScaler().fit_transform(features)
scores = {k: silhouette_score(X, KMeans(k, n_init=20, random_state=0).fit_predict(X)) for k in range(2, 6)}
K = max(scores, key=scores.get)
print("Silhouette by k:", {k: round(v, 3) for k, v in scores.items()}, "→ using k =", K)

km = KMeans(K, n_init=20, random_state=0).fit(X)
features["phenotype"] = [f"Type {c + 1}" for c in km.labels_]

# %% [markdown]
# ### What does each phenotype look like?
#
# Each cell is the group's **average z-score**. **Blue** = lower than the
# cohort, **red** = higher, and 0 = typical. For example a type with blue
# *night rest*, red *night rest variability* and blue *SRI* is
# "short, irregular resters".

# %%
profile = pd.DataFrame(X, index=features.index, columns=list(FEATURE_LABELS)).groupby(features["phenotype"]).mean()
fig, ax = plt.subplots(figsize=(10, 0.6 * K + 1.6))
im = ax.imshow(profile.values, cmap="RdBu_r", vmin=-1.5, vmax=1.5, aspect="auto")
ax.set_xticks(range(len(FEATURE_LABELS)), FEATURE_LABELS.values(), rotation=30, ha="right", fontsize=9)
ax.set_yticks(range(K), [f"{t} (n={(features['phenotype'] == t).sum()})" for t in profile.index])
for i in range(K):
    for j in range(len(FEATURE_LABELS)):
        ax.text(j, i, f"{profile.iat[i, j]:+.1f}", ha="center", va="center", fontsize=8)
plt.colorbar(im, ax=ax, label="z-score vs. cohort")
ax.set_title("Phenotype profiles")
plt.tight_layout()
plt.show()

# Real units, easier to talk about with clinicians
features.groupby("phenotype")[list(FEATURE_LABELS)].median().round(1).rename(columns=FEATURE_LABELS)

# %% [markdown]
# ### A map of everyone
#
# **PCA** squeezes the numbers per person down to 2, so we can draw everyone
# on a flat map. People close together have similar Oura patterns. Colours are
# the phenotypes. If the colours form clear islands, the types are real
# separations. If they blur together, the types are more like regions of a
# continuum.

# %%
pca = PCA(2).fit(X)
xy = pca.transform(X)
fig, ax = plt.subplots(figsize=(6.5, 5))
for i, t in enumerate(sorted(features["phenotype"].unique())):
    sel = (features["phenotype"] == t).values
    ax.scatter(xy[sel, 0], xy[sel, 1], s=28, alpha=0.8, label=t, color=plt.cm.tab10(i))
ax.set(xlabel=f"PC1 ({pca.explained_variance_ratio_[0]:.0%} of variation)",
       ylabel=f"PC2 ({pca.explained_variance_ratio_[1]:.0%} of variation)", title="Each dot = one person")
ax.legend()
plt.show()

# %% [markdown]
# ### Do phenotypes line up with clinical labels?
#
# This is the clinically interesting part. For each label we show the
# **% of each phenotype** that has it, and a **Fisher / chi-square p-value**
# (could this split happen by chance?). With a small cohort and several
# labels, treat anything under 0.05 as **"worth a closer look"**, not as proof.

# %%
clin = valid.groupby("name").first()
label_rows = []
for col, (title, mapping) in GROUPS.items():
    lab = clin.loc[features.index, f"grp_{col}"]
    positive = list(dict.fromkeys(mapping.values()))[1]
    tab = pd.crosstab(features["phenotype"], lab == positive)
    if tab.shape[1] < 2:
        continue
    p = stats.fisher_exact(tab.values).pvalue if tab.shape == (2, 2) else stats.chi2_contingency(tab.values).pvalue
    pct = (tab[True] / tab.sum(axis=1) * 100).round(0).astype(int).astype(str) + "%"
    label_rows.append({"label": positive, **pct.to_dict(), "p": fmt_p(p), "p (raw)": p})
pd.DataFrame(label_rows).drop(columns="p (raw)", errors="ignore")

# %% [markdown]
# ## Part 6 · Summary
#
# Counts and medians only, no individual rows. It's saved next to the
# exploration notebook's summary, outside the repo (on synthetic data, in the
# git-ignored `notebook_outputs/`).

# %%
lines = [
    "**Trajectories & phenotypes: SYNTHETIC cohort. Generator artifacts, not clinical findings "
    "(docs/synthetic-data.md)**" if SYNTHETIC else "**Trajectories & phenotypes: HE study data**",
    f"- Clinical columns are baseline-only: {int((changes['people whose value changes over time'] == 0).sum())}"
    f"/{len(changes)} never change within a person, so pre/post-event plots need an event table "
    "(name + study day) from the study team or the EHR.",
    f"- Night rest bout median {valid['night_rest_h'].median():.1f} h; rest regularity (SRI) median "
    f"{valid['sri'].median():.0f}."
    + (f" Oura sleep record: median total sleep {valid['tst_h'].median():.1f} h on main nights." if HAVE_SLEEP else ""),
    f"- Phenotypes: {K} types among {len(features)} people (silhouette {scores[K]:.2f}).",
]
for r in label_rows:
    if r["p (raw)"] < 0.05:
        lines.append(f"- {r['label']} differs across phenotypes (p {r['p'] if r['p'].startswith('<') else '= ' + r['p']}; exploratory).")
lines.append(f"- Candidate change points (|z| ≥ 1.5): {big['name'].nunique()} people. "
             "List saved locally to compare with real event dates.")
summary = "\n".join(lines)
(OUTPUT_DIR / "summary_trajectories_phenotypes.md").write_text(summary)
print(summary)
