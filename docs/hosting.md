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

- `DEMO_PASSWORD`: turns on HTTP basic auth for every page, API and static file. The browser shows its own login box.
- `DEMO_USER`: the user name to type, default `demo`.

`/healthz` stays open so Render's health check keeps working. The comparison is constant-time (`hmac.compare_digest`). Don't put the password in `render.yaml` (the repo is public); remove the variable to make the demo open again. Setting it locally works the same way.

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

## Not covered

- **Real authentication.** The demo is public by design. `DEMO_PASSWORD` (below) only keeps casual visitors out of a synthetic demo; it is one shared password, not per-user accounts, and is no basis for hosting real data.
- **Real data.** Hosting with study data needs IRB approval, authentication, and an institutional host. Demo mode exists so that is never done by accident.
