"""HE Clinician Dashboard: a simpler, clinician-facing view of the HE study's Oura data.

Three tabs: Patients (list → personal profile), Cohort, Notebooks.
Runs on 127.0.0.1 with no login by default. Two hosted modes (docs/hosting.md), both with local-only features off:
HE_PUBLIC_DEMO=1 serves synthetic data only; HE_HOSTED_STUDY=1 serves the de-identified study data downloaded
from HE_DATA_URL and refuses to start without DEMO_PASSWORD.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import mimetypes
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import nbformat
from flask import (Flask, Response, abort, jsonify, redirect, render_template, request, send_file, send_from_directory,
                   session, url_for)
from markupsafe import Markup, escape
from werkzeug.middleware.proxy_fix import ProxyFix

import data

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

HERE = Path(__file__).resolve().parent
JUPYTER_BASE_URL = os.environ.get("JUPYTER_BASE_URL", "http://localhost:8888")
# Folders whose .ipynb files show in the Notebooks tab, relative to where JupyterLab is started (JUPYTER_ROOT).
JUPYTER_ROOT = Path(os.environ.get("JUPYTER_ROOT", HERE)).expanduser().resolve()
NOTEBOOK_DIRS = [Path(p).expanduser().resolve() for p in
                 os.environ.get("NOTEBOOK_DIRS", str(HERE / "notebooks")).split(os.pathsep) if p]
# Public demo Notebooks tab: links to the public repo's notebooks on Binder and JupyterLite (docs/hosting.md).
NOTEBOOK_REPO = os.environ.get("NOTEBOOK_REPO", "dicristea/jupyterhealth-he-dashboard")
NOTEBOOK_REF = os.environ.get("NOTEBOOK_REF", "main")
JUPYTERLITE_URL = os.environ.get("JUPYTERLITE_URL", "https://dicristea.github.io/jupyterhealth-he-dashboard").rstrip("/")
# Written out rather than globbed: the Docker image holds no notebooks/ folder, and a test keeps this in step with it.
DEMO_NOTEBOOKS = [
    {"name": "data_overview", "title": "What's in the newest de-identified file",
     "description": "A plain-language inventory: how many people and days, what every column means (with units), "
                    "how complete each column is, and what the file does not contain. Aggregates only."},
    {"name": "deid_oura_data_exploration", "title": "Getting to know the dataset",
     "description": "A beginner-friendly tour: who is in the cohort, how complete each patient's data is, "
                    "and what the sleep and activity measures look like."},
    {"name": "he_trajectories_phenotypes", "title": "Trajectories and phenotypes",
     "description": "How groups of patients (for example decompensated vs. compensated) compare across the "
                    "study days, and whether patients cluster into a few recognisable types."},
]
# Shared password (docs/hosting.md). Optional for the synthetic demo, required for the hosted study site.
DEMO_USER = os.environ.get("DEMO_USER", "demo")
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "")
if data.HOSTED_STUDY and not DEMO_PASSWORD:  # checked before the download, so no data arrives on an open server
    raise RuntimeError("HE_HOSTED_STUDY=1 serves study data and refuses to start without DEMO_PASSWORD")
if data.hosted():
    # Render terminates HTTPS in front of the app; trust its one proxy hop so redirects and cookies see https.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    data.load()  # a misconfigured host (wrong source, MRN map, failed download) should fail the deploy, not a visitor
app.config.update(
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    # Hosted sites are HTTPS-only; HE_COOKIE_INSECURE=1 lets a local http:// run of a hosted mode keep its login.
    SESSION_COOKIE_SECURE=data.hosted() and os.environ.get("HE_COOKIE_INSECURE", "") != "1")
OPEN_PATHS = {"/healthz", "/login", "/logout", "/robots.txt"}  # the host's health check can't log in


def _login_token() -> str:
    """What a logged-in session holds: tied to the current user, password and secret key, so changing any of them
    logs everyone out."""
    return hmac.new(app.secret_key.encode(), f"{DEMO_USER}:{DEMO_PASSWORD}".encode(), "sha256").hexdigest()


def _logged_in() -> bool:
    return hmac.compare_digest(str(session.get("login", "")), _login_token())


def _safe_next(target: str | None) -> str:
    """Only a path on this site: no scheme, no host, no protocol-relative //, no backslashes."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


