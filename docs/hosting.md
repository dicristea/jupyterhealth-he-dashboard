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
| Notebooks tab | lists local `.ipynb` files, links to JupyterLab on `localhost` | explains that notebooks run locally; no file list, no localhost links |
| Cohort → Export → New notebook | writes an `.ipynb` to disk | hidden, `POST /api/cohort/notebook` returns 404 |
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

- `Dockerfile`: `python:3.13-slim`, installs only the web app's packages (pinned) plus gunicorn, copies `app.py`, `data.py`, `templates/`, `static/` and nothing else, runs as a non-root user, one gunicorn worker with a 120 s timeout (booting on the free plan's 0.1 CPU can take longer than gunicorn's default 30 s).
- `.dockerignore`: excludes everything except those four paths, so nothing else is even sent to the builder.
- `render.yaml`: one free web service in Oregon, health check on `/healthz` (answers without loading data), `HE_PUBLIC_DEMO=1`, a generated `FLASK_SECRET_KEY`, auto-deploy off (`autoDeployTrigger: "off"`).
- Click-by-click first deploy, troubleshooting and a message for testers: [render-quickstart.md](render-quickstart.md).

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

## What is and isn't exposed

Exposed to anyone with the URL (there is no login unless `DEMO_PASSWORD` is set):

- The 24 generated synthetic patients and every page built from them.
- The app's code paths, so the same things as the public repo: column names of the study file (in `data.py`), the measures and their definitions, and the HE / Child-Pugh grouping.
- Anything a visitor types into the MRN simulator is only echoed back to them, unless it matches a synthetic MRN.

Not exposed, because it never reaches the host:

- The `.rds` study file, the CSV cache, `exploration_outputs/`, any `HE_MRN_MAP`. They're outside the repo, outside the Docker build context, and demo mode never opens them.
- Notebooks (`.py` sources or `.ipynb` copies), scripts and tests: not copied into the image.
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
- [ ] Notebooks tab says "Not available in the public demo"; Cohort → Export has only "PDF report".
- [ ] `https://<url>/ehr/launch?iss=https://example.com` returns 404.
- [ ] `curl -sI https://<url>/ | grep -i x-robots-tag` shows `noindex, nofollow`.

## Not covered

- **Real authentication.** The demo is public by design. `DEMO_PASSWORD` (below) only keeps casual visitors out of a synthetic demo; it is one shared password, not per-user accounts, and is no basis for hosting real data.
- **Real data.** Hosting with study data needs IRB approval, authentication, and an institutional host. Demo mode exists so that is never done by accident.
