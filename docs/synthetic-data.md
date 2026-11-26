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

One row per **participant-day**, in the format of the study's de-identified Oura export (after the app's R → CSV conversion). There are no calendar dates: days are numbered from enrollment, starting at **day 0** (as in the Nov 2026 study file; older files start at 1, and the app handles either). Clinical fields are **baseline only**: the same value is repeated on every row of a participant.

### Required columns

The app needs these columns to run at all.

| Column | Type | Meaning | Valid values |
|---|---|---|---|
| `name` | string | Participant ID | Any non-empty string; unique per participant |
| `days_since_start` | integer | Study day | 0, 1, 2, … (or from 1; gaps allowed; consecutive days are needed for night measures). A duplicated (`name`, day) keeps one row: the valid-wear copy, else the first |
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
| `met_5min` | string | 288 comma-separated five-minute mean MET values, aligned with `class_5_min` | floats ≥ 0, e.g. `0.95,0.97,…` | `/api/patient/<id>/intraday`, the notebooks' MET views |
| `non_wear_pct` | float | Share of the day the ring was off | 0–1 | "median wear" on the profile |
| `day_type` | string | Weekday or weekend | `Weekday`, `Weekend` | Not used by the dashboard. Gone from the Nov 2026 study file; the generator keeps it because the exploration notebook reads it |
| `age_yrs` | integer | Age at enrollment | years | Patients table, age filter |
| `ctp_class` | string | Child-Pugh class | `Child A`, `Child B`, `Child C` | Patients table, filter, Cohort comparison (A / B / C) |
| `ctp_cat` | string | Child-Pugh grouped | `Child A`, `Child B/C` | Cohort comparison |
| `meld` | integer | MELD score | 6–40 | Patients table, filter, Cohort comparison (bands <10 / 10–14 / ≥15) |
| `cirrhosis_etiology` | string | Cause of cirrhosis | free text, e.g. `Alcohol`, `NAFLD`, `Hepatitis C` | Profile, filter, Cohort comparison (Alcohol / MASLD / Other) |
| `decomp` | float | Decompensated cirrhosis | `1` yes, `0` no | Cohort comparison, filter |
| `anyhe` | float | Any hepatic encephalopathy | `1`, `0` | HE status |
| `anyOHE` | float | Overt HE ever | `1`, `0` | HE status, cohort comparison |
| `che_phes` | string | Covert HE by PHES | `Yes`, `No` | HE status, cohort comparison |
| `phes_score` | integer | PHES score | lower is worse; ≤ −4 is commonly abnormal | Profile |
| `he_meds` | string | On HE medication at enrollment | `Yes`, `No` | Cohort comparison, filter |
| `ascites` | float | Ascites | `1`, `0` | Cohort comparison, filter. **Derived by the app** when the file has only `current_ascites`/`previous_ascites` (the study file and the generator): 1 if current is Slight/Moderate or previous is Yes, 0 if both are known and neither, else missing |
| `current_ascites` | string | Ascites at enrollment | `Absent`, `Slight`, `Moderate` | Derives `ascites`; Cohort comparison "Ascites now"; snapshot |
| `previous_ascites` | string | Ascites before enrollment | `Yes`, `No` | Derives `ascites`; snapshot |
| `previous_encephalopathy` | string | HE before enrollment | `Yes`, `No` | Snapshot |
| `medication_for_hepatic_enc` | string | On HE medication | `Yes`, `No` | Snapshot |
| `bb_use_t0` | string | Beta-blocker at enrollment (affects HR and HRV) | `Yes`, `No` | Snapshot, Cohort comparison |
| `total_bilirubin_t0`, `albumin_t0`, `inr_t0` | float | Baseline labs | mg/dL, g/dL, ratio; `-999` = not available (the app makes it missing) | Snapshot |
| `ctp_score` | integer | Child-Pugh points | 5–15; `-999` = not available | Snapshot, profile header |
| `whichsite`, `whicharm` | string | Study site and arm | `WCM`/`CUMC`, `Pragmatic`/`Frailty` | Snapshot (small print) |
| `variceal_bleeding_history` | string | History of variceal bleeding | `Yes`, `No` | Profile |
| `psqiglobal` | integer | Pittsburgh Sleep Quality Index | 0–21, higher is worse | Profile |
| `cldq_total` | float | Chronic Liver Disease Questionnaire | 1–7, higher is better | Profile |
| `liver_frailty_index` | float | Liver Frailty Index | roughly 2–7, higher is frailer | Profile |

### Sleep columns (Oura sleep record)

