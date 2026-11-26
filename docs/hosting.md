# Hosting the public synthetic demo

A hosted copy lets collaborators click through the dashboard without installing anything. **It only ever shows generated synthetic patients.** The local app (study data, `127.0.0.1`, no login) doesn't change.

## How demo mode works

`HE_PUBLIC_DEMO=1` turns the app into a public demo:

| | Local (default) | Public demo |
|---|---|---|
| Data | study file if present, otherwise synthetic | always the in-memory synthetic cohort (`DEMO-01`…`DEMO-24`, built from a fixed seed) |
| `HE_DATA_FILE`, `HE_SYNTHETIC_FILE` | read | ignored, never opened |
| `HE_DATA_SOURCE=study`, `HE_MRN_MAP` | used | the app refuses to start |
| Binds to | `127.0.0.1` only | `HOST` / `PORT` from the environment (gunicorn binds `0.0.0.0:$PORT` in the container) |
| Banner | amber "synthetic demo data" when there's no study file | no banner; the nav shows a "Public demo · synthetic data" pill whose tooltip says every patient is synthetic |
| Notebooks tab | lists local `.ipynb` files, links to JupyterLab on `localhost` | one card per notebook: **View (read-only)** (`/notebooks/view/<name>`, pages pre-rendered when the image was built, see below), **Run in your browser** (JupyterLite, `JUPYTERLITE_URL`) and **Run on Binder** (`NOTEBOOK_REPO`, `NOTEBOOK_REF`); all on synthetic data, none of it runs on this server; no localhost links |
| Cohort → Export → New notebook | writes an `.ipynb` to disk and opens local JupyterLab | hidden, `POST /api/cohort/notebook` returns 404 |
| Cohort → Export → Download notebook | `GET /api/cohort/notebook.ipynb`: same notebook as a download, nothing written to disk | same; the notebook says it runs on synthetic data |
| Cohort → Export → Open in browser (JupyterLite) | not shown | opens the notebook in JupyterLite via `?fromURL=` (see [Notebook export](#notebook-export-in-the-demo)) |
| Cohort → Export → PDF report | works | works (print to PDF in the visitor's browser) |
| EHR launch | real SMART launch + simulator | simulator only; `/ehr/launch` and `/ehr/callback` return 404 (they would fetch any URL a visitor passed as `iss`) |
| Recent launches log | every launch | only launches that matched a synthetic patient (the log is shared by all visitors) |
| Search engines | n/a | `X-Robots-Tag: noindex, nofollow` on every response |
| Flask debugger | on by default | off |
| Login | none | none, unless `DEMO_PASSWORD` is set (see below) |
| Internal notes (meeting refs on the EHR tab, "De-identified study data" on the PDF report) | shown | hidden |

The container sets `HE_PUBLIC_DEMO=1` in its start command, so a host setting can't turn it off.

## Host: Render (free web service, Docker)

Chosen because it builds straight from a GitHub repo with a `Dockerfile`, has a free tier, and needs no CLI. Limits of the free plan: the service sleeps after about 15 minutes without visitors and takes up to a minute to wake, with 512 MB of RAM (enough: the app uses pandas and scipy on 24 synthetic patients). Fly.io or Google Cloud Run would run the same `Dockerfile` unchanged.

Files:

- `Dockerfile`: two stages (next section). The runtime stage is `python:3.13-slim` with only the web app's packages (pinned) plus gunicorn, copies `app.py`, `data.py`, `templates/`, `static/` and the rendered notebook pages and nothing else, runs as a non-root user, one gunicorn worker with a 120 s timeout (booting on the free plan's 0.1 CPU can take longer than gunicorn's default 30 s).
- `.dockerignore`: excludes everything except those paths plus what the builder stage needs (`requirements-notebooks.txt`, `scripts/generate_synthetic_data.py`, `scripts/render_notebooks.py`, `notebooks/*.py`), so nothing else is even sent to the builder.
- `render.yaml`: one free web service in Oregon, health check on `/healthz` (answers without loading data), `HE_PUBLIC_DEMO=1`, a generated `FLASK_SECRET_KEY`, auto-deploy off (`autoDeployTrigger: "off"`).
- Click-by-click first deploy, troubleshooting and a message for testers: [render-quickstart.md](render-quickstart.md).

### Build stages: what runs where

| Stage | Runs | Has | Leaves behind |
|---|---|---|---|
| 1 `notebooks` (builder, on Render's build machine) | `generate_synthetic_data.py` (default seed 7), then `scripts/render_notebooks.py`: each `notebooks/*.py` → jupytext → executed with nbclient (10 min timeout each, cwd `notebooks/`) → nbconvert `lab` HTML with prompts hidden, code shown, images embedded, every `<script>` removed (so no CDN calls), and a "Read-only. Run on synthetic data…" banner on top | `data.py`, the two scripts, `notebooks/*.py`, the pinned notebook stack (`requirements-notebooks.txt`: matplotlib, scikit-learn, jupytext, nbclient, nbconvert, ipykernel); `HE_DATA_SOURCE=synthetic`, `HE_PUBLIC_DEMO=1` | `rendered_notebooks/*.html` (1–2 MB each) |
| 2 runtime (the web service) | gunicorn only; it never runs a notebook | app code, the web app's packages, `rendered_notebooks/` copied from stage 1 | |

`render_notebooks.py` refuses to run unless `HE_DATA_SOURCE=synthetic`, if any of `HE_DATA_FILE`, `HE_SYNTHETIC_FILE`, `HE_MRN_MAP`, `HE_CACHE_DIR`, `JH_DATA_FILE`, `JH_DATA_DIR` is set, or if a `.rds` file sits in the default study folder (`../de-identified data/` next to the repo). A refusal fails the build. **A notebook that fails to run gets no page and the build still succeeds**: its View page says "Not available" (404), the rest of the demo deploys, and the build log has a `FAILED <name>` line with the error. One broken notebook shouldn't block a dashboard deploy; `--strict` exits 1 on any failure if that's ever preferred. Rendered pages are build output and are ignored by Git; never commit them.

How they're served: `/notebooks/view/<name>` (names allowlisted from `DEMO_NOTEBOOKS` in `app.py`) is a dashboard page with the notebook in an `<iframe sandbox>`. The iframe loads `/notebooks/view/<name>/page`, the raw HTML, sent with `Content-Security-Policy: default-src 'none'; img-src data:; style-src 'unsafe-inline'; … sandbox`, so it runs no scripts and makes no requests even when opened directly. In local mode both routes return 404 with a note: local notebook outputs can come from study data, so the dashboard never shows them.

To run stage 1 by hand (in a scratch copy with no study data next to it):

```bash
pip install -r requirements-notebooks.txt
python scripts/generate_synthetic_data.py
HE_DATA_SOURCE=synthetic python scripts/render_notebooks.py   # → rendered_notebooks/*.html
```

Measured without Docker (Apple silicon, Python 3.13, the pinned versions): rendering took 9 s and 7 s (1.9 MB and 1.2 MB of HTML). Render's build machine is slower per core but has far more CPU than the 0.1 CPU runtime. The gunicorn worker used 130–150 MB RSS after boot and at most about 165 MB after serving both notebook pages and every other page (serving a notebook page adds about 5 MB), well under the free plan's 512 MB.

### Steps (a person does these; nothing here has been deployed)

1. Publish the public repo first (see the checklist below) and deploy from **that** repo, not the private one.
2. Sign in at render.com with GitHub, **New → Blueprint**, pick the public repo. Render reads `render.yaml` and proposes one web service.
3. Apply. The first build takes a few minutes. Open the `onrender.com` URL and check the checklist's last section.
4. Later updates: auto-deploy is off, so press **Manual Deploy** after reviewing what changed.

To try the image locally (needs Docker):

```bash
docker build -t he-demo .
docker run --rm -p 127.0.0.1:8000:8000 he-demo   # → http://127.0.0.1:8000
```

Without Docker, the same mode runs with `HE_PUBLIC_DEMO=1 python app.py` (still on `127.0.0.1` unless you set `HOST`).

## Optional shared password

Off by default. To keep the demo to people you send the password to, set environment variables on the host (Render: service → **Environment**):

- `DEMO_PASSWORD`: turns on the login page (`/login`) for every page and API. Pages redirect to it; APIs answer 401. After logging in, a signed session cookie (HttpOnly, SameSite=Lax, Secure on the host, 12 h) keeps you in; `/logout` ends it. A wrong password waits 1 s before answering. Changing `DEMO_USER`, `DEMO_PASSWORD` or `FLASK_SECRET_KEY` logs everyone out.
- `DEMO_USER`: the user name to type, default `demo`.

`/healthz`, `/login`, `/logout`, `/robots.txt` and `/static/` (CSS and chart code, public anyway) stay open, so Render's health check keeps working and the login page has its styles. The comparison is constant-time (`hmac.compare_digest`). It replaced HTTP basic auth because some browsers (embedded previews, Little Arc) never show the basic-auth box, and JupyterLite's background requests don't carry it reliably. A local `http://` run of a hosted mode needs `HE_COOKIE_INSECURE=1`, or the browser drops the Secure cookie. Don't put the password in `render.yaml` (the repo is public); remove the variable to make the demo open again. Setting it locally works the same way.

## Notebook export in the demo

**Cohort → Export → Download notebook** returns the cohort notebook built in memory (`application/x-ipynb+json`, `Content-Disposition: attachment; filename=cohort-<filters>.ipynb`); demo mode never writes it to disk. Like the local export it stores the Patients-tab filters (as a Python dict literal built with `repr`, so a typed filter value can't become code), not patient IDs, and its first cell finds `data.py` one folder up (`notebooks/`) or next to it. It runs unchanged in a clone of the repo, in Binder and in JupyterLite: `data.load()` finds no study file there and builds the same 24 synthetic patients the demo shows. To open the download, upload it into the `notebooks/` folder of JupyterLite or Binder (the Export menu links both).

**Open in browser (JupyterLite)** opens `<JUPYTERLITE_URL>/lab/index.html?fromURL=<notebook URL>&fromURLToFolder=notebooks`. JupyterLite names the fetched file after the URL's last path segment, query string included, so this URL carries the filters base64url-encoded in the path: `/api/cohort/notebook/<token>/cohort-<filters>.ipynb`. The browser fetches it cross-origin, so both notebook routes send `Access-Control-Allow-Origin` only to origins in `NOTEBOOK_CORS_ORIGINS` (comma-separated, default `https://dicristea.github.io`). Requests with more than 2,000 bytes of filters get 414.

Verify after GitHub Pages is live (not testable offline): the link opens the notebook in `notebooks/` in JupyterLite (the Lite build includes `jupyterlab-open-url-parameter`, which provides `fromURL` and `fromURLToFolder`; tested against a local build, [jupyterlite.md](jupyterlite.md)) and the first cell prints `24 patients (data: synthetic)`. With `DEMO_PASSWORD` set the cross-origin fetch has no credentials and fails, so use the download there. If the link misbehaves, the download is the primary path and keeps working.

| Variable | Default | |
|---|---|---|
| `JUPYTERLITE_URL`, `NOTEBOOK_REPO`, `NOTEBOOK_REF` | as for the Notebooks tab | JupyterLite site and Binder repo the Export menu links to |
| `NOTEBOOK_CORS_ORIGINS` | `https://dicristea.github.io` | Origins allowed to fetch exported notebooks |

## What is and isn't exposed

Exposed to anyone with the URL (there is no login unless `DEMO_PASSWORD` is set):

- The 24 generated synthetic patients and every page built from them.
- The app's code paths, so the same things as the public repo: column names of the study file (in `data.py`), the measures and their definitions, and the HE / Child-Pugh grouping.
- Anything a visitor types into the MRN simulator is only echoed back to them, unless it matches a synthetic MRN.

Not exposed, because it never reaches the host:

- The `.rds` study file, the CSV cache, `exploration_outputs/`, any `HE_MRN_MAP`. They're outside the repo, outside the Docker build context, and demo mode never opens them.
- Notebook sources (`.py`, `.ipynb`), scripts and tests: not copied into the runtime image. Only the HTML of the notebooks as run on synthetic data at build time is.
- EHR credentials: `SMART_CLIENT_ID` / `SMART_CLIENT_SECRET` aren't set on the host, and the real launch routes are off.

## Checklist: no study data can reach the host

Before publishing the repo:

- [ ] Publish from a fresh single-commit history, or re-run the history audit (`git rev-list --all --objects` for data file types, `git log --all -p` for paths, IDs and secrets) and confirm it still finds nothing.
- [ ] `git ls-files` lists only code, templates, docs and the two jupytext `notebooks/*.py` (no `.rds`, `.csv*`, `.parquet`, `.ipynb`, `.pdf`, images).
- [ ] `git grep -nE "/Users/|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}"` finds no local paths or personal emails.

Before deploying:

- [ ] The host builds from the **public** repo.
- [ ] Render's environment has no `HE_DATA_FILE`, `HE_DATA_SOURCE=study`, `HE_MRN_MAP`, `SMART_CLIENT_ID` or `SMART_CLIENT_SECRET`. (`HE_DATA_SOURCE=study` or `HE_MRN_MAP` would stop the app from starting anyway.)
- [ ] No disk is attached to the service (the blueprint adds none).

After deploying, on the live URL:

- [ ] Every page shows the "Public demo · synthetic data" pill in the nav, and hovering it explains that every patient is synthetic.
- [ ] Patient IDs are all `DEMO-xx`; the Patients tab shows 24 patients.
- [ ] Notebooks tab shows both notebooks; **Run on Binder** builds and opens the notebook (first build takes minutes), **Run in your browser** opens it in JupyterLite, and both run top to bottom on synthetic data.
- [ ] **View (read-only)** shows each notebook inside the dashboard with the amber "Read-only. Run on synthetic data…" banner and its charts. If one says "Not available", look for `FAILED` in the build log.
- [ ] Cohort → Export has "PDF report", "Download notebook (.ipynb)" and "Open in browser (JupyterLite)", and no "New notebook".
- [ ] `curl -s -D- -o /dev/null -H 'Origin: https://dicristea.github.io' 'https://<url>/api/cohort/notebook.ipynb?group=decomp'` shows `Content-Disposition: attachment; filename="cohort-all-patients.ipynb"` and `Access-Control-Allow-Origin: https://dicristea.github.io`; with any other `Origin` there is no `Access-Control-Allow-Origin`.
- [ ] `https://<url>/ehr/launch?iss=https://example.com` returns 404.
- [ ] `curl -sI https://<url>/ | grep -i x-robots-tag` shows `noindex, nofollow`.

## Hosted de-identified site

A second, separate Render service shows the **de-identified study data** to the study team behind a shared password. The study PI approved this on 2026-11-11: the de-identified data is low sensitivity (the IRB requires it to be open-sourced eventually), but it is still kept off public pages. Everything below is for that service only; the public demo above is unchanged and stays synthetic.

How it differs from the demo:

| | Public demo (`Dockerfile`) | Hosted study (`Dockerfile.deid`) |
|---|---|---|
| Mode | `HE_PUBLIC_DEMO=1` | `HE_HOSTED_STUDY=1` (forced in the image's `CMD`; the two can't both be on) |
| Data | in-memory synthetic cohort | downloaded once at start-up from `HE_DATA_URL` into the container's `/tmp`; never in the image or in git |
| Password | optional | **required**: the app refuses to start without `DEMO_PASSWORD`, and answers 503 if it is somehow missing at request time |
| Notebooks tab | read-only pages, JupyterLite, Binder (all synthetic) | **this site's own JupyterLite** at `/lite/` (behind the login) on the study data, plus a blank download of each notebook; Cohort → Export → Open in browser uses the same Lite. No Binder (a public cloud) and no read-only pre-run pages (there is no data at build time, and running notebooks at start-up is too heavy for 512 MB) |
| Local-only features (JupyterLab export, real EHR launch, `HE_MRN_MAP`) | off | off |
| Search engines | `X-Robots-Tag: noindex, nofollow`, `/robots.txt` disallows all | same |
| Built from | public repo | **private repo** `dicristea/jupyterhealth-he-dashboard-deid` |

### 1. Put the data where the service can fetch it

The app reads the converted cache (`<stem>.v3.csv`, made by `data._read_rds` the first time you run the app locally on the `.rds`), gzipped. It never needs R.

```bash
# from the folder that holds exploration_outputs/ (outside any repo)
gzip -c "exploration_outputs/_cache/<stem>.v3.csv" > /tmp/study.v3.csv.gz
gh release create data-2026-11-17 /tmp/study.v3.csv.gz --repo dicristea/jupyterhealth-he-dashboard-deid \
  --title "Study data (de-identified, Nov 17 2026)" --notes "Release asset only. Never commit data."
gh api repos/dicristea/jupyterhealth-he-dashboard-deid/releases/tags/data-2026-11-17 --jq '.assets[0].url'
rm /tmp/study.v3.csv.gz
```

The last command prints the asset API URL (`https://api.github.com/repos/dicristea/jupyterhealth-he-dashboard-deid/releases/assets/<id>`): that is `HE_DATA_URL`. The release is private because the repo is. A release creates a git tag, but the file is not in the repository's history.

Then make a token the service uses to download it: GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate: repository access *Only select repositories* → `jupyterhealth-he-dashboard-deid`; permissions **Contents: Read-only**, nothing else; expiry at the end of the semester. That is `HE_DATA_TOKEN`. The app sends it only to `api.github.com` (not to the storage host GitHub redirects to) and never logs it or the URL's query string.

When the data changes (a new export from the study team, or a new cache version), make a new release, update `HE_DATA_URL` and redeploy. Delete old releases you no longer need.

### 2. Create the Render service

1. Render dashboard → **New** → **Web Service** → connect the GitHub repo **`dicristea/jupyterhealth-he-dashboard-deid`** (if it isn't listed, Render → Account settings → GitHub → configure the Render app to include it).
2. Name e.g. `he-dashboard-deid`; **Language: Docker**; branch `main`; region Oregon; **Instance type: Free**.
3. Advanced → **Dockerfile path: `./Dockerfile.deid`**; **Health check path: `/healthz`**; **Auto-Deploy: Off**.
4. Environment variables:

   | Key | Value |
   |---|---|
   | `HE_DATA_URL` | the asset API URL from step 1 |
   | `HE_DATA_TOKEN` | the fine-grained token |
   | `DEMO_USER` | the shared user name |
   | `DEMO_PASSWORD` | the shared password (only in Render, never in the repo or a chat) |
   | `FLASK_SECRET_KEY` | **Generate** |

5. **Create Web Service**. The log should show `Study data downloaded from https://api.github.com/... (<n> bytes)` and then gunicorn listening. A download failure logs the HTTP status and the deploy fails.

### 3. Check it

- [ ] `curl -s https://<url>/healthz` → `ok`; `curl -s -o /dev/null -w '%{http_code}' https://<url>/` → `302` (to `/login`); `curl -s -o /dev/null -w '%{http_code}' https://<url>/api/study-data.csv.gz` → `401`.
- [ ] In a browser, the login page appears; after logging in the nav says "De-identified study data · password protected" with **Log out**; the Patients tab shows the study's patient count, not `DEMO-xx`.
- [ ] Notebooks tab → **Run in your browser** on each notebook: JupyterLite opens (30–60 s the first time), **Run → Run All Cells** finishes, and the first cells print `Data source: study (from the study site)` and the study's participant count.
- [ ] `curl -sI https://<url>/healthz | grep -i x-robots-tag` → `noindex, nofollow`.

The free plan has 512 MB of memory and sleeps after 15 idle minutes; each wake-up downloads the data again (a few seconds) and recomputes the measures.

### Notebooks on this site (JupyterLite)

`Dockerfile.deid`'s first stage builds JupyterLite with `LITE_VARIANT=hosted-study scripts/build_jupyterlite.sh`: the two analysis notebooks (blank) and `data.py`, **no data at all** (not even synthetic), plus a `hosted_study.json` marker. The build refuses to finish if any `.csv`, `.csv.gz`, `.rds` or `.parquet` lands in it. The runtime image serves it from `lite_site/` at `/lite/` (about 19 MB of static files; the app's memory doesn't change). In the browser, `data.py` sees the marker and fetches `/api/study-data.csv.gz` (the downloaded cache, `Cache-Control: no-store`, logged-in visitors only) with the visitor's login cookie, then runs it through the same study loading, so `data.load()` and both notebooks report `data_source == "study"`. Details: [jupyterlite.md → Hosted study site](jupyterlite.md#hosted-study-site).

What this means for the data: anyone with the password can now pull the whole de-identified file into their browser (they could already see every patient's page). Edits and outputs live in that browser's storage only. The notebooks' outputs describe participants, so the team shouldn't share them outside the study. The speech notebook isn't included: its audio library (Praat) has no browser build.

## Not covered

- **Real authentication.** `DEMO_PASSWORD` is one shared password and a session cookie, not per-user accounts or audit logs. It is enough for de-identified data the PI has approved sharing with the team, and no basis for identifiable data.
- **Identifiable data.** Hosting anything identifiable (names, MRNs, dates) needs IRB approval, real authentication and an institutional host. Neither hosted mode reads `HE_MRN_MAP`.
