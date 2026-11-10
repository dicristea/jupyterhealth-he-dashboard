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
# notebook explains why that isn't possible with the current file yet, then
# does the two analyses we *can* do now:
#
# 1. **Trajectories (population level).** How do groups of patients (for
#    example decompensated vs. compensated) look across their 180 study days?
# 2. **Phenotypes (individual level).** Do patients fall into a few
#    recognisable "types" based on their own Oura patterns, and do those types
#    line up with their clinical labels?
#
# It builds on `deid_oura_data_exploration`: same data file, same cleaning,
# same **night rest bout** sleep proxy.
#
# > 🔒 **Privacy:** the data stays on this computer. No participant IDs are
# > shown in charts. **Never commit an `.ipynb` with outputs.** Only this blank
# > `.py` version goes to GitHub.

# %% [markdown]
# ## Part 1 · Why "before and after clinical events" isn't possible yet
#
# **The goal:** line up every patient's resting HR, HRV, sleep and activity on
# the day of an event such as decompensation, an HE episode or a medication
# change, then compare the weeks before vs. after. That needs **when** each
# event happened.
#
# **What the de-identified study file has:**
#
# | Need | In the file? |
# |---|---|
# | Event **dates** (decompensation, HE episode, hospitalisation, med change) | ❌ No. `decomp`, `anyhe`, `anyOHE`, `ascites`, `he_meds`… are **one value per person**, measured at enrolment and copied onto every day. The check below confirms none of them ever changes within a person. |
# | A real calendar | ❌ No. Only `days_since_start` (study day 1–180). We don't know what day 1 is (enrolment? ring activation?). |
# | Sleep data (stages, total sleep, bedtime) | ❌ No. Only the 5-minute **activity** string, which we turn into a sleep *proxy*. Sleep regularity is a promising signal in cirrhosis, so this gap matters. |
# | Repeated labs (MELD, ammonia, sodium over time) | ❌ No. MELD and Child-Pugh are baseline only. |
# | Resting HR, temperature deviation | ❌ No. Only daily *average* HR. |
#
# **Where event data could come from:**
#
# 1. **A clinician-provided event table**, keyed by `name` + `days_since_start`,
#    with columns such as event type and study day. That keeps the data
#    de-identified (no calendar dates) and drops straight into Part 4.
# 2. **The EHR.** Admissions, paracentesis (ascites), variceal bleeds, HE
#    admissions, lactulose/rifaximin start and stop dates, and repeated MELD
#    labs all live there. A **SMART on FHIR** app launched from the patient's
#    chart only needs the **MRN** to match wearable data held outside the EHR
#    (for example in JupyterHealth Exchange). This needs the appropriate
#    ethics approval and authentication.
# 3. **More complete Oura data.** A full Oura export has nightly **sleep
#    periods** with bedtime start/end, the 5-minute sleep-stage string,
#    temperature deviation and lowest HR, plus raw HR/HRV files. Oura
#    timestamps are in UTC and must be converted to local time. The Oura API
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
# Same loader as the exploration notebook: R converts the `.rds` file to a CSV
# once (cached outside the repo), then pandas reads it. By default it uses the
# single `.rds` file in `../../de-identified data/`; set **`JH_DATA_FILE`** if
# your copy lives somewhere else.

# %%
import os
import re
import subprocess
from pathlib import Path

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

def default_data_file(folder: Path = Path("../../de-identified data")) -> Path:
    """The single .rds file in the data folder; set JH_DATA_FILE to choose one explicitly."""
    found = sorted(folder.glob("*.rds"))
    if len(found) != 1:
        raise FileNotFoundError(f"Expected exactly one .rds file in {folder.resolve()}, found {len(found)}. "
                                "Set JH_DATA_FILE to the study file.")
    return found[0]


DATA_FILE = Path(os.environ.get("JH_DATA_FILE") or default_data_file()).expanduser().resolve()
CACHE_DIR = DATA_FILE.parent.parent / "exploration_outputs" / "_cache"   # outside the repo
OUTPUT_DIR = CACHE_DIR.parent
print("Data file:", DATA_FILE, "| exists:", DATA_FILE.exists())