@app.before_request
def demo_password():
    if request.path in OPEN_PATHS or request.path.startswith("/static/"):
        return None
    if not DEMO_PASSWORD:
        if data.HOSTED_STUDY:  # fail closed, even if the start-up check was bypassed
            return Response("Not configured.", 503)
        return None
    if _logged_in():
        return None
    if request.path.startswith("/api/") or request.method != "GET":
        return Response("Login required.", 401, {"Cache-Control": "no-store"})
    return redirect(url_for("login", next=request.full_path.rstrip("?")))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not DEMO_PASSWORD:
        return redirect("/")
    nxt = _safe_next(request.values.get("next"))
    error = None
    if request.method == "POST":
        given = f"{request.form.get('username', '')}:{request.form.get('password', '')}"
        if hmac.compare_digest(given.encode(), f"{DEMO_USER}:{DEMO_PASSWORD}".encode()):
            session.clear()
            session.permanent = True
            session["login"] = _login_token()
            return redirect(nxt)
        time.sleep(LOGIN_FAIL_DELAY_S)  # slows password guessing; one shared password, so no per-user lockout
        error = "Wrong username or password."
    res = Response(render_template("login.html", next=nxt, error=error), 401 if error else 200)
    res.headers["Cache-Control"] = "no-store"
    return res


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login") if DEMO_PASSWORD else "/")


LOGIN_FAIL_DELAY_S = 1.0


@app.context_processor
def inject_source():
    _, source = data.load()
    return {"data_source": source, "public_demo": data.PUBLIC_DEMO, "hosted_study": data.HOSTED_STUDY,
            "hosted": data.hosted()}


@app.after_request
def demo_headers(res):
    if data.hosted():  # neither fake nor study patient pages belong in search results
        res.headers["X-Robots-Tag"] = "noindex, nofollow"
    return res


@app.route("/robots.txt")
def robots():
    if not data.hosted():
        abort(404)
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")


def _local_only() -> None:
    """404 for features that need this computer (JupyterLab, writing files, a registered EHR client)."""
    if data.hosted():
        abort(404)


@app.route("/healthz")
def healthz():
    return "ok"  # for the host's health check: no data, no templates


@app.route("/")
def patients():
    sel = _selection(request.args)
    return render_template("patients.html", active="patients", patients=data.patient_list(), sel=sel,
                           summary=data.cohort_summary(sel["ids"]), filters=data.FILTERS,
                           options=data.filter_options())


@app.route("/patient/<pid>")
def patient(pid):
    # ?window=7|14: the recent window the cards compare with the usual range; anything else falls back to 14.
    raw = request.args.get("window", "")
    window = int(raw) if raw.isdigit() and int(raw) in data.WINDOWS else data.RECENT_DAYS
    detail = data.patient_detail(pid, window)
    if detail is None:
        abort(404)
    ctx = session.get("ehr_context")
    return render_template("patient.html", active="patients", p=detail,
                           sections=data.SNAPSHOT_SECTIONS,
                           ehr_context=ctx if ctx and ctx.get("patient") == pid else None)


@app.route("/api/patient/<pid>/intraday")
def api_intraday(pid):
    out = data.intraday(pid, request.args.get("start", 1, type=int), request.args.get("days", 3, type=int))
    if out is None:
        abort(404)
    return jsonify(out)


# ── SMART on FHIR EHR launch (bare bones) ────────────────────────────────────
# Flow: EHR (e.g. Medplum) opens /ehr/launch?iss=…&launch=… → we redirect to the EHR's authorize
# endpoint (PKCE) → EHR redirects to /ehr/callback?code=… → we swap the code for a token, read the
# launched Patient, take its MRN identifier and open that patient's profile here.
SMART = {
    "client_id": os.environ.get("SMART_CLIENT_ID", ""),
    "client_secret": os.environ.get("SMART_CLIENT_SECRET", ""),
    "scope": os.environ.get("SMART_SCOPES", "launch openid fhirUser patient/Patient.rs"),
    "redirect_uri": os.environ.get("SMART_REDIRECT_URI", ""),
}
LAUNCH_LOG: list[dict] = []  # in memory only, last 20 launches


def _log_launch(**entry) -> None:
    if data.hosted() and entry.get("patient") == "no match":
        return  # the log is shared by every visitor, so keep free text typed by strangers out of it
    LAUNCH_LOG.insert(0, {"time": datetime.now().strftime("%b %d %H:%M:%S"), **entry})
    del LAUNCH_LOG[20:]


