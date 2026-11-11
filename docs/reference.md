# Technical reference

How the dashboard is put together, how every measure is computed, and every setting. The [README](../README.md) has the overview and quickstart.

**Contents:** [Architecture](#architecture) · [Data flow](#data-flow) · [Routes](#routes) · [How the measures are computed](#how-the-measures-are-computed) · [Configuration](#configuration)

## Architecture

```
ehr-wearable-he-dashboard/
├── app.py                 Flask routes, SMART on FHIR launch, notebook export
├── data.py                loading, synthetic generator, derived measures, page payloads
├── templates/             Jinja pages (all extend base.html)
├── static/charts.js       hand-written SVG/canvas charts (no chart library, no build step)
├── static/style.css
├── scripts/
│   ├── generate_synthetic_data.py   writes synthetic_data/*.csv.gz
│   ├── render_notebooks.py          demo image build: runs the notebooks on synthetic data → read-only HTML
│   └── seed_medplum.py              creates synthetic FHIR Patients in Medplum
├── notebooks/*.py         exploratory notebooks, jupytext percent format (no outputs)
├── tests/                 pytest, runs on synthetic data only
├── docs/                  medplum.md, synthetic-data.md, hosting.md, jupyterlite.md
├── lite/, scripts/build_jupyterlite.sh   JupyterLite site (GitHub Pages, synthetic data)
├── Dockerfile, .dockerignore, render.yaml   public synthetic demo only
├── requirements-notebooks.txt   notebook stack for the Dockerfile's builder stage
└── requirements.txt
```

### Data flow

```
 study .rds (outside repo) ──Rscript + haven──▶ CSV cache <stem>.v2.csv (outside repo) ─┐
 synthetic CSV (HE_SYNTHETIC_FILE) ─────────────────────────────────────────────────────┼─▶ data.load()
 data.synthetic_cohort() in memory (no file / public demo) ─────────────────────────────┘      │
                                                                                                ▼
                    daily table + derived columns: night_rest_h, rest_mid_6pm, sri, grp_*  (lru_cache)
                                                                                                │
        patient_list() · patient_detail() · intraday() · cohort_summary() · cohort_compare()   │
                                                                                                ▼
                         app.py routes → Jinja templates / JSON → static/charts.js in the browser
```

1. **Source selection** (`data.load()`, controlled by `HE_DATA_SOURCE` and `HE_PUBLIC_DEMO`):

   | Mode | Reads |
   |---|---|
   | `HE_PUBLIC_DEMO=1` | always the in-memory cohort `synthetic_cohort()` (24 patients, seed 7). Refuses to start with `HE_DATA_SOURCE=study` or `HE_MRN_MAP` |
   | `HE_DATA_SOURCE=synthetic` | `HE_SYNTHETIC_FILE` (error if missing) |
   | `HE_DATA_SOURCE=study` | `HE_DATA_FILE` (error if missing) |
   | `HE_DATA_SOURCE=auto` (default) | `HE_DATA_FILE` if it exists, otherwise the in-memory synthetic cohort |

2. **Raw file → cache** (study data only). Python's `pyreadr` can't parse the `.rds` file's haven-labelled and list columns, so `data._read_rds` writes a small R script (`R_TO_CSV` in `data.py`) to `HE_CACHE_DIR` and runs `Rscript` once. It converts labelled columns to factors, drops list columns, and turns per-minute MET (`met_val`, 1,440 values per day) into 288 five-minute means (`met_5min`, comma-separated, aligned with `class_5_min`). The CSV `<stem>.v2.csv` is reused until the `.rds` is newer. The cache sits outside the repo by default so a converted copy can't be committed by accident.
3. **Derived measures** are added to every row in `load()` (next section), then the table is cached in memory (`functools.lru_cache`). **Restart the app after the data changes.**
4. **Page payloads**: `patient_list()` (Patients tab, also cached), `patient_detail()` (profile), `intraday()` (MET explorer, `/api/patient/<id>/intraday`), `cohort_summary()` and `cohort_compare()` (Cohort tab, `/api/cohort`, PDF report).
5. **Cohort selections** travel as the Patients-tab filter parameters (defined in `data.FILTERS`, parsed by `data.parse_filters`), never as lists of patient IDs. Exported notebooks store the filters too, and re-select patients when run.

### Routes

| Route | Purpose |
|---|---|
| `GET /` | Patients tab (accepts the filter params) |
| `GET /patient/<id>` | Patient profile |
| `GET /api/patient/<id>/intraday?start=&days=` | 5-minute MET and activity class for 1–14 consecutive days |
| `GET /cohort`, `GET /api/cohort?group=&metric=` | Cohort tab and its JSON |
| `GET /cohort/report?group=` | Printable report of every measure (default group `decomp`) |
| `POST /api/cohort/notebook` | Writes a pre-filled `.ipynb` to the first `NOTEBOOK_DIRS` folder (local only) |
| `GET /api/cohort/notebook.ipynb?group=`, `GET /api/cohort/notebook/<token>/<name>.ipynb` | The same notebook as a download, never written to disk (also in the demo); the second form carries the filters base64url-encoded for JupyterLite's `fromURL`. CORS only for `NOTEBOOK_CORS_ORIGINS` |
| `GET /notebooks` | Notebooks tab |
| `GET /notebooks/view/<name>` | Read-only notebook page (public demo only; rendered on synthetic data when the image is built) |
| `GET /ehr`, `/ehr/launch`, `/ehr/callback`, `/ehr/simulate?mrn=` | SMART on FHIR launch (launch and callback are local only) |

## How the measures are computed

All of this is in `data.py`. The input is one row per participant-day. Oura's `class_5_min` is a 288-character string, one character per 5-minute slot of a day that runs **04:00 → 04:00** local: `0` ring off, `1` rest, `2` inactive, `3` low, `4` medium, `5` high activity. A string with the wrong length or other characters is treated as missing. The file has no sleep stages, so the three sleep measures are **proxies** built from the rest class.

| Measure | How it's computed |
|---|---|
| **Valid day** | `valid80 == 1` (ring worn ≥ 80% of the day; the synthetic generator sets it when `non_wear_pct ≤ 0.2`). Every per-patient and cohort summary uses valid days only. |
| **Night rest** (`night_rest_h`, hours) | Join day *d* and day *d+1*, take the window **18:00 on day *d* → 12:00 on day *d+1*** (18 h). Find rest bouts: runs that start and end with a rest slot, where gaps of at most 2 non-rest slots (≤ 10 minutes, any class including ring off) are bridged. Take the longest (the first one on a tie). Bouts under 2 h, and any value outside 2–16 h, are missing. Computed only when days *d* and *d+1* are consecutive study days, both are valid and both strings are well formed. Stored on day *d*. |
| **Rest midpoint** (`rest_mid_6pm`) | Middle of that same bout, in hours after 18:00; shown as clock time (e.g. `03:15`). Has no "better" direction. |
| **Rest regularity** (`sri`, −100 to 100) | Adapted from the Sleep Regularity Index. Compare day *d* and day *d+1* slot by slot at the same clock time, using only slots where the ring was on (class ≠ 0) on both days. Needs at least 80% of the 288 slots. SRI = 200 × (share of those slots where both days agree on rest vs. not rest) − 100. 100 = identical days, 0 = the days agree on half the slots. Needs consecutive days with well-formed strings; stored on day *d*, and summaries then keep only valid days *d*. |
| **HRV, average HR, steps, SpO₂** | Taken from the file (`average_hrv`, `average_heart_rate`, `steps`, `spo2`). `average_heart_rate == 0` is a device error and becomes missing. |
| **MET** | Study data: per-minute MET → five-minute means (see Architecture). The **24-hour activity profile** shows, for each 5-minute slot, the 10th/25th/50th/75th/90th percentile of MET across the patient's valid days, plus the share of valid days the slot was rest. **Time in activity classes** is the share of slots per class and the average hours per day, over valid days. The **activity explorer** shows raw 5-minute MET and class for 1, 3 or 7 consecutive days. |
| **Measure cards** (profile) | For each measure in `data.PATIENT_TILES`: *last 7* = median of the patient's last 7 valid-day values (the last 7 non-missing values, not the last 7 calendar days); *usual range* = the patient's own 25th–75th percentile over all valid days; status = below / typical / above. **Trend** (needs ≥ 28 values): median of the last 14 values vs. the 28 before; the difference divided by the usual-range width (IQR) above +0.5 is *Increasing*, below −0.5 *Decreasing*, otherwise *Stable*. |
| **Needs attention** (Patients tab) | For each measure in `PATIENT_TILES` with at least 14 valid-day values: flag it when the median of the last 7 values is outside the patient's own **10th–90th percentile** in the worse direction (lower is worse for night rest, regularity, HRV, steps and SpO₂; higher is worse for heart rate; rest midpoint never flags). Stricter than the cards' 25–75% range so ordinary weeks don't trigger it. A patient needs attention if any measure is flagged. |
| **Ring wear coverage** (profile) | Share of study days 1…last day that are valid (days missing from the file count as not valid), plus the median daily wear `1 − non_wear_pct`. The Patients-tab "valid days %" and the valid-days filter use the share of rows in the file instead. |
| **HE status** | `Overt` if `anyOHE == 1`, else `Covert` if `che_phes == "Yes"`, else `No` if `anyhe == 0`, otherwise unknown. Clinical fields are baseline only (first row per patient). |
| **Patient-list values** | Medians over valid days. The sparkline is the 7-day rolling mean of night rest (at least 3 values), every third day. |
| **Cohort summary** | Night rest and regularity are the median of per-patient medians, so each patient counts once. |
| **Cohort comparison** | Valid days only. **Trajectory**: each patient's mean per study week (week = (day − 1) // 7 + 1), then the median and 25th–75th percentile across patients, for weeks with ≥ 5 patients in the group. **Dots**: each patient's median across valid days. **Test**: two-sided Mann-Whitney U on those per-patient medians (`scipy.stats.mannwhitneyu`) when both groups have ≥ 3 patients; effect size r = 1 − 2U / (n₁·n₂) (rank-biserial). Exploratory and unadjusted. |

## Configuration

Every environment variable the code reads:

| Variable | Read by | Default | Meaning |
|---|---|---|---|
| `HE_DATA_SOURCE` | `data.py`, both notebooks | `auto` | `auto`, `study` or `synthetic` (see [Data flow](#data-flow)) |
| `HE_DATA_FILE` | `data.py` | the single `.rds` file in `../de-identified data/` (next to the repo) | Study `.rds` file. With no file, or several, there, set it explicitly |
| `HE_SYNTHETIC_FILE` | `data.py`, `seed_medplum.py` | `./synthetic_data/synthetic_he.csv.gz` | CSV read when `HE_DATA_SOURCE=synthetic` |
| `HE_CACHE_DIR` | `data.py` | `<parent of the data folder>/exploration_outputs/_cache` | Where the `.rds` → CSV cache is written |
| `HE_PUBLIC_DEMO` | `data.py`, `app.py` | off | `1`/`true`/`yes`: public synthetic demo ([docs/hosting.md](hosting.md)) |
| `DEMO_PASSWORD`, `DEMO_USER` | `app.py` | unset, `demo` | Optional HTTP basic auth on every route except `/healthz` ([docs/hosting.md](hosting.md)) |
| `HE_MRN_MAP` | `data.py`, `app.py` | unset | CSV `mrn,study_id` **outside the repo**, for real patients only after IRB approval and authentication. Refused in demo mode |
| `PORT` | `app.py`, `Dockerfile` | `5050` (`8000` in the container) | Port |
| `HOST` | `app.py` | `127.0.0.1` | Bind address, **only honoured when `HE_PUBLIC_DEMO=1`**; otherwise always `127.0.0.1` |
| `FLASK_DEBUG` | `app.py` | `1` | Flask debugger/reloader for local runs; always off in demo mode |
| `FLASK_SECRET_KEY` | `app.py` | random per process | Session signing key (sessions reset on restart without it) |
| `JUPYTER_BASE_URL` | `app.py` | `http://localhost:8888` | JupyterLab URL for notebook links |
| `JUPYTER_ROOT` | `app.py` | the repo folder | Folder JupyterLab was started from (links are relative to it) |
| `NOTEBOOK_DIRS` | `app.py` | `./notebooks` | Folders (separated by `:`, `;` on Windows) listed in the Notebooks tab; exports go to the first |
| `NOTEBOOK_REPO`, `NOTEBOOK_REF` | `app.py` | `dicristea/ehr-wearable-he-dashboard`, `main` | GitHub repo and branch the demo Notebooks tab and Cohort export link to on Binder (and for docs links) |
| `JUPYTERLITE_URL` | `app.py` | `https://dicristea.github.io/ehr-wearable-he-dashboard` | JupyterLite site the demo Notebooks tab and Cohort export (`?fromURL=`) link to |
| `NOTEBOOK_CORS_ORIGINS` | `app.py` | `https://dicristea.github.io` | Comma-separated origins allowed to fetch exported notebooks cross-origin (JupyterLite) |
| `SMART_CLIENT_ID` | `app.py` | unset | Client ID from the EHR's app registration |
| `SMART_CLIENT_SECRET` | `app.py` | unset | Client secret (optional: PKCE is always used) |
| `SMART_REDIRECT_URI` | `app.py` | `<this app>/ehr/callback` | Must exactly match the registered redirect URI |
| `SMART_SCOPES` | `app.py` | `launch openid fhirUser patient/Patient.rs` | Requested scopes |
| `SMART_MRN_SYSTEM` | `data.py`, `seed_medplum.py` | `https://jupyterhealth.org/fhir/identifier/mrn` | FHIR identifier system for the MRN |
| `MEDPLUM_BASE_URL` | `seed_medplum.py` | `https://api.medplum.com/` | Medplum server |
| `MEDPLUM_CLIENT_ID`, `MEDPLUM_CLIENT_SECRET` | `seed_medplum.py` | unset | Client-credentials login for seeding |
| `JH_DATA_DIR` | `notebooks/deid_oura_data_exploration.py` | `../../de-identified data` (relative to `notebooks/`) | Data folder for the exploration notebook |
| `JH_DATA_FILE` | `notebooks/he_trajectories_phenotypes.py` | the single `.rds` file in `../../de-identified data/` (relative to `notebooks/`) | Study file for the trajectories notebook |