R_TO_CSV = r"""
args <- commandArgs(trailingOnly = TRUE)
x <- readRDS(args[1])
x <- x[, !vapply(x, is.list, logical(1)), drop = FALSE]
x[] <- lapply(x, function(v) if (inherits(v, "haven_labelled")) haven::as_factor(v) else v)
write.csv(x, args[2], row.names = FALSE, na = "")
"""


def load_rds(path: Path) -> pd.DataFrame:
    cache = CACHE_DIR / (path.stem + ".csv")
    if not cache.exists() or cache.stat().st_mtime < path.stat().st_mtime:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        script = CACHE_DIR / "rds_to_csv.R"
        script.write_text(R_TO_CSV)
        subprocess.run(["Rscript", str(script), str(path), str(cache)], check=True)
    return pd.read_csv(cache, low_memory=False, dtype={"class_5_min": str, "name": str})


df = load_rds(DATA_FILE).sort_values(["name", "days_since_start"]).reset_index(drop=True)
print(f"{df['name'].nunique()} participants, {len(df):,} participant-days")

# %% [markdown]
# ### Check: do the clinical columns ever change within a person?
#
# If a column had an event *date* built in, its value would switch partway
# through someone's timeline (e.g. `decomp` going 0 → 1). **0 people changing
# = baseline-only**, which confirms we can't place events in time.

# %%
CLINICAL_COLS = ["decomp", "anyhe", "anyOHE", "che_phes", "he_meds", "ever_he_meds", "ascites",
                 "medication_for_ascites", "variceal_bleeding_history", "ctp_class", "meld", "phes_score",
                 "psqiglobal", "cldq_total", "liver_frailty_index"]
changes = pd.DataFrame({
    "column": CLINICAL_COLS,
    "people with a value": [df.groupby("name")[c].apply(lambda s: s.notna().any()).sum() for c in CLINICAL_COLS],
    "people whose value changes over time": [(df.groupby("name")[c].nunique() > 1).sum() for c in CLINICAL_COLS],
})
changes

# %% [markdown]
# ### Build the daily measures
#
# * Only **valid days** (`valid80 == 1`, ≥80% wear) are used for measures;
#   night-rest measures require both days spanned by the overnight window to be valid.
# * An average HR of **0 bpm** is a recording error and is blanked out.
# * **Night rest bout:** the longest stretch of "rest" in the activity string
#   between 6 pm and noon the next day. Breaks of up to 10 minutes are allowed.
#   This is our sleep proxy (see the exploration notebook, Step 8b).
# * **Rest regularity (SRI)** is new, and the closest proxy for *sleep
#   regularity*. For each pair of consecutive days, we check every 5-minute
#   slot: was the person in the same state (resting vs. not resting) at the
#   same clock time both days? SRI runs from **100** (identical days) through
#   0 (random) to −100 (exactly flipped). It follows the published Sleep
#   Regularity Index, applied here to Oura's rest/not-rest classes.

# %%
SLOTS, DAY_STARTS_AT = 288, 4      # Oura activity day = 04:00 → 04:00 local, 5-min slots
strings = df["class_5_min"].where(df["class_5_min"].str.fullmatch(r"[0-5]{288}", na=False))
same_person_next = (df["name"].shift(-1) == df["name"]) & (df["days_since_start"].shift(-1) == df["days_since_start"] + 1)
next_string = strings.shift(-1).where(same_person_next)
valid_day = pd.to_numeric(df["valid80"], errors="coerce").eq(1)
night_pair_ok = same_person_next & valid_day & valid_day.shift(-1, fill_value=False)

win_start, win_end = (18 - DAY_STARTS_AT) * 12, SLOTS + (12 - DAY_STARTS_AT) * 12
bout_re = re.compile(r"1(?:[0-5]{0,2}1)*")


def night_rest(today, tomorrow):
    if pd.isna(today) or pd.isna(tomorrow):
        return (np.nan, np.nan)
    window = (today + tomorrow)[win_start:win_end]
    best = max(bout_re.finditer(window), key=lambda m: m.end() - m.start(), default=None)
    if best is None or best.end() - best.start() < 24:
        return (np.nan, np.nan)
    return ((best.end() - best.start()) * 5 / 60, (best.start() + best.end()) / 2 * 5 / 60)