def _http_json(url: str, form: dict | None = None, token: str | None = None) -> dict:
    headers = {"Accept": "application/json"}
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        if SMART["client_secret"]:
            creds = f"{SMART['client_id']}:{SMART['client_secret']}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(creds).decode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=15) as res:
        return json.loads(res.read())


def _redirect_uri() -> str:
    return SMART["redirect_uri"] or url_for("ehr_callback", _external=True)


def _open_launched_patient(mrn: str | None, source: str, record: dict | None = None):
    local = data.resolve_mrn(mrn) if mrn else None
    _log_launch(source=source, mrn=mrn or "—", patient=local or "no match")
    if not local:
        return redirect(url_for("ehr", error=f"No local patient matches MRN {mrn or '(none on the FHIR Patient)'}."))
    session["ehr_context"] = {"patient": local, "mrn": mrn, "source": source, "time": datetime.now().strftime("%H:%M"),
                              "record": record}
    return redirect(url_for("patient", pid=local))


def _read_launched_patient(iss: str, tok: dict) -> dict:
    base, ref = iss.rstrip("/"), str(tok["patient"]).removeprefix("Patient/")
    try:
        return _http_json(f"{base}/Patient/{urllib.parse.quote(ref)}", token=tok["access_token"])
    except urllib.error.HTTPError as exc:
        if exc.code not in (400, 404):
            raise
    # Medplum puts an identifier value, not a FHIR id, in `patient` when the client sets launchIdentifierSystems.
    query = urllib.parse.urlencode({"identifier": f"{data.MRN_SYSTEM}|{ref}"})
    hits = _http_json(f"{base}/Patient?{query}", token=tok["access_token"]).get("entry") or []
    if not hits:
        raise KeyError(f"no Patient for launch context {ref}")
    return hits[0]["resource"]


def _record_summary(p: dict) -> dict:
    """What the EHR told us about the launched patient, for the profile banner (read-only)."""
    name = (p.get("name") or [{}])[0]
    full = name.get("text") or " ".join([*name.get("given", []), name.get("family", "")]).strip()
    return {"id": p.get("id"), "name": full or None, "gender": p.get("gender"), "birth_date": p.get("birthDate")}


@app.route("/ehr")
def ehr():
    df, source = data.load()
    examples = [(pid, data.synthetic_mrn(pid)) for pid in sorted(df["name"].unique())[:8]] if source == "synthetic" else []
    return render_template("ehr.html", active="ehr", smart=SMART, mrn_system=data.MRN_SYSTEM, log=LAUNCH_LOG,
                           launch_url=url_for("ehr_launch", _external=True), redirect_uri=_redirect_uri(),
                           examples=examples, error=request.args.get("error"),
                           mrn_map=None if data.hosted() else os.environ.get("HE_MRN_MAP"))


