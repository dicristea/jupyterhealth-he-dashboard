# Technical reference

How the dashboard is put together, how every measure is computed, and every setting. The [README](../README.md) has the overview and quickstart.

**Contents:** [Architecture](#architecture) · [Data flow](#data-flow) · [Routes](#routes) · [How the measures are computed](#how-the-measures-are-computed) · [Configuration](#configuration)

## Architecture

```
jupyterhealth-he-dashboard/
├── app.py                 Flask routes, SMART on FHIR launch, notebook export
├── data.py                loading, synthetic generator, derived measures, page payloads
├── templates/             Jinja pages (all extend base.html)
├── static/charts.js       hand-written SVG/canvas charts (no chart library, no build step)
├── static/style.css
├── scripts/
│   ├── generate_synthetic_data.py   writes synthetic_data/*.csv.gz
│   ├── render_notebooks.py          demo image build: runs the notebooks on synthetic data → read-only HTML
│   └── seed_medplum.py              creates synthetic FHIR Patients in Medplum
├── notebooks/*.py         data_overview, deid_oura_data_exploration, he_trajectories_phenotypes, speech_features_starter
│                          (jupytext percent format, no outputs)
├── tests/                 pytest, runs on synthetic data only
├── docs/                  medplum.md, synthetic-data.md, hosting.md, jupyterlite.md
├── lite/, scripts/build_jupyterlite.sh   JupyterLite site (GitHub Pages, synthetic data)
├── Dockerfile, .dockerignore, render.yaml   public synthetic demo only
├── requirements-notebooks.txt   notebook stack for the Dockerfile's builder stage
├── requirements-speech.txt      Praat/librosa stack for the speech notebook only (also in binder/)
└── requirements.txt
```

### Data flow

```
 study .rds (outside repo) ──Rscript + haven──▶ CSV cache <stem>.v3.csv (outside repo) ─┐
 synthetic CSV (HE_SYNTHETIC_FILE) ─────────────────────────────────────────────────────┼─▶ data.load()
 data.synthetic_cohort() in memory (no file / public demo) ─────────────────────────────┘      │
                                                                                                ▼
                    daily table + derived: night_rest_h, rest_mid_6pm, sri, sleep_*, grp_*  (lru_cache)
                                                                                                │
        patient_list() · patient_detail() · intraday() · cohort_summary() · cohort_compare()   │
                                                                                                ▼
                         app.py routes → Jinja templates / JSON → static/charts.js in the browser
```

1. **Source selection** (`data.load()`, controlled by `HE_DATA_SOURCE`, `HE_PUBLIC_DEMO` and `HE_HOSTED_STUDY`):

   | Mode | Reads |
   |---|---|
   | `HE_PUBLIC_DEMO=1` | always the in-memory cohort `synthetic_cohort()` (24 patients, seed 7). Refuses to start with `HE_DATA_SOURCE=study` or `HE_MRN_MAP` |
   | `HE_HOSTED_STUDY=1` | the gzipped `.v3.csv` cache downloaded once from `HE_DATA_URL` (source `study`). Refuses to start without `DEMO_PASSWORD` or `HE_DATA_URL`, with `HE_DATA_SOURCE=synthetic` or `HE_MRN_MAP`, or together with `HE_PUBLIC_DEMO` ([hosting.md](hosting.md#hosted-de-identified-site)) |
   | `HE_DATA_SOURCE=synthetic` | `HE_SYNTHETIC_FILE` (error if missing) |
   | `HE_DATA_SOURCE=study` | `HE_DATA_FILE` (error if missing) |
   | `HE_DATA_SOURCE=auto` (default) | `HE_DATA_FILE` if it exists, otherwise the in-memory synthetic cohort |

2. **Raw file → cache** (study data only). Python's `pyreadr` can't parse the `.rds` file's haven-labelled and list columns, so `data._read_rds` writes a small R script (`R_TO_CSV` in `data.py`) to `HE_CACHE_DIR` and runs `Rscript` once. It converts labelled columns to factors, drops list columns, and turns per-minute MET (`met_val`, 1,440 values per day) into 288 five-minute means (`met_5min`, comma-separated, aligned with `class_5_min`). **No calendar date leaves this step:** `bedtime_start`/`bedtime_end` become `bedtime_start_h18`/`bedtime_end_h18` (wall-clock hours after 18:00 local on the evening before the wake day, so no midnight wrap and start < end) plus `time_in_bed_s` (real elapsed seconds), the date-times are dropped, `movement_30_sec` (unusable numbers) is dropped, and the script stops if any other Date/POSIXct column is left. The CSV `<stem>.v3.csv` (`data.CACHE_VERSION`) is reused until the `.rds` is newer; bump the version whenever `R_TO_CSV`'s output changes. The file is written under a temporary name and renamed when R finishes. Any file name works for a downloaded cache (`_read_cache_csv` tells gzip by its magic bytes).
   Then `harmonise_columns()` maps every source onto what the app reads: `-999` (the study's missing code in `total_bilirubin_t0`, `albumin_t0`, `inr_t0`, `ctp_score`) becomes missing; `ascites` is derived when the file only has `current_ascites`/`previous_ascites` (1 if current is Slight/Moderate or previous is Yes, 0 if both are known and neither, else missing); one row per (`name`, `days_since_start`), keeping the valid-wear copy of a duplicated day, then the first in file order. Study days start at 0 in the Nov 2026 file (1 in older files): page code uses `data.first_day(df)`, never a literal 1. The cache sits outside the repo by default so a converted copy can't be committed by accident.
3. **Derived measures** are added to every row in `load()` (next section), then the table is cached in memory (`functools.lru_cache`). **Restart the app after the data changes.**
4. **Page payloads**: `patient_list()` (Patients tab, also cached), `patient_detail()` (profile), `intraday()` (5-minute MET, `/api/patient/<id>/intraday`), `cohort_summary()` and `cohort_compare()` (Cohort tab, `/api/cohort`, PDF report).
5. **Cohort selections** travel as the Patients-tab filter parameters (defined in `data.FILTERS`, parsed by `data.parse_filters`), never as lists of patient IDs. Exported notebooks store the filters too, and re-select patients when run.

### Routes

| Route | Purpose |
|---|---|
| `GET /` | Patients tab (accepts the filter params) |
| `GET /patient/<id>?window=` | Patient profile; `window` = 7 or 14 recent study days for the cards (default 14; any other value falls back to 14) |
| `GET /api/patient/<id>/intraday?start=&days=` | 5-minute MET and activity class for 1–14 consecutive days |
| `GET /cohort?group=&metric=`, `GET /api/cohort?group=&metric=` | Cohort tab (the params preselect the menus; unknown values are ignored) and its JSON (defaults `decomp`, `sleep_mid_h18`; 400 for unknown keys). Both also take the filter params |
| `GET /cohort/report?group=` | Printable report of all 12 Cohort measures in menu order (default group `decomp`) |
| `POST /api/cohort/notebook` | Writes a pre-filled `.ipynb` to the first `NOTEBOOK_DIRS` folder (local only) |
| `GET /api/cohort/notebook.ipynb?group=`, `GET /api/cohort/notebook/<token>/<name>.ipynb` | The same notebook as a download, never written to disk (also in the demo); the second form carries the filters base64url-encoded for JupyterLite's `fromURL`. CORS only for `NOTEBOOK_CORS_ORIGINS` |
| `GET /notebooks` | Notebooks tab |
| `GET /notebooks/view/<name>` | Read-only notebook page (public demo only; rendered on synthetic data when the image is built) |
| `GET /notebooks/code/<name>` | Read-only code of a notebook in `CODE_PAGES` (the speech starter), every mode; never executed. Both Docker images copy that one `.py` |
| `GET /ehr`, `/ehr/launch`, `/ehr/callback`, `/ehr/simulate?mrn=` | SMART on FHIR launch (launch and callback are local only) |
| `GET /healthz` | Host health check; open even with a password set |
| `GET`/`POST /login`, `GET /logout` | Login page and logout when `DEMO_PASSWORD` is set (signed session cookie) |
| `GET /lite/<path>` | The hosted study site's own JupyterLite (static, behind the login; hosted study only, 404 otherwise) |
| `GET /api/study-data.csv.gz` | The downloaded study cache for that JupyterLite (logged in, hosted study only, `no-store`) |
| `GET /robots.txt` | Disallow all (hosted modes only; 404 locally) |

## How the measures are computed

All of this is in `data.py`. The input is one row per participant-day. Oura's `class_5_min` is a 288-character string, one character per 5-minute slot of a day that runs **04:00 → 04:00** local: `0` ring off, `1` rest, `2` inactive, `3` low, `4` medium, `5` high activity. A string with the wrong length or other characters is treated as missing. Night rest, rest midpoint and rest regularity are activity-based **proxies** built from the rest class; the Cohort tab still uses them. Since the Nov 2026 file there is also an Oura sleep record per wake day (hypnogram `sleep_phase_5_min`, durations in seconds, `bedtime_*_h18`; columns in [synthetic-data.md](synthetic-data.md#sleep-columns-oura-sleep-record)); the patient profile and the Patients tab use those (`sleep_nights()`, `data.parse_hypnogram()` for minutes of deep/light/REM/awake).

| Measure | How it's computed |
|---|---|
| **Valid day** | `valid80 == 1` (ring worn ≥ 80% of the day; the synthetic generator sets it when `non_wear_pct ≤ 0.2`). Activity measures (steps, rest proxies, SRI) use valid days only; sleep measures and the Oura nightly vitals use main nights instead (below). |
| **Night rest** (`night_rest_h`, hours) | Join day *d* and day *d+1*, take the window **18:00 on day *d* → 12:00 on day *d+1*** (18 h). Find rest bouts: runs that start and end with a rest slot, where gaps of at most 2 non-rest slots (≤ 10 minutes, any class including ring off) are bridged. Take the longest (the first one on a tie). Bouts under 2 h, and any value outside 2–16 h, are missing. Computed only when days *d* and *d+1* are consecutive study days, both are valid and both strings are well formed. Stored on day *d*. |
| **Rest midpoint** (`rest_mid_6pm`) | Middle of that same bout, in hours after 18:00; shown as clock time (e.g. `03:15`). Has no "better" direction. |
| **Rest regularity** (`sri`, −100 to 100) | Adapted from the Sleep Regularity Index. Compare day *d* and day *d+1* slot by slot at the same clock time, using only slots where the ring was on (class ≠ 0) on both days. Needs at least 80% of the 288 slots. SRI = 200 × (share of those slots where both days agree on rest vs. not rest) − 100. 100 = identical days, 0 = the days agree on half the slots. Needs consecutive days with well-formed strings; stored on day *d*, and summaries then keep only valid days *d*. |
| **HRV, average HR, steps, SpO₂** | Taken from the file (`average_hrv`, `average_heart_rate`, `steps`, `spo2`). `average_heart_rate == 0` is a device error and becomes missing. HRV, HR and SpO₂ are Oura nightly values (`"mask": "main_sleep"` in `METRICS`), summarised over main nights; steps over valid days. |
| **MET** | Study data: per-minute MET → five-minute means (see Architecture). Served by `intraday()` (`/api/patient/<id>/intraday`); the patient profile no longer shows MET, the activity profile or the actogram (they are in the notebooks). |
| **Main night and sleep measures** (`sleep_nights()`) | Row *d* holds the Oura sleep record of the night that ended on the morning of day *d*. A **main night** is `type == "long_sleep"` with 3–16 h in bed (`time_in_bed_s`) and 1–14 h asleep (`total_sleep_duration`); it is not gated on `valid80`. Other records and days with none are missing, never 0. **Sleep midpoint** `sleep_mid_h18` = (`bedtime_start_h18` + `bedtime_end_h18`) / 2, on the hours-after-18:00 scale (continuous across midnight; shown as clock time). **Total sleep** `sleep_tst_h`. **Sleep efficiency** `sleep_eff_pct` = 100 × total sleep ÷ time in bed. **Awake after sleep onset** `sleep_waso_min` = 5 × the `4` (awake) slots in `sleep_phase_5_min` after removing leading and trailing awake runs; needs a stage string of 1–4 whose length matches time in bed within 2 slots. **Bedtime variability** `sleep_bed_sd_min` (`bedtime_sd_min()`) = SD in minutes of `bedtime_start_h18` over the main nights in the 14 study days up to that night, when there are ≥ 7 (the profile's 7-day window uses 7 days and ≥ 4 nights, `BED_SD_MIN`). |
| **Measure cards** (profile) | Sleep cards: `data.PATIENT_TILES` (the five sleep measures, then steps); HRV, average HR, SpO₂ and rest regularity are in the collapsed *More measures* (`MORE_TILES`). Sleep cards, HRV, HR and SpO₂ use main nights; steps and SRI use valid days. **Recent window** (`?window=7|14`, a toggle on the page, default 14, `WINDOWS`): *recent* = median of the values in the last 7 or 14 study days, needing ≥ 4 (`MIN_RECENT`); bedtime variability is the SD over that window's main nights (needs 4 / 7); *usual range* = the patient's 25th–75th percentile of the values **before** the window, needing ≥ 14 (`MIN_BASE`). Otherwise the card says *Not enough nights* (or days). An "i" button next to *Sleep and activity* explains these rules (popover on hover, focus or tap; Escape closes). The pill names the direction ("Later than usual", "Shorter than usual"); longer-than-usual total sleep is shown as a change, not an improvement. **Trend** (needs ≥ 28 values): median of the last 14 values vs. the 28 before; difference ÷ IQR above +0.5 is *Increasing* (*Later* for clock measures), below −0.5 *Decreasing* (*Earlier*), otherwise *Stable*. **Sleep timing chart**: the last 30 study days, one bar per sleep record from bedtime to wake, midpoint dot for main nights, the usual-midpoint IQR as a band; clicking a night draws its hypnogram (1 deep, 2 light, 3 REM, 4 awake). |
| **Needs attention** (Patients tab) | Sleep cards only, always the 14-day window (`RECENT_DAYS`) and the cards' minimums: flag a measure when the median of the recent nights is outside the patient's own **10th–90th percentile** of earlier nights in the worse direction (later midpoint, shorter sleep, lower efficiency, more awake time, more variable bedtime). The flag says it in words, e.g. "Sleep midpoint 1 h 20 min later than usual" (difference from the usual median). The filter param (`attention`) is unchanged. |
| **Clinical snapshot** (profile side panel) | One *At enrollment* section from the patient's first row, grouped as in `data.CLINICAL_SNAPSHOT`: liver severity (MELD, Child-Pugh score and class, bilirubin mg/dL, albumin g/dL, INR), decompensation, encephalopathy, beta-blocker, frailty; site, arm and etiology as small text. A column the data lacks is left out; a present column with no value is listed once under "Not recorded at enrollment"; empty groups are hidden. The Medications and Clinical history dropdowns are illustrative and appear on synthetic data only. The header line summarises age, sex, Child-Pugh, MELD, prior HE and HE medication (`clinical_headline()`). |
| **Ring wear coverage** (profile) | Share of study days from the patient's first day (`first_day()`, 0 in the Nov 2026 file) to the last that are valid (days missing from the file count as not valid), plus the median daily wear `1 − non_wear_pct`. The Patients-tab "valid days %" and the valid-days filter use the share of rows in the file instead. |
| **HE status** | `Overt` if `anyOHE == 1`, else `Covert` if `che_phes == "Yes"`, else `No` if `anyhe == 0`, otherwise unknown. Clinical fields are baseline only (first row per patient). |
| **Patient-list values** | Sleep midpoint, total sleep and efficiency: medians over main nights; steps: median over valid days. The sparkline is the 7-night rolling mean of the sleep midpoint (at least 3 nights), every third day. |
| **Cohort summary** | Median of per-patient medians, so each patient counts once: sleep midpoint and total sleep over main nights (the strip on Cohort, Patients and the report), night rest and regularity over valid days. |
| **Cohort measures** (`COHORT_MENU`) | *Sleep (Oura)*: sleep midpoint (default), total sleep, efficiency, awake after sleep onset, bedtime variability, on main nights. *Activity*: steps, rest regularity (SRI), night rest, rest midpoint, on valid days. *Physiology*: average HR, HRV, SpO₂, on main nights. |
| **Cohort groupings** (`GROUPS`, "Compare by", `GROUP_MENU`) | All baseline, at enrollment. *Encephalopathy*: overt HE, covert HE (PHES), HE medication. *Liver severity*: Child-Pugh A vs B/C, Child-Pugh A / B / C, MELD <10 / 10–14 / ≥15 (`meld_band`), ascites now (`ascites_now` from `current_ascites`), ascites now or before (`ascites`), decompensation. *Medications*: beta-blocker (`bb_use_t0`). *Demographics*: sex, etiology Alcohol / MASLD (NAFLD in the file) / Other (`etiology_group`). `meld_band`, `ascites_now` and `etiology_group` are derived in `harmonise_columns()` (`derive_groups()`). Site and arm are deliberately not groupings. |
| **Cohort comparison** (`cohort_compare()`) | A person counts with ≥ 14 values of the measure (`MIN_PERSON`; the others are reported as "N people left out" per level). **Trajectory**: each person's mean per study week (week = (day − first day) // 7 + 1) with ≥ 3 values, then the median and 25th–75th percentile across people, for weeks with ≥ 5 people in the level. **Dots**: each person's median. **Test** on those per-person medians, only between levels with ≥ 5 people (`MIN_TEST`; smaller levels are drawn but named as untested, and with fewer than two testable levels the page says "Too few patients for a test"): two levels, two-sided Mann-Whitney U with rank-biserial r = 1 − 2U / (n₁·n₂); three, Kruskal-Wallis with a descriptive r of each level vs the first (no pairwise p). `p_text` (`fmt_p()`) never says "p = 0": `p < 0.001`, else 3 decimals below 0.1, else 2. A direction sentence names the result ("Decompensated tend later (r = 0.32)", "No clear difference"). Exploratory and unadjusted. |

## Configuration

Every environment variable the code reads:

| Variable | Read by | Default | Meaning |
|---|---|---|---|
| `HE_DATA_SOURCE` | `data.py`, the data notebooks | `auto` | `auto`, `study` or `synthetic` (see [Data flow](#data-flow)) |
| `HE_DATA_FILE` | `data.py` | the single `.rds` file in `../de-identified data/` (next to the repo) | Study `.rds` file. With no file, or several, there, set it explicitly |
| `HE_SYNTHETIC_FILE` | `data.py`, `seed_medplum.py` | `./synthetic_data/synthetic_he.csv.gz` | CSV read when `HE_DATA_SOURCE=synthetic` |
| `HE_CACHE_DIR` | `data.py` | `<parent of the data folder>/exploration_outputs/_cache` | Where the `.rds` → CSV cache is written |
| `HE_PUBLIC_DEMO` | `data.py`, `app.py` | off | `1`/`true`/`yes`: public synthetic demo ([docs/hosting.md](hosting.md)) |
| `HE_HOSTED_STUDY` | `data.py`, `app.py`, `Dockerfile.deid` | off | `1`/`true`/`yes`: password-protected hosted site on the de-identified study data ([docs/hosting.md](hosting.md#hosted-de-identified-site)); forced on by `Dockerfile.deid` |
| `HE_DATA_URL` | `data.py` | unset | Hosted study only: URL of the gzipped `.v3.csv` cache, normally a private GitHub release asset API URL |
| `HE_DATA_TOKEN` | `data.py` | unset | Hosted study only: token sent as `Authorization: Bearer` to `HE_DATA_URL` (not forwarded on redirect, never logged) |
| `DEMO_PASSWORD`, `DEMO_USER` | `app.py` | unset, `demo` | Login page on every route except `/healthz`, `/login`, `/logout`, `/robots.txt`, `/static/`: optional in the demo, **required** with `HE_HOSTED_STUDY` ([docs/hosting.md](hosting.md#optional-shared-password)) |
| `HE_COOKIE_INSECURE` | `app.py` | unset | `1`: drop the Secure flag on the session cookie, only for running a hosted mode over local `http://` |
| `HE_LITE_DIR` | `app.py` | `lite_site/` | Hosted study only: the built JupyterLite served at `/lite/` (`Dockerfile.deid` puts it there; [jupyterlite.md](jupyterlite.md#hosted-study-site)) |
| `LITE_VARIANT` | `scripts/build_jupyterlite.sh` | `public` | `hosted-study`: build the study site's JupyterLite with no data and the `hosted_study.json` marker |
| `HE_MRN_MAP` | `data.py`, `app.py` | unset | CSV `mrn,study_id` **outside the repo**, for real patients only after IRB approval and authentication. Refused in demo mode |
| `PORT` | `app.py`, `Dockerfile` | `5050` (`8000` in the container) | Port |
| `HOST` | `app.py` | `127.0.0.1` | Bind address, **only honoured in a hosted mode** (`HE_PUBLIC_DEMO=1` or `HE_HOSTED_STUDY=1`); otherwise always `127.0.0.1` |
| `FLASK_DEBUG` | `app.py` | `1` | Flask debugger/reloader for local runs; always off in demo mode |
| `FLASK_SECRET_KEY` | `app.py` | random per process | Session signing key (sessions reset on restart without it) |
| `JUPYTER_BASE_URL` | `app.py` | `http://localhost:8888` | JupyterLab URL for notebook links |
| `JUPYTER_ROOT` | `app.py` | the repo folder | Folder JupyterLab was started from (links are relative to it) |
| `NOTEBOOK_DIRS` | `app.py` | `./notebooks` | Folders (separated by `:`, `;` on Windows) listed in the Notebooks tab; exports go to the first |
| `NOTEBOOK_REPO`, `NOTEBOOK_REF` | `app.py` | `dicristea/jupyterhealth-he-dashboard`, `main` | GitHub repo and branch the demo Notebooks tab and Cohort export link to on Binder (and for docs links) |
| `JUPYTERLITE_URL` | `app.py` | `https://dicristea.github.io/jupyterhealth-he-dashboard` | JupyterLite site the demo Notebooks tab and Cohort export (`?fromURL=`) link to |
| `NOTEBOOK_CORS_ORIGINS` | `app.py` | `https://dicristea.github.io` | Comma-separated origins allowed to fetch exported notebooks cross-origin (JupyterLite) |
| `SMART_CLIENT_ID` | `app.py` | unset | Client ID from the EHR's app registration |
| `SMART_CLIENT_SECRET` | `app.py` | unset | Client secret (optional: PKCE is always used) |
| `SMART_REDIRECT_URI` | `app.py` | `<this app>/ehr/callback` | Must exactly match the registered redirect URI |
| `SMART_SCOPES` | `app.py` | `launch openid fhirUser patient/Patient.rs` | Requested scopes |
| `SMART_MRN_SYSTEM` | `data.py`, `seed_medplum.py` | `https://jupyterhealth.org/fhir/identifier/mrn` | FHIR identifier system for the MRN |
| `MEDPLUM_BASE_URL` | `seed_medplum.py` | `https://api.medplum.com/` | Medplum server |
| `MEDPLUM_CLIENT_ID`, `MEDPLUM_CLIENT_SECRET` | `seed_medplum.py` | unset | Client-credentials login for seeding |
| `JH_DATA_DIR` | `notebooks/deid_oura_data_exploration.py` | `../../de-identified data` (relative to `notebooks/`) | Data folder for the exploration notebook |
| `JH_DATA_FILE` | `notebooks/he_trajectories_phenotypes.py`, `notebooks/data_overview.py` (which also honours `HE_DATA_FILE`) | the single `.rds` file in `../../de-identified data/` (relative to `notebooks/`) | Study file for the trajectories notebook |
| `HE_SPEECH_DIR` | `notebooks/speech_features_starter.py` | `../speech recordings/` (next to the repo) | Folder of voice recordings (`.wav`, `.flac`, `.ogg`; `.mp3`/`.m4a` need ffmpeg). Never inside the repo |
| `HE_SPEECH_OUTPUT_DIR` | `notebooks/speech_features_starter.py` | `notebook_outputs/` | Where `speech_features.csv` and its `.params.json` are written |