def sri(today, tomorrow):
    if pd.isna(today) or pd.isna(tomorrow):
        return np.nan
    a = np.frombuffer(today.encode(), dtype=np.uint8) - 48
    b = np.frombuffer(tomorrow.encode(), dtype=np.uint8) - 48
    worn = (a != 0) & (b != 0)
    if worn.sum() < SLOTS * 0.8:
        return np.nan
    return 200 * ((a == 1) == (b == 1))[worn].mean() - 100


df[["night_rest_h", "rest_mid_6pm"]] = pd.DataFrame(
    [night_rest(t, n) for t, n in zip(strings.where(night_pair_ok), next_string.where(night_pair_ok))], index=df.index)
df["sri"] = [sri(t, n) for t, n in zip(strings, next_string)]
df["active_h"] = strings.str.count("[345]") * 5 / 60
df["average_heart_rate"] = df["average_heart_rate"].where(df["average_heart_rate"] > 0)
df.loc[~df["night_rest_h"].between(2, 16), "night_rest_h"] = np.nan

valid = df[df["valid80"] == 1].copy()

METRICS = {
    "night_rest_h": "Night rest bout (h)",
    "rest_mid_6pm": "Rest midpoint (h after 6 pm)",
    "sri": "Rest regularity (SRI)",
    "average_hrv": "HRV (ms)",
    "average_heart_rate": "Average HR (bpm)",
    "steps": "Steps / day",
}

# Baseline groups worth comparing (column, label for 0/No, label for 1/Yes)
GROUPS = {
    "decomp": ("Decompensated?", {0: "Compensated", 1: "Decompensated", "0": "Compensated", "1": "Decompensated"}),
    "che_phes": ("Covert HE by PHES?", {"No": "No covert HE", "Yes": "Covert HE"}),
    "ctp_cat": ("Child-Pugh group", {"Child A": "Child A", "Child B/C": "Child B/C"}),
    "anyOHE": ("Overt HE ever?", {0: "No overt HE", 1: "Overt HE", "0": "No overt HE", "1": "Overt HE"}),
    "he_meds": ("HE meds at enrolment?", {"No": "No HE meds", "Yes": "On HE meds"}),
}
for col, (_, mapping) in GROUPS.items():
    raw = valid[col].map(lambda v: int(v) if isinstance(v, float) and not np.isnan(v) else v)
    valid[f"grp_{col}"] = raw.map(mapping)

print(f"{len(valid):,} valid days. Median night rest {valid['night_rest_h'].median():.1f} h, "
      f"median SRI {valid['sri'].median():.0f}")

# %% [markdown]
# ## Part 3 · Trajectories: how do groups look over the 180 study days?
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
valid["week"] = (valid["days_since_start"] - 1) // 7 + 1
weekly = valid.groupby(["name", "week"])[list(METRICS)].mean().reset_index()
people = valid.groupby("name")[[f"grp_{c}" for c in GROUPS]].first()
weekly = weekly.join(people, on="name")
PALETTE = ["#2563eb", "#d97706"]


def plot_trajectories(group_col: str, metrics=("night_rest_h", "sri", "average_hrv", "average_heart_rate", "steps")):
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
for g in ["decomp", "che_phes", "ctp_cat"]:
    levels = list(dict.fromkeys(GROUPS[g][1].values()))
    for m in METRICS:
        a = slopes.loc[slopes[f"grp_{g}"] == levels[0], m].dropna()
        b = slopes.loc[slopes[f"grp_{g}"] == levels[1], m].dropna()
        p = stats.mannwhitneyu(a, b).pvalue if len(a) >= 3 and len(b) >= 3 else np.nan
        rows.append((GROUPS[g][0], METRICS[m], f"{a.median():+.2f}", f"{b.median():+.2f}", round(p, 3),
                     f"{levels[0]} vs {levels[1]}"))