@app.route("/ehr/launch")
def ehr_launch():
    _local_only()  # the server would fetch any 'iss' URL a visitor passes
    iss, launch = request.args.get("iss"), request.args.get("launch")
    if not iss:
        return redirect(url_for("ehr", error="Launch is missing the 'iss' (FHIR server) parameter."))
    if not SMART["client_id"]:
        return redirect(url_for("ehr", error="SMART_CLIENT_ID is not set. Register this app in the EHR first."))
    try:
        cfg = _http_json(iss.rstrip("/") + "/.well-known/smart-configuration")
    except (urllib.error.URLError, ValueError) as exc:
        return redirect(url_for("ehr", error=f"Could not read SMART configuration from {iss}: {exc}"))
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    session["smart"] = {"iss": iss, "state": state, "verifier": verifier, "token_endpoint": cfg["token_endpoint"]}
    params = {"response_type": "code", "client_id": SMART["client_id"], "redirect_uri": _redirect_uri(),
              "scope": SMART["scope"], "state": state, "aud": iss,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    if launch:
        params["launch"] = launch
    return redirect(cfg["authorization_endpoint"] + "?" + urllib.parse.urlencode(params))


@app.route("/ehr/callback")
def ehr_callback():
    _local_only()
    st = session.pop("smart", None)
    if request.args.get("error"):
        return redirect(url_for("ehr", error=f"EHR returned an error: {request.args.get('error_description') or request.args['error']}"))
    if not st or request.args.get("state") != st["state"]:
        return redirect(url_for("ehr", error="Launch state didn't match. Start the launch again from the EHR."))
    try:
        tok = _http_json(st["token_endpoint"], form={
            "grant_type": "authorization_code", "code": request.args.get("code", ""), "redirect_uri": _redirect_uri(),
            "client_id": SMART["client_id"], "code_verifier": st["verifier"]})
        fhir_patient = _read_launched_patient(st["iss"], tok)
    except (urllib.error.URLError, KeyError, ValueError) as exc:
        return redirect(url_for("ehr", error=f"Token or Patient request failed: {exc}"))
    mrn = next((i.get("value") for i in fhir_patient.get("identifier", []) if i.get("system") == data.MRN_SYSTEM), None)
    return _open_launched_patient(mrn, source=urllib.parse.urlparse(st["iss"]).netloc, record=_record_summary(fhir_patient))


@app.route("/ehr/simulate")
def ehr_simulate():
    return _open_launched_patient(request.args.get("mrn", "").strip(), source="simulated launch")


def _selection(args) -> dict:
    """Patients-tab filters → {'ids': [...] or None, 'label': str, 'params': {...}}."""
    params = data.parse_filters(args)
    if not params:
        return {"ids": None, "label": "All patients", "params": {}}
    return {"ids": data.select_ids(params), "label": data.describe_filters(params), "params": params}


@app.route("/cohort")
def cohort():
    sel = _selection(request.args)
    return render_template("cohort.html", active="cohort", summary=data.cohort_summary(sel["ids"]), sel=sel,
                           nb_name=_notebook_name(sel), lite_url=_lite_url(), binder_url=EXPORT_BINDER_URL)


@app.route("/api/cohort")
def api_cohort():
    sel = _selection(request.args)
    try:
        return jsonify(data.cohort_compare(request.args.get("group", "decomp"), request.args.get("metric", data.DEFAULT_COHORT_METRIC), sel["ids"]))
    except KeyError:
        abort(400)


@app.route("/cohort/report")
def cohort_report():
    sel = _selection(request.args)
    group = request.args.get("group", "decomp")
    summary = data.cohort_summary(sel["ids"])
    if group not in summary["groups"]:
        abort(400)
    # A list, not a dict: tojson sorts dict keys, and the report keeps the Measure menu's order.
    comparisons = [[m, data.cohort_compare(group, m, sel["ids"])] for m in data.COHORT_METRICS]
    return render_template("report.html", summary=summary, sel=sel, group_label=summary["groups"][group],
                           comparisons=comparisons, generated=datetime.now().strftime("%b %d, %Y %H:%M"))


# Notebook export. Two ways out: POST writes the file next to local JupyterLab (local only); GET returns the
# same notebook as a download and never touches disk (local and public demo). The GET also serves JupyterLite's
# `?fromURL=`, which fetches cross-origin and names the file after the URL's last path segment, query string
# included, so that link carries the filters base64url-encoded in the path instead.
# The hosted study site serves its own JupyterLite (built without data in Dockerfile.deid) at /lite/, same origin.
def _lite_url() -> str:
    return "/lite/lab/index.html" if data.HOSTED_STUDY else f"{JUPYTERLITE_URL}/lab/index.html"
EXPORT_BINDER_URL = (f"https://mybinder.org/v2/gh/{NOTEBOOK_REPO}/{urllib.parse.quote(NOTEBOOK_REF, safe='')}"
                     "?urlpath=lab/tree/notebooks")
NOTEBOOK_CORS_ORIGINS = {o.strip().rstrip("/") for o in
                         os.environ.get("NOTEBOOK_CORS_ORIGINS", "https://dicristea.github.io").split(",") if o.strip()}
MAX_EXPORT_QUERY = 2000  # filters only: anything longer isn't a cohort selection


def _notebook_name(sel: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", sel["label"].lower()).strip("-")[:60].strip("-")
    return f"cohort-{slug or 'all-patients'}.ipynb"


def _export_args(token: str | None):
    if token is None:
        raw, args = request.query_string, request.args
    else:
        try:
            raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            args = dict(urllib.parse.parse_qsl(raw.decode()))
        except (ValueError, UnicodeDecodeError):
            abort(404)
    if len(raw) > MAX_EXPORT_QUERY:
        abort(414)
    return args


@app.route("/api/cohort/notebook.ipynb")
@app.route("/api/cohort/notebook/<token>/<name>")
def cohort_notebook_download(token: str | None = None, name: str | None = None):
    if name is not None and not name.endswith(".ipynb"):
        abort(404)
    args = _export_args(token)
    sel = _selection(args)
    group = args.get("group", "decomp")
    if group not in data.cohort_summary(sel["ids"])["groups"]:
        abort(400)
    res = Response(nbformat.writes(_build_cohort_notebook(sel, group)), mimetype="application/x-ipynb+json")
    res.headers["Content-Disposition"] = f'attachment; filename="{_notebook_name(sel)}"'
    res.headers["Cache-Control"] = "no-store"
    res.headers["Vary"] = "Origin"
    if request.headers.get("Origin", "").rstrip("/") in NOTEBOOK_CORS_ORIGINS:
        res.headers["Access-Control-Allow-Origin"] = request.headers["Origin"]
    return res


@app.route("/api/cohort/notebook", methods=["POST"])
def cohort_notebook():
    _local_only()
    body = request.get_json(silent=True) or {}
    sel = _selection(body)
    group = body.get("group", "decomp")
    if group not in data.cohort_summary(sel["ids"])["groups"]:
        abort(400)
    folder = NOTEBOOK_DIRS[0]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"cohort_{datetime.now():%Y%m%d_%H%M%S}.ipynb"
    nbformat.write(_build_cohort_notebook(sel, group), path)
    try:
        url = f"{JUPYTER_BASE_URL}/lab/tree/{path.relative_to(JUPYTER_ROOT).as_posix()}"
    except ValueError:
        url = None
    return jsonify({"file": path.name, "url": url})


def _md_text(text: str) -> str:
    """Filter text (the patient-ID search is free text) as inert Markdown: one line, no markup or HTML."""
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|<>~&$])", r"\\\1", " ".join(text.split()))