One record per day at most, filed on the **wake day**: row *d* holds the night that ended on the morning of day *d*. A day with no record has every one of these empty. These are the converted-cache names (the R step turns the study's `bedtime_start`/`bedtime_end` date-times into the two `_h18` columns and `time_in_bed_s`, so no calendar date reaches the CSV).

| Column | Type | Meaning | Valid values |
|---|---|---|---|
| `type` | string | Oura record type | `long_sleep` (main sleep), `sleep`, `late_nap`, `rest` |
| `sleep_phase_5_min` | string | Hypnogram, one character per 5 minutes in bed | `1` deep, `2` light, `3` REM, `4` awake (Oura API codes; matched the duration columns in the study's aggregates, still to confirm with the study team). `data.parse_hypnogram()` turns it into minutes per stage |
| `num_sleep_phase_5_min` | float | Length of the hypnogram | slots |
| `time_in_bed_s` | float | Time in bed (real elapsed time) | seconds ≈ 300 × slots |
| `total_sleep_duration` | float | Time asleep | seconds; + `awake_time` = `time_in_bed_s` |
| `awake_time` | float | Time awake in bed, including latency | seconds |
| `latency` | float | Time to fall asleep | seconds |
| `rem_sleep_duration` | float | REM sleep | seconds |
| `rem_sleep_dur_pct` | float | REM / total sleep | **fraction 0–1**, not percent |
| `restless_periods` | float | Restless periods in the night | count |
| `breathing_disturbance_index` | float | Breathing disturbances | events/hour, often missing |
| `average_breath` | float | Average breathing rate | breaths/min |
| `bedtime_start_h18`, `bedtime_end_h18` | float | Bedtime and wake time, wall-clock hours after 18:00 on the evening before the wake day | e.g. 5.0 = 23:00, 13.0 = 07:00 on the wake day. Continuous across midnight, start < end; show with `_clock(18 + h)` |

The study file also has `movement_30_sec`, which the R step drops (the values are unusable).

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
3. `anyhe`, `anyOHE`, `he_meds`, `ascites`: yes with the section's probability. Compensated: always no. Decompensated: 0.6, 0.4, 0.5, 0.8. Each is drawn **independently**.
4. `che_phes`: Yes 40%, independent of everything else.
5. `ctp_class`: Child A if compensated, Child B if decompensated (Child C when the points in step 10 reach 10 or more); `ctp_cat` follows (`Child A` or `Child B/C`).
6. `meld`: uniform 6–11 (compensated) or 12–21 (decompensated).
7. `cirrhosis_etiology` uniform over Alcohol, NAFLD, Hepatitis C; `variceal_bleeding_history` always No; `phes_score` uniform −8 to 2; `psqiglobal` uniform 3–14; `cldq_total` uniform 3.0–6.5; `liver_frailty_index` always missing. (`psqiglobal`, `cldq_total` and `day_type` are no longer in the study file; the generator still makes them.)
8. The participant's own usual values, drawn once from a normal distribution around the section's mean with `between_patient_sd`:

| Setting | Compensated mean | Decompensated mean | Between-patient SD |
|---|---|---|---|
| `bedtime_h` (clock hour; 24.5 = 00:30) | 23.3 (23:18) | 24.5 (00:30) | 0.6 h |
| `rest_h` (night rest length) | 6.6 h | 5.2 h | 0.4 h |
| `hrv_ms` | 28 ms | 20 ms | 4 |
| `hr_bpm` | 66 bpm | 72 bpm | 4 |
| `steps` | 8,500 | 5,500 | 1,500 |

9. **Ascites.** The study file has no `ascites` column, so the generator writes `current_ascites` and `previous_ascites` and the app derives `ascites` from them. With ascites (the section's probability): `previous_ascites = Yes` and `current_ascites` drawn from `baseline.current_ascites_if_ascites` (Slight 55%, Moderate 25%, Absent 20% = resolved). Without: Absent and No.
10. **Labs, tied to MELD.** INR ~ normal(1.0 + 0.065 × (MELD − 6), 0.08); bilirubin ~ lognormal around 0.6 + 0.17 × (MELD − 6) mg/dL; albumin ~ normal(4.3 − 0.08 × (MELD − 6) − 0.3 if ascites, 0.25) g/dL; rounded to 1 decimal. `ctp_score` is the Child-Pugh points from those labs, current ascites and prior overt HE, clipped to the section's `ctp_score` range (5–6 compensated, 7–12 decompensated); a decompensated participant with 10 or more points is Child C. With the defaults (24 participants, seed 7): 11 decompensated, 7 with ascites now, Child-Pugh A/B/C 13/6/5, so every Cohort-tab grouping has at least 5 people per level and gets a test.
11. `previous_encephalopathy` = Yes when `anyOHE` is 1; `medication_for_hepatic_enc` copies `he_meds`; `bb_use_t0` Yes with probability `bb_use` (0.3 / 0.55); `whichsite` WCM 65% / CUMC 35%; `whicharm` Pragmatic 60% / Frailty 40%.
12. The participant's usual sleep: time in bed (`time_in_bed_h`, between-patient SD 0.4 h), deep and REM shares of sleep (`deep_share`, `rem_share`, SD 0.03) and breathing rate (`breath_rpm`, SD 1).
13. Follow-up length uniform between `--min-days` and `--max-days`, numbered from day 0.

The baseline extras (items 9–12) and the sleep records come from a second random stream (`seed` plus 1), so adding them didn't change the older columns for a given seed.

**Per day:**

1. **Rest.** Bedtime = the participant's bedtime + normal noise with SD `bedtime_night_sd` (0.6 h compensated, 1.2 h decompensated). Length = normal around the participant's `rest_h` with SD `day_to_day_sd.rest_h` (1.0 h), at least `floors.rest_h` (2.5 h). Those slots get class `1`, wrapping past 04:00 so the morning of the same day string is rest too.
2. **Activity.** Every other slot is inactive (`2`), except a random `activity.active_share` (25%) that becomes low/medium/high (`3`/`4`/`5`) with probabilities 0.70/0.25/0.05.
3. **Wear.** With probability `wear.invalid_day_share` (15%) the ring is off (`0`) for `wear.gap_slots` (90 slots = 7.5 h) starting at a random slot in `wear.gap_start_slot` (0–199). `non_wear_pct` is the share of `0` slots, and `valid80 = 1` when it is ≤ 0.2, so with the defaults a gap always makes the day invalid (about 85% of days are valid).
4. **MET.** Each slot's MET is `activity.met_by_class[class]` (0, 0.95, 1.2, 2.2, 3.8, 6.5) times a uniform multiplier in `activity.met_jitter` (0.85–1.15).
5. **HRV, heart rate, steps.** Normal around the participant's usual value with `day_to_day_sd` (4 ms, 3 bpm, 2,000 steps), floored at `floors.hrv_ms` (5) and `floors.steps` (0).
6. **SpO₂.** Present on a random `spo2.day_share` (40%) of days, normal with mean 97% and SD 1.
7. `day_type` is `Weekend` when `days_since_start % 7` is 6 or 0. Nothing else depends on it.
8. **Sleep record** (the night that ended this morning). No record with probability `no_sleep_record_share` (6% compensated, 10% decompensated): every sleep column is empty, and so are HRV, heart rate and SpO₂, which Oura measures overnight. Otherwise, with probability `non_main_share` (5% / 15%) it is a short non-main record (`sleep.non_main_types`: sleep 85%, late_nap 10%, rest 5%) of 0.5–3 h starting at a uniform hour 10:00–20:00. Otherwise it is a `long_sleep` that starts at the previous evening's activity-rest bedtime (so it lines up with `class_5_min`) and lasts the participant's time in bed ± 0.8 h (at least 3 h).
9. **Hypnogram first, everything else from it**, so the study's identities hold exactly: a leading run of `4` with mean `latency_min` (12 / 25 min); then 90-minute cycles (`sleep.cycle_slots`) with deep sleep front-loaded (`sleep.deep_by_cycle` 45/35/15/5% of the night's deep slots) and REM growing from cycle to cycle (`sleep.rem_per_cycle` 2 → 6), light sleep filling the rest; wake bouts of 1–3 slots at `wake_bouts_per_h` (0.6 / 1.2 per hour) inserted at random; 1–3 slots of `4` at the end. Then `total_sleep_duration` = 300 × slots of 1–3, `rem_sleep_duration` = 300 × slots of 3, `latency` = 300 × leading 4s, `time_in_bed_s` = 300 × length ± up to 150 s, `awake_time` = `time_in_bed_s` − total sleep, `rem_sleep_dur_pct` = REM / total sleep (0–1), `restless_periods` ~ Poisson(`restless_per_h` × hours in bed; 22 / 32), `breathing_disturbance_index` present on `sleep.bdi_share` (40%) of nights (gamma, mean `bdi` 3 / 6, at most 50), `average_breath` normal around the participant's rate, SD 0.5. `bedtime_start_h18` = start − 18 h, `bedtime_end_h18` = start + time in bed, both rounded to the minute.

## Medications and clinical history

The patient profile's Clinical snapshot has **Medications** and **Clinical history** dropdowns. The study file has neither, so on study data they say so. On synthetic data the app makes a few illustrative rows per patient when the page loads (`data.synthetic_clinical_extras`; nothing is stored, and they aren't columns in the CSV). Every row is tagged *Synthetic* on the page. They're there to show the layout, not to model anything: no measure or test uses them.

- **Seeded by patient id** (with the generator's default seed, `data.EXTRAS_SEED`), so a patient always gets the same rows, whichever seed made the cohort.
- **Medications (0–4)** follow the patient's baseline fields: HE meds → lactulose, plus rifaximin (always after overt HE, otherwise half the time); ascites → spironolactone, plus furosemide 70% of the time; variceal bleeding history → nadolol, otherwise decompensated patients get carvedilol 40% of the time. Each is "before enrollment" (70%) or "since study day N".
- **Clinical history (0–4 events)**, by study day within the patient's own days (the data has no calendar dates): HCC surveillance imaging (ultrasound; MRI for 30% of compensated patients) for 75% of patients, usually followed by an AFP check within a week; for decompensated patients only, a hospitalization half the time (overt HE episode if they've had overt HE, else paracentesis if they have ascites, else upper GI bleed) and MELD labs 40% of the time.

## Built-in relationships

The only link between clinical status and the wearable data is **decompensation**. By construction, decompensated participants have:

- shorter night rest (5.2 vs 6.6 h on average),
- later rest midpoint (bedtime 00:30 vs 23:18),
- less regular rest, so lower SRI (bedtime varies ±1.2 h vs ±0.6 h night to night),
- lower HRV (20 vs 28 ms), higher heart rate (72 vs 66 bpm), fewer steps (5,500 vs 8,500),
- higher MELD and Child-Pugh B or C instead of A, with higher bilirubin and INR, lower albumin and a higher `ctp_score`,
- Oura sleep that is later (bedtime ~00:30 vs ~23:20), shorter (less total sleep despite slightly more time in bed), more fragmented (efficiency ~0.73 vs ~0.85: longer latency, twice the wake bouts), with less deep sleep (9% vs 15% of sleep) and less REM (14% vs 20%), more restless periods, slightly faster breathing, and more nights with no record (10% vs 6%) or only a short non-main record (15% vs 5%).

Overt HE, any HE, HE medication and ascites (now or before) occur only in decompensated participants, so they **inherit** those wearable differences indirectly. Covert HE (`che_phes`), beta-blocker, sex, age, etiology, PHES, PSQI and CLDQ have **no** built-in relationship with any wearable measure.

With the defaults (24 participants, seed 7), the Cohort tab's decompensation comparison shows clear "significant" differences in sleep timing, total sleep and steps, while the covert-HE comparison shows "No clear difference". Both results are exactly what the settings put in.

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

- One row per participant-day, `days_since_start` from 0 (or 1).
- Repeat baseline fields on every row of a participant (the app reads the first row).
- `class_5_min` must be exactly 288 characters `0`–`5`, starting at 04:00. If your device reports minutes or another day start, resample it first.
- Binary clinical fields use `1`/`0` (`decomp`, `anyhe`, `anyOHE`, `ascites`) or `Yes`/`No` (`che_phes`, `he_meds`) exactly as in the tables above; other codings won't match the Cohort-tab groups.
- The PDF report defaults to the decompensation comparison, so without `decomp` pass another group (e.g. `?group=sex`).
- The app labels data loaded this way as synthetic (amber banner, generated MRNs). It runs locally with no login: don't load identifiable data.

## Limitations

- **Independent draws.** Every day is drawn independently around the participant's usual values: no weekly rhythm, weekend effect, seasonality, trends, events, hospitalisations or dropout. The trend and "needs attention" indicators only ever react to noise.
- **Inconsistent combinations are possible.** `anyhe`, `anyOHE` and `che_phes` are drawn independently, so a participant can have overt HE with `anyhe = 0`, or covert HE while compensated with `anyhe = 0`. A few participants therefore have an unknown HE status.
- **Simplified clinical picture.** Child-Pugh C only through the points rule, never at random; MELD isn't linked to anything within a group; variceal bleeding is always No; frailty is always missing.
- **Unlinked wearable measures.** Steps aren't tied to the activity classes or MET; HRV, heart rate and SpO₂ aren't tied to each other or to rest. SpO₂ is missing on random days, whereas in practice availability usually depends on the ring model and settings, so it is missing for whole participants.
- **Idealised rest.** One unbroken rest bout per night in `class_5_min`, no naps, no daytime rest, no wake bouts; the ring-off gap is always one block. The Oura sleep record has wake bouts, but only its start matches the activity rest bout: its length comes from its own time-in-bed setting, and non-main records don't appear in `class_5_min` at all.
- **Schematic hypnograms.** Every cycle is light → deep → light → REM, with no stage transitions inside a block and no brief arousals shorter than 5 minutes. `movement_30_sec` is not generated.
- **Not a model of the study cohort.** Sizes, prevalences and effect sizes were chosen to make every part of the dashboard show something, not to resemble any real population.
- The analysis notebooks in `notebooks/` run on this data with `HE_DATA_SOURCE=synthetic` (see the README's Notebooks section). Whatever they "find" comes from the [built-in relationships](#built-in-relationships) above.