pd.DataFrame(rows, columns=["grouping", "measure", "median slope group 1", "median slope group 2", "p-value", "groups"])

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
    s = sub.set_index("days_since_start")[col].reindex(range(1, 181))
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
for m in ["night_rest_h", "average_hrv", "average_heart_rate", "steps"]:
    res = valid.groupby("name").apply(biggest_shift, m, include_groups=False)
    for pid, (day, z) in res.items():
        shift_rows.append((pid, METRICS[m], day, z))
shifts = pd.DataFrame(shift_rows, columns=["name", "measure", "study_day", "z"]).dropna()

big = shifts[shifts["z"].abs() >= 1.5]
print(f"People with at least one large shift (|z| ≥ 1.5) in any measure: "
      f"{big['name'].nunique()} of {valid['name'].nunique()}")
print(big.groupby("measure")["z"].agg(["count", lambda z: (z < 0).sum()]).rename(
    columns={"count": "large shifts", "<lambda_0>": "of which drops"}).to_string())

# Save the candidate list locally (outside the repo) to compare with event dates later
big.sort_values(["name", "study_day"]).to_csv(OUTPUT_DIR / "candidate_change_points.csv", index=False)

fig, ax = plt.subplots(figsize=(9, 3))
for m, color in zip(big["measure"].unique(), ["#2563eb", "#d97706", "#16a34a", "#9333ea"]):
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
def pre_post_plot(events: pd.DataFrame, metrics=("night_rest_h", "average_hrv", "average_heart_rate", "steps"),
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
# **The numbers per person** (from valid days):
# * typical night rest (median) and how much it varies (SD)
# * typical rest midpoint (circadian timing) and its night-to-night spread
# * rest regularity (median SRI)
# * HRV, average HR and steps (medians)
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
features = valid.groupby("name").agg(
    rest_median=("night_rest_h", "median"),
    rest_sd=("night_rest_h", "std"),
    midpoint_median=("rest_mid_6pm", "median"),
    midpoint_sd=("rest_mid_6pm", "std"),
    sri_median=("sri", "median"),
    hrv_median=("average_hrv", "median"),
    hr_median=("average_heart_rate", "median"),
    steps_median=("steps", "median"),
    valid_days=("days_since_start", "count"),
)
features = features[features["valid_days"] >= 30].drop(columns="valid_days").dropna()
FEATURE_LABELS = {
    "rest_median": "Night rest (h)", "rest_sd": "Night rest variability", "midpoint_median": "Rest midpoint",
    "midpoint_sd": "Midpoint variability", "sri_median": "Rest regularity (SRI)", "hrv_median": "HRV",
    "hr_median": "Average HR", "steps_median": "Steps",
}
print(f"{len(features)} people with ≥30 valid days and all features")

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
# **PCA** squeezes the 8 numbers per person down to 2, so we can draw everyone
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
    label_rows.append({"label": positive, **pct.to_dict(), "p-value": round(p, 3)})
pd.DataFrame(label_rows)

# %% [markdown]
# ## Part 6 · Summary
#
# Counts and medians only, no individual rows. It's saved next to the
# exploration notebook's summary, outside the repo.

# %%
lines = [
    "**Trajectories & phenotypes: HE study, de-identified file**",
    f"- Clinical columns are baseline-only: {int((changes['people whose value changes over time'] == 0).sum())}"
    f"/{len(changes)} never change within a person, so pre/post-event plots need an event table "
    "(name + study day) from the study team or the EHR.",
    f"- Night rest bout median {valid['night_rest_h'].median():.1f} h; rest regularity (SRI) median "
    f"{valid['sri'].median():.0f}.",
    f"- Phenotypes: {K} types among {len(features)} people (silhouette {scores[K]:.2f}).",
]
for r in label_rows:
    if r["p-value"] < 0.05:
        lines.append(f"- {r['label']} differs across phenotypes (p = {r['p-value']}).")
lines.append(f"- Candidate change points (|z| ≥ 1.5): {big['name'].nunique()} people. "
             "List saved locally to compare with real event dates.")
summary = "\n".join(lines)
(OUTPUT_DIR / "summary_trajectories_phenotypes.md").write_text(summary)
print(summary)