def _build_cohort_notebook(sel: dict, group: str) -> nbformat.NotebookNode:
    # The notebook stores the filter rules, not patient IDs, and re-selects patients when run.
    # Filter values reach code cells only through repr() of a str -> str dict, so they stay data.
    nb = nbformat.v4.new_notebook()
    nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
    group_label = data.cohort_summary(sel["ids"])["groups"][group]
    filters = {str(k): str(v) for k, v in sel["params"].items()}
    if data.HOSTED_STUDY:
        where = ("Exported from the **password-protected study site**. Open it in the site's own JupyterLite "
                 "(Notebooks tab): there `data.load()` fetches the de-identified study data from the site while you "
                 "are logged in. 🔒 Don't share it with outputs.")
    elif data.PUBLIC_DEMO:
        where = ("Exported from the **public demo**, so it runs on **synthetic data**: `data.load()` finds no study "
                 "file in Binder, JupyterLite or a fresh clone and builds the same 24 synthetic patients the demo "
                 "shows. Run it from the repo's `notebooks/` folder (in JupyterLite, upload it into `notebooks/`).")
    else:
        where = ("Each cell recomputes with `data.load()`: the local study file if this computer has one, otherwise "
                 "synthetic data. 🔒 Don't commit this notebook with outputs.")
    nb.cells = [
        nbformat.v4.new_markdown_cell(
            f"# Cohort export: {_md_text(sel['label'])}\n\n"
            f"Exported from the HE Clinician Dashboard on {datetime.now():%b %d, %Y %H:%M}. "
            f"Comparison: **{_md_text(group_label)}**.\n\n{where} Exploratory and unadjusted."),
        nbformat.v4.new_code_cell(
            "import sys\nfrom pathlib import Path\n"
            "# the dashboard's data.py: repo root, one folder up from notebooks/ (or here, if saved next to it)\n"
            "sys.path.insert(0, '.' if Path('data.py').exists() and not Path('../data.py').exists() else '..')\n"
            "import pandas as pd, matplotlib.pyplot as plt\nimport data\n\n"
            f"FILTERS = {filters!r}  # same filters as the Patients tab\n"
            f"GROUP = {group!r}\n"
            "ids = data.select_ids(FILTERS) if FILTERS else None\n"
            "df, source = data.load()\n"
            "print(f'{len(ids) if ids is not None else df.name.nunique()} patients (data: {source})')"),
        nbformat.v4.new_markdown_cell("## Summary"),
        nbformat.v4.new_code_cell(
            "s = data.cohort_summary(ids)\n"
            "pd.Series({k: s[k] for k in ['participants', 'days', 'valid_pct', 'median_days', 'sleep_mid', 'sleep_tst']})"),
        nbformat.v4.new_markdown_cell(
            "## Group comparison for every measure\n\n"
            "Each person's median over their main nights (sleep, HR, HRV, SpO₂) or valid days (steps, activity "
            "measures), for people with at least 14 values. Two groups: Mann-Whitney; more: Kruskal-Wallis. "
            "Groups with fewer than 5 people are not tested. Exploratory: unadjusted for age, sex, beta-blockers, "
            "site or arm, and about 1 in 20 comparisons reaches p < 0.05 by chance."),
        nbformat.v4.new_code_cell(
            "rows = []\n"
            "for m, spec in data.COHORT_METRICS.items():\n"
            "    c = data.cohort_compare(GROUP, m, ids)\n"
            "    row = {'measure': spec['label']}\n"
            "    for l in c['levels']:\n"
            "        row[l['name']] = l['overall_median']\n"
            "        row[f\"n {l['name']}\"] = l['n']\n"
            "    row['p'] = c.get('p_text', '')\n"
            "    row['reading'] = c['direction']\n"
            "    rows.append(row)\n"
            "pd.DataFrame(rows)"),
        nbformat.v4.new_markdown_cell("## Trajectories over the study (weekly median and middle 50%)\n\n"
                                      "Weeks with fewer than 5 people in a group are hidden. Later weeks include "
                                      "fewer people (dropout), so the tail is not a trend."),
        nbformat.v4.new_code_cell(
            "fig, axes = plt.subplots(3, 4, figsize=(16, 9.5))\n"
            "for ax, (m, spec) in zip(axes.flat, data.COHORT_METRICS.items()):\n"
            "    c = data.cohort_compare(GROUP, m, ids)\n"
            "    for color, l in zip(['#2563eb', '#d97706', '#059669', '#7c3aed'], c['levels']):\n"
            "        ax.plot(l['weeks'], l['median'], color=color, lw=2, label=f\"{l['name']} (n={l['n']})\")\n"
            "        ax.fill_between(l['weeks'], l['q1'], l['q3'], color=color, alpha=0.15)\n"
            "    ax.set_title(spec['label']); ax.set_xlabel('study week')\n"
            "axes.flat[0].legend(fontsize=8)\nfor ax in list(axes.flat)[len(data.COHORT_METRICS):]: ax.set_axis_off()\n"
            "plt.tight_layout(); plt.show()"),
        nbformat.v4.new_markdown_cell("## Your analysis\n\nAdd cells below, or ask Jupyter AI in the chat panel."),
        nbformat.v4.new_code_cell(""),
    ]
    return nb


