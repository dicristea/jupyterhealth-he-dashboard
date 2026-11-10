# Render quickstart: first deploy of the public demo

The click path for putting the synthetic demo online on Render's free plan, from nothing to a working URL. Background (what demo mode hides, the pre-publication checklist) is in [hosting.md](hosting.md). Allow about 20 minutes, most of it waiting for the first build.

Before you start: the GitHub repo you connect must be the one that's safe to make public (hosting.md, "Checklist: no study data can reach the host"), and `render.yaml`, `Dockerfile` and `.dockerignore` must be in its root on the branch you deploy (`main`).

## 1. Create the service

1. Go to <https://render.com> → **Get Started** → **GitHub**. Approve the Render app on GitHub; when GitHub asks which repositories, choose **Only select repositories** and pick the demo repo.
2. In the Render dashboard: **+ New** (top right) → **Blueprint**.
3. Pick the repo from the list (if it's missing: **Configure account** / **Connect GitHub** and grant access to it). Branch: `main`.
4. Give the Blueprint a name, e.g. `he-dashboard-demo`. Render reads `render.yaml` and lists one resource: web service `he-dashboard-synthetic-demo`, plan **Free**, region Oregon. It shows `HE_PUBLIC_DEMO` and `FLASK_SECRET_KEY` (generated); nothing to fill in.
5. Click **Deploy Blueprint** (older UI: **Apply**).

## 2. Wait for the build

1. Click through to the service (**Dashboard → he-dashboard-synthetic-demo**). The **Events** tab shows "Deploy started"; **Logs** shows the build.
2. The first build takes about 3 to 6 minutes: pulling `python:3.13-slim`, then `pip install` of six pinned packages (all prebuilt wheels, nothing compiles).
3. Success in the logs looks like:

   ```
   [INFO] Starting gunicorn 27.0.0
   [INFO] Listening at: http://0.0.0.0:10000
   [INFO] Using worker: gthread
   [INFO] Booting worker with pid: …
   "GET /healthz HTTP/1.1" 200 2
   ==> Your service is live 🎉
   ```

   The status badge at the top turns green: **Live**.

## 3. Open it and check

The URL is at the top of the service page: `https://he-dashboard-synthetic-demo.onrender.com` (Render adds a suffix like `-abcd` if the name is taken).

What success looks like:

- `/healthz` shows `ok`.
- Every page has the amber "Public demo. Every patient here is synthetic" banner; the nav reads "Public demo · synthetic data only".
- Patients tab: 24 patients, `DEMO-01` … `DEMO-24`.
- A profile (`/patient/DEMO-01`) shows tiles, trend charts and the activity explorer.
- Cohort tab draws the comparison charts; **Export → PDF report** opens a printable page. There's no "New notebook" button.
- Notebooks tab says "Not available in the public demo".
- EHR tab: clicking a `JH-DEMOxx` button opens that patient with the "Opened from the EHR" banner.
- `https://<url>/ehr/launch?iss=https://example.com` gives 404.

Then run the "After deploying" checks at the end of hosting.md.

## 4. Optional: put a password on it

1. Service → **Environment** → **+ Add Environment Variable**.
2. Key `DEMO_PASSWORD`, value: the password. Optionally a second one, `DEMO_USER` (default user name is `demo`).
3. **Save, rebuild, and deploy** (or **Save and deploy**). After about a minute the browser asks for a user name and password on every page. `/healthz` stays open so Render's health check still works.
4. To make it open again, delete `DEMO_PASSWORD` and save.

Don't add the password to `render.yaml`: the repo is public.

## 5. Redeploy after a change

Auto-deploy is off on purpose. Push to `main`, then on the service page: **Manual Deploy → Deploy latest commit**. Watch **Logs** as above. If a deploy fails, Render keeps the previous version live.

## 6. Read the logs

Service → **Logs**. Build output and the running app's output are in one stream; each request appears as a gunicorn access-log line. Use the search box (e.g. `ERROR`, `Traceback`, `WORKER TIMEOUT`). **Events** lists each deploy and why it failed.

The free service sleeps after 15 minutes without visitors. The first visit afterwards shows Render's loading screen for up to a minute; that's normal, not a failure.

## 7. The three most likely problems

1. **Render asks for a credit card, or the plan shows "Starter" ($7/month).** Check the plan column says **Free** before deploying (it comes from `plan: free` in `render.yaml`). Render sometimes asks for a card just to verify the account, even for free services; adding one doesn't charge anything on the free plan. If you'd rather not use the Blueprint at all: **+ New → Web Service** → the repo → Language **Docker** → Instance type **Free** → Advanced: Health Check Path `/healthz`, add env var `HE_PUBLIC_DEMO=1` and `FLASK_SECRET_KEY` (**Generate**) → **Deploy Web Service**.
2. **"No render.yaml found" or the Blueprint shows no services.** The file must be named exactly `render.yaml`, sit in the repo root, and be on the branch you picked. Push it, then **Blueprints → (your blueprint) → Manual Sync**.
3. **Deploy fails after the build: "Timed out", "No open ports detected", or repeated `WORKER TIMEOUT` / `Booting worker` lines.** The app must answer on `0.0.0.0:$PORT`; the Dockerfile's start command already does this, so check nobody changed it or set a **Docker Command** override in Settings (clear it). Look further up in **Logs** for a Python `Traceback`: a `RuntimeError: HE_PUBLIC_DEMO=1 serves synthetic data only` means someone added `HE_DATA_SOURCE=study` or `HE_MRN_MAP` under Environment; delete them. If the free CPU is so slow that boot still times out, raise `--timeout 120` in the Dockerfile's `CMD`, push, and redeploy.

Less likely: "Ran out of memory (used over 512MB)". In a local test the app used about 190 MB after opening every page, so this would point at a code change, not the plan.
