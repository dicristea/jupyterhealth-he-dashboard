# Synthetic data

The public version of this dashboard runs only on synthetic data. This page describes the data format the dashboard expects, how the synthetic cohort is generated, which relationships are built into it, and how to change it or swap in your own dataset.

> **Findings on synthetic data are artefacts of the generator's assumptions, not clinical results.** If decompensated patients show shorter night rest in the Cohort tab, it's because the generator was told to make them rest less (see [Built-in relationships](#built-in-relationships)). A p-value computed on this data says nothing about patients.

## Quick start

```bash
python scripts/generate_synthetic_data.py --participants 40 --min-days 90 --max-days 180 --seed 7
HE_DATA_SOURCE=synthetic python app.py
```

This writes `synthetic_data/synthetic_he.csv.gz` and `synthetic_data/synthetic_he.settings.json` (the participant count, day range, seed and every setting used, so the file can be regenerated exactly). `synthetic_data/` is git-ignored.

Without any data file the app also builds the default cohort (24 participants, seed 7) in memory.

| Option | Default | Meaning |
|---|---|---|
| `--participants` | 24 | Number of participants (`DEMO-01`, `DEMO-02`, …) |
| `--min-days`, `--max-days` | 90, 180 | Each participant gets a follow-up length drawn uniformly from this range (inclusive) |
| `--seed` | 7 | Random seed. Same seed + same settings → identical file |
| `--config` | none | JSON file overriding any of the settings below |
| `--print-config` | | Print the full settings as JSON and exit (a starting point for your own `--config`) |
| `--output` | `synthetic_data/synthetic_he.csv.gz` | `.csv` or `.csv.gz` |

From Python: `data.synthetic_cohort(n, seed, min_days, max_days, config={...})` returns the same table as a DataFrame.

## Format

One row per **participant-day**, in the format of the study's de-identified Oura export (after the app's R → CSV conversion). There are no calendar dates: days are numbered from enrolment. Clinical fields are **baseline only**: the same value is repeated on every row of a participant.

### Required columns

The app needs these columns to run at all.

| Column | Type | Meaning | Valid values |
|---|---|---|---|
| `name` | string | Participant ID | Any non-empty string; unique per participant |
| `days_since_start` | integer | Study day | 1, 2, 3, … (gaps allowed; consecutive days are needed for night measures) |
| `valid80` | integer | Valid day: ring worn ≥ 80% of the day | `1` valid, `0` not valid. Only valid days enter any summary |
| `class_5_min` | string | Oura activity class for each 5-minute slot, 288 characters, starting 04:00 local | Each character `0` ring off, `1` rest, `2` inactive, `3` low, `4` medium, `5` high activity. Anything else (wrong length, other characters) is treated as missing |
| `average_hrv` | float | Average heart rate variability | ms |
| `average_heart_rate` | float | Average heart rate | bpm; `0` is a device error and is dropped |
| `steps` | float | Daily steps | ≥ 0 |
| `spo2` | float | Average blood oxygen | %, may be empty |
| `sex` | string | Sex | `Female`, `Male` |

### Optional columns

Each one adds something to the dashboard; leave it out and that part is hidden or empty.

| Column | Type | Meaning | Valid values | Used for |
|---|---|---|---|---|
| `met_5min` | string | 288 comma-separated five-minute mean MET values, aligned with `class_5_min` | floats ≥ 0, e.g. `0.95,0.97,…` | 24-hour MET profile, MET explorer |
| `non_wear_pct` | float | Share of the day the ring was off | 0–1 | "median wear" on the profile |
| `day_type` | string | Weekday or weekend | `Weekday`, `Weekend` | Not used by the dashboard; kept for the study format |
| `age_yrs` | integer | Age at enrolment | years | Patients table, age filter |
| `ctp_class` | string | Child-Pugh class | `Child A`, `Child B`, `Child C` | Patients table, filter |
| `ctp_cat` | string | Child-Pugh grouped | `Child A`, `Child B/C` | Cohort comparison |
| `meld` | integer | MELD score | 6–40 | Patients table, filter |
| `cirrhosis_etiology` | string | Cause of cirrhosis | free text, e.g. `Alcohol`, `NAFLD`, `Hepatitis C` | Profile, filter |
| `decomp` | float | Decompensated cirrhosis | `1` yes, `0` no | Cohort comparison, filter |
| `anyhe` | float | Any hepatic encephalopathy | `1`, `0` | HE status |
| `anyOHE` | float | Overt HE ever | `1`, `0` | HE status, cohort comparison |
| `che_phes` | string | Covert HE by PHES | `Yes`, `No` | HE status, cohort comparison |
| `phes_score` | integer | PHES score | lower is worse; ≤ −4 is commonly abnormal | Profile |
| `he_meds` | string | On HE medication at enrolment | `Yes`, `No` | Cohort comparison, filter |
| `ascites` | float | Ascites | `1`, `0` | Cohort comparison, filter |
| `variceal_bleeding_history` | string | History of variceal bleeding | `Yes`, `No` | Profile |
| `psqiglobal` | integer | Pittsburgh Sleep Quality Index | 0–21, higher is worse | Profile |
| `cldq_total` | float | Chronic Liver Disease Questionnaire | 1–7, higher is better | Profile |
| `liver_frailty_index` | float | Liver Frailty Index | roughly 2–7, higher is frailer | Profile |

The patient's HE status shown in the app is derived: `Overt` if `anyOHE == 1`, else `Covert` if `che_phes == "Yes"`, else `No` if `anyhe == 0`, otherwise unknown.

### Columns the app adds

`load()` computes these from `class_5_min`; don't supply them.

| Column | Meaning |
|---|---|
| `night_rest_h` | Longest rest bout between 6 pm and noon (breaks up to 10 minutes allowed), hours. Needs this day and the next to be valid and consecutive. Bouts under 2 h or over 16 h are dropped |
| `rest_mid_6pm` | Midpoint of that bout, hours after 6 pm |
| `sri` | Rest regularity (adapted Sleep Regularity Index), −100 to 100, from this day vs. the next |
| `grp_<column>` | Readable group labels for the Cohort tab |

## How each column is generated

All settings live in `data.SYNTHETIC_DEFAULTS` (also printed by `--print-config`). Names in `code` below are setting names. Ranges `[lo, hi]` are inclusive integers; a list of categories is drawn uniformly; a `{category: probability}` dict is drawn with those weights.

The values are hand-picked, round and clinically plausible for a cirrhosis cohort. They were **not** estimated from the study data. The only externally sourced numbers are the MET levels, which follow standard intensity bands (0.95 MET is the Compendium of Physical Activities value for sleeping; light < 3, moderate 3–6, vigorous > 6).

**Per participant (baseline):**

1. Decompensated with probability `decompensated_share` (0.5). Everything status-dependent below comes from the `compensated` or `decompensated` section.
2. `sex` uniform Female/Male; `age_yrs` uniform 35–74.
3. `anyhe`, `anyOHE`, `he_meds`, `ascites`: yes with the section's probability. Compensated: always no. Decompensated: 0.6, 0.4, 0.5, 0.5. Each is drawn **independently**.
4. `che_phes`: Yes 40%, independent of everything else.
5. `ctp_class`: Child A if compensated, Child B if decompensated; `ctp_cat` follows (`Child A` or `Child B/C`).
6. `meld`: uniform 6–11 (compensated) or 12–21 (decompensated).
7. `cirrhosis_etiology` uniform over Alcohol, NAFLD, Hepatitis C; `variceal_bleeding_history` always No; `phes_score` uniform −8 to 2; `psqiglobal` uniform 3–14; `cldq_total` uniform 3.0–6.5; `liver_frailty_index` always missing.
8. The participant's own usual values, drawn once from a normal distribution around the section's mean with `between_patient_sd`:

| Setting | Compensated mean | Decompensated mean | Between-patient SD |
|---|---|---|---|
| `bedtime_h` (clock hour; 24.5 = 00:30) | 23.3 (23:18) | 24.5 (00:30) | 0.6 h |
| `rest_h` (night rest length) | 6.6 h | 5.2 h | 0.4 h |
| `hrv_ms` | 28 ms | 20 ms | 4 |
| `hr_bpm` | 66 bpm | 72 bpm | 4 |
| `steps` | 8,500 | 5,500 | 1,500 |

9. Follow-up length uniform between `--min-days` and `--max-days`.

**Per day:**

1. **Rest.** Bedtime = the participant's bedtime + normal noise with SD `bedtime_night_sd` (0.6 h compensated, 1.2 h decompensated). Length = normal around the participant's `rest_h` with SD `day_to_day_sd.rest_h` (1.0 h), at least `floors.rest_h` (2.5 h). Those slots get class `1`, wrapping past 04:00 so the morning of the same day string is rest too.
2. **Activity.** Every other slot is inactive (`2`), except a random `activity.active_share` (25%) that becomes low/medium/high (`3`/`4`/`5`) with probabilities 0.70/0.25/0.05.
3. **Wear.** With probability `wear.invalid_day_share` (15%) the ring is off (`0`) for `wear.gap_slots` (90 slots = 7.5 h) starting at a random slot in `wear.gap_start_slot` (0–199). `non_wear_pct` is the share of `0` slots, and `valid80 = 1` when it is ≤ 0.2, so with the defaults a gap always makes the day invalid (about 85% of days are valid).
4. **MET.** Each slot's MET is `activity.met_by_class[class]` (0, 0.95, 1.2, 2.2, 3.8, 6.5) times a uniform multiplier in `activity.met_jitter` (0.85–1.15).
5. **HRV, heart rate, steps.** Normal around the participant's usual value with `day_to_day_sd` (4 ms, 3 bpm, 2,000 steps), floored at `floors.hrv_ms` (5) and `floors.steps` (0).
6. **SpO₂.** Present on a random `spo2.day_share` (40%) of days, normal with mean 97% and SD 1.
7. `day_type` is `Weekend` when `days_since_start % 7` is 6 or 0. Nothing else depends on it.

## Built-in relationships

The only link between clinical status and the wearable data is **decompensation**. By construction, decompensated participants have:

- shorter night rest (5.2 vs 6.6 h on average),
- later rest midpoint (bedtime 00:30 vs 23:18),
- less regular rest, so lower SRI (bedtime varies ±1.2 h vs ±0.6 h night to night),
- lower HRV (20 vs 28 ms), higher heart rate (72 vs 66 bpm), fewer steps (5,500 vs 8,500),
- higher MELD and Child-Pugh B instead of A.

Overt HE, any HE, HE medication and ascites occur only in decompensated participants, so they **inherit** those wearable differences indirectly. Covert HE (`che_phes`), sex, age, etiology, PHES, PSQI and CLDQ have **no** relationship with any wearable measure.

With the defaults (24 participants, seed 7), the Cohort tab's decompensation comparison shows clear "significant" differences in night rest, SRI and steps, while the covert-HE comparison shows nothing (p ≈ 0.9–1.0). Both results are exactly what the settings put in.

## Changing the parameters

Write a JSON file with only the settings you want to change. Top-level sections merge key by key, and each value inside a section is replaced whole. Unknown names are rejected, so typos fail loudly.

```json
{
  "decompensated_share": 0.3,
  "decompensated": {"rest_h": 6.6, "bedtime_night_sd": 0.6},
  "baseline": {"cirrhosis_etiology": {"Alcohol": 0.4, "NAFLD": 0.4, "Hepatitis C": 0.1, "Other": 0.1}},
  "wear": {"invalid_day_share": 0.3},
  "spo2": {"day_share": 0}
}
```

```bash
python scripts/generate_synthetic_data.py --config my_settings.json --participants 100 --seed 1
```

That example removes the night-rest difference between groups (a useful null check: the dashboard should then show none), makes etiology non-uniform, adds more invalid days and drops SpO₂.

To build a relationship the generator doesn't have (for example covert HE lowering SRI, or a trend over time), edit `synthetic_cohort()` in `data.py`. Draws happen in a fixed order; adding or reordering random calls changes every dataset for a given seed, so regenerate any saved files afterwards.

## Using your own dataset

Any CSV (or `.csv.gz`) with the [required columns](#required-columns) works:

```bash
HE_DATA_SOURCE=synthetic HE_SYNTHETIC_FILE=/path/to/your.csv.gz python app.py
```

- One row per participant-day, `days_since_start` from 1.
- Repeat baseline fields on every row of a participant (the app reads the first row).
- `class_5_min` must be exactly 288 characters `0`–`5`, starting at 04:00. If your device reports minutes or another day start, resample it first.
- Binary clinical fields use `1`/`0` (`decomp`, `anyhe`, `anyOHE`, `ascites`) or `Yes`/`No` (`che_phes`, `he_meds`) exactly as in the tables above; other codings won't match the Cohort-tab groups.
- The PDF report defaults to the decompensation comparison, so without `decomp` pass another group (e.g. `?group=sex`).
- The app labels data loaded this way as synthetic (amber banner, generated MRNs). It runs locally with no login: don't load identifiable data.

## Limitations

- **Independent draws.** Every day is drawn independently around the participant's usual values: no weekly rhythm, weekend effect, seasonality, trends, events, hospitalisations or dropout. The trend and "needs attention" indicators only ever react to noise.
- **Inconsistent combinations are possible.** `anyhe`, `anyOHE` and `che_phes` are drawn independently, so a participant can have overt HE with `anyhe = 0`, or covert HE while compensated with `anyhe = 0`. A few participants therefore have an unknown HE status.
- **Simplified clinical picture.** No Child-Pugh C; MELD isn't linked to anything within a group; variceal bleeding is always No; frailty is always missing.
- **Unlinked wearable measures.** Steps aren't tied to the activity classes or MET; HRV, heart rate and SpO₂ aren't tied to each other or to rest. SpO₂ is missing on random days, whereas in practice availability usually depends on the ring model and settings, so it is missing for whole participants.
- **Idealised rest.** One unbroken rest bout per night, no naps, no daytime rest, no wake bouts; the ring-off gap is always one block.
- **Not a model of the study cohort.** Sizes, prevalences and effect sizes were chosen to make every part of the dashboard show something, not to resemble any real population.
- The analysis notebooks in `notebooks/` read the study `.rds` file directly and do not run on synthetic data.