@app.route("/notebooks")
def notebooks():
    if data.PUBLIC_DEMO:
        return render_template("notebooks.html", active="notebooks", notebooks=_demo_notebooks(),
                               synthetic_doc=f"https://github.com/{NOTEBOOK_REPO}/blob/{NOTEBOOK_REF}/docs/synthetic-data.md",
                               repo_url=f"https://github.com/{NOTEBOOK_REPO}")
    if data.HOSTED_STUDY:  # this site's own JupyterLite, on this data; no Binder or pre-run copies (no data at build)
        return render_template("notebooks.html", active="notebooks", notebooks=_hosted_notebooks(),
                               lite_available=(LITE_DIR / "index.html").is_file())
    return render_template("notebooks.html", active="notebooks", notebooks=_list_notebooks(),
                           jupyter_base=JUPYTER_BASE_URL, notebook_dirs=NOTEBOOK_DIRS)


def _demo_notebooks() -> list[dict]:
    ref = urllib.parse.quote(NOTEBOOK_REF, safe="")
    return [{**nb,
             "view_url": f"/notebooks/view/{nb['name']}",  # pre-run copies baked into the demo image
             "lite_url": f"{JUPYTERLITE_URL}/lab/index.html?path=notebooks/{nb['name']}.ipynb",
             "binder_url": f"https://mybinder.org/v2/gh/{NOTEBOOK_REPO}/{ref}"
                           f"?urlpath=lab/tree/notebooks/{nb['name']}.ipynb"}
            for nb in DEMO_NOTEBOOKS]


def _hosted_notebooks() -> list[dict]:
    return [{**nb, "lite_url": f"{_lite_url()}?path=notebooks/{nb['name']}.ipynb",
             "download_url": f"/lite/files/notebooks/{nb['name']}.ipynb"} for nb in DEMO_NOTEBOOKS]


# The hosted study site's JupyterLite: static files from Dockerfile.deid's builder stage, no data in them. Behind
# the login like every page; the notebooks fetch the data from /api/study-data.csv.gz with the same cookie.
LITE_DIR = Path(os.environ.get("HE_LITE_DIR", HERE / "lite_site")).expanduser()
for _ext, _type in {".wasm": "application/wasm", ".mjs": "text/javascript", ".js": "text/javascript",
                    ".json": "application/json", ".ipynb": "application/x-ipynb+json"}.items():
    mimetypes.add_type(_type, _ext)


@app.route("/lite/")
@app.route("/lite/<path:path>")
def lite(path: str = ""):
    if not data.HOSTED_STUDY:
        abort(404)
    if path == "" or path.endswith("/"):
        path += "index.html"
    elif (LITE_DIR / path).is_dir():
        return redirect(f"/lite/{path}/")
    res = send_from_directory(LITE_DIR, path, max_age=0)  # safe_join: no escaping LITE_DIR
    res.headers["Cache-Control"] = "private, no-cache"  # revalidate, so a redeploy shows up; never shared caches
    return res


@app.route("/api/study-data.csv.gz")
def study_data():
    """The downloaded study cache, for notebooks running in this site's JupyterLite (logged-in visitors only)."""
    if not data.HOSTED_STUDY or data.HOSTED_DATA_FILE is None:
        abort(404)
    res = send_file(data.HOSTED_DATA_FILE, mimetype="application/gzip", max_age=0, conditional=False,
                    download_name=f"study.{data.CACHE_VERSION}.csv.gz")
    res.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    return res


# Read-only notebook pages for the public demo, rendered on synthetic data in the Docker builder stage
# (scripts/render_notebooks.py, same names). The app never runs a notebook; it only serves these files.
RENDERED_DIR = HERE / "rendered_notebooks"
RENDERED_NOTEBOOKS = {nb["name"]: nb["title"] for nb in DEMO_NOTEBOOKS}  # the allowlist
# The page is static: no scripts, no requests anywhere, sandboxed even when opened outside the iframe.
NOTEBOOK_CSP = ("default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; "
                "form-action 'none'; frame-ancestors 'self'; sandbox allow-top-navigation-by-user-activation")


def _rendered_notebook(name: str) -> Path | None:
    """The pre-rendered page for an allowlisted name, or None. Local mode never serves these."""
    if not data.PUBLIC_DEMO or name not in RENDERED_NOTEBOOKS:
        return None
    path = RENDERED_DIR / f"{name}.html"
    return path if path.is_file() else None


@app.route("/notebooks/view/<name>")
def notebook_view(name):
    if name not in RENDERED_NOTEBOOKS:
        abort(404)
    available = _rendered_notebook(name) is not None
    return render_template("notebook_view.html", active="notebooks", name=name, title=RENDERED_NOTEBOOKS[name],
                           available=available), 200 if available else 404


@app.route("/notebooks/view/<name>/page")
def notebook_page(name):
    path = _rendered_notebook(name)
    if path is None:
        abort(404)
    res = Response(path.read_bytes(), mimetype="text/html")
    res.headers.update({"Content-Security-Policy": NOTEBOOK_CSP, "X-Frame-Options": "SAMEORIGIN",
                        "X-Content-Type-Options": "nosniff", "Cache-Control": "public, max-age=3600"})
    return res


# Notebooks shown as read-only code in every mode (never run by the app). Both Dockerfiles copy these files.
CODE_PAGES = {"speech_features_starter": "Speech features (starter)"}


def _code_cells(source: str) -> list[dict]:
    """Split a jupytext percent file into markdown and code cells, dropping the YAML header."""
    cells: list[dict] = []
    for block in re.split(r"^# %%", source, flags=re.M)[1:]:
        head, _, body = block.partition("\n")
        if "[markdown]" in head:
            text = "\n".join(re.sub(r"^# ?", "", line) for line in body.splitlines())
            cells.append({"kind": "markdown", "text": text.strip()})
        elif body.strip():
            cells.append({"kind": "code", "text": body.strip()})
    return cells


_PY_TOKEN = re.compile(
    r'(?P<comment>#[^\n]*)'
    r'|(?P<string>[rRbBfF]{0,2}(?:"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'))'
    r'|(?P<keyword>\b(?:and|as|def|elif|else|except|for|from|if|import|in|is|lambda|not|None|or|return|True|False|try|while|with|yield)\b)'
    r'|(?P<number>\b\d+(?:\.\d+)?(?:e-?\d+)?\b)')


def _py_html(code: str) -> Markup:
    """Light syntax colouring for the read-only code page; every piece is escaped."""
    out, pos = [], 0
    for m in _PY_TOKEN.finditer(code):
        out.append(escape(code[pos:m.start()]))
        out.append(Markup('<span class="tok-{}">{}</span>').format(m.lastgroup, m.group()))
        pos = m.end()
    out.append(escape(code[pos:]))
    return Markup("").join(out)


def _md_inline(text: str) -> Markup:
    html = str(escape(text))
    html = re.sub(r"`([^`]+)`", r"<code>\1</code>", html)
    html = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", html)
    return Markup(html)


def _md_html(text: str) -> Markup:
    """The small Markdown subset the notebooks' notes use: headings, bullet lists, quotes, paragraphs."""
    blocks: list[Markup] = []
    para: list[str] = []
    items: list[str] = []
    quote: list[str] = []

    def flush():
        if para:
            blocks.append(Markup("<p>{}</p>").format(_md_inline(" ".join(para))))
            para.clear()
        if items:
            blocks.append(Markup("<ul>{}</ul>").format(Markup("").join(
                Markup("<li>{}</li>").format(_md_inline(i)) for i in items)))
            items.clear()
        if quote:
            blocks.append(Markup("<blockquote>{}</blockquote>").format(_md_inline(" ".join(quote))))
            quote.clear()

    for line in text.splitlines():
        s = line.strip()
        if not s:
            flush()
        elif m := re.match(r"(#{1,4})\s+(.*)", s):
            flush()
            level = min(len(m.group(1)) + 1, 4)  # the page title is the h1
            blocks.append(Markup("<h{0}>{1}</h{0}>").format(level, _md_inline(m.group(2))))
        elif s.startswith(("- ", "* ")):
            if para or quote:
                flush()
            items.append(s[2:])
        elif s.startswith(">"):
            if para or items:
                flush()
            quote.append(s.lstrip("> ").strip())
        elif items and line.startswith("  "):
            items[-1] += " " + s  # continuation of a list item
        else:
            if items or quote:
                flush()
            para.append(s)
    flush()
    return Markup("").join(blocks)


app.jinja_env.filters["py_html"] = _py_html
app.jinja_env.filters["md_html"] = _md_html


@app.route("/notebooks/code/<name>")
def notebook_code(name):
    if name not in CODE_PAGES:
        abort(404)
    path = HERE / "notebooks" / f"{name}.py"
    if not path.is_file():
        abort(404)
    return render_template("notebook_code.html", active="notebooks", title=CODE_PAGES[name], file=path.name,
                           cells=_code_cells(path.read_text(encoding="utf-8")))


def _list_notebooks() -> list[dict]:
    out = []
    for folder in NOTEBOOK_DIRS:
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.ipynb")):
            stat = path.stat()
            try:
                rel = path.relative_to(JUPYTER_ROOT).as_posix()
            except ValueError:
                rel = None  # outside the JupyterLab root, so it can't be linked
            out.append({
                "name": path.stem.replace("_", " "),
                "file": path.name,
                "folder": folder.name,
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%b %d, %Y %H:%M"),
                "size_kb": round(stat.st_size / 1024),
                "url": f"{JUPYTER_BASE_URL}/lab/tree/{rel}" if rel else None,
            })
    return out


if __name__ == "__main__":
    if data.hosted():  # only the hosted modes may listen beyond this computer
        app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5050)), debug=False)
    else:
        app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5050)), debug=os.environ.get("FLASK_DEBUG", "1") == "1")
