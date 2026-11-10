"""HE Clinician Dashboard: a simpler, clinician-facing view of the HE study's Oura data.

Three tabs: Patients (list → personal profile), Cohort, Notebooks.
Runs on 127.0.0.1 only. There is no login, so it must never be deployed with study data.
The one exception is HE_PUBLIC_DEMO=1 (docs/hosting.md): synthetic data only, local-only features off.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import nbformat
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for

import data

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

HERE = Path(__file__).resolve().parent
JUPYTER_BASE_URL = os.environ.get("JUPYTER_BASE_URL", "http://localhost:8888")
# Folders whose .ipynb files show in the Notebooks tab, relative to where JupyterLab is started (JUPYTER_ROOT).
JUPYTER_ROOT = Path(os.environ.get("JUPYTER_ROOT", HERE)).expanduser().resolve()
NOTEBOOK_DIRS = [Path(p).expanduser().resolve() for p in
                 os.environ.get("NOTEBOOK_DIRS", str(HERE / "notebooks")).split(os.pathsep) if p]
if data.PUBLIC_DEMO:
    data.load()  # a misconfigured demo (study source, MRN map) should fail the deploy, not the first visitor
# Optional shared password for the hosted demo (docs/hosting.md). Off unless DEMO_PASSWORD is set.
DEMO_USER = os.environ.get("DEMO_USER", "demo")
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "")


@app.before_request
def demo_password():
    if not DEMO_PASSWORD or request.path == "/healthz":  # the host's health check can't log in
        return None
    auth = request.authorization
    given = f"{auth.username}:{auth.password}" if auth and auth.type == "basic" else ""
    if hmac.compare_digest(given.encode(), f"{DEMO_USER}:{DEMO_PASSWORD}".encode()):
        return None
    return Response("Password required.", 401, {"WWW-Authenticate": 'Basic realm="HE dashboard demo", charset="UTF-8"'})


@app.context_processor
def inject_source():
    _, source = data.load()
    return {"data_source": source, "public_demo": data.PUBLIC_DEMO}


@app.after_request
def demo_headers(res):
    if data.PUBLIC_DEMO:  # fake patient pages shouldn't show up in search results as if they were real
        res.headers["X-Robots-Tag"] = "noindex, nofollow"
    return res


def _local_only() -> None:
    """404 for features that need this computer (JupyterLab, writing files, a registered EHR client)."""
    if data.PUBLIC_DEMO:
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
    detail = data.patient_detail(pid)
    if detail is None:
        abort(404)
    ctx = session.get("ehr_context")
    return render_template("patient.html", active="patients", p=detail,
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
    if data.PUBLIC_DEMO and entry.get("patient") == "no match":
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
                           mrn_map=None if data.PUBLIC_DEMO else os.environ.get("HE_MRN_MAP"))


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
    return render_template("cohort.html", active="cohort", summary=data.cohort_summary(sel["ids"]), sel=sel)


@app.route("/api/cohort")
def api_cohort():
    sel = _selection(request.args)
    try:
        return jsonify(data.cohort_compare(request.args.get("group", "decomp"), request.args.get("metric", "night_rest_h"), sel["ids"]))
    except KeyError:
        abort(400)


@app.route("/cohort/report")
def cohort_report():
    sel = _selection(request.args)
    group = request.args.get("group", "decomp")
    summary = data.cohort_summary(sel["ids"])
    if group not in summary["groups"]:
        abort(400)
    comparisons = {m: data.cohort_compare(group, m, sel["ids"]) for m in data.METRICS}
    return render_template("report.html", summary=summary, sel=sel, group_label=summary["groups"][group],
                           comparisons=comparisons, generated=datetime.now().strftime("%b %d, %Y %H:%M"))


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


def _build_cohort_notebook(sel: dict, group: str) -> nbformat.NotebookNode:
    # The notebook stores the filter rules, not patient IDs, and re-selects patients when run.
    nb = nbformat.v4.new_notebook()
    nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
    group_label = data.cohort_summary(sel["ids"])["groups"][group]
    nb.cells = [
        nbformat.v4.new_markdown_cell(
            f"# Cohort export: {sel['label']}\n\n"
            f"Exported from the HE Clinician Dashboard on {datetime.now():%b %d, %Y %H:%M}. "
            f"Comparison: **{group_label}**.\n\n"
            "Each cell recomputes from the local study file, so re-run to refresh. "
            "Exploratory and unadjusted. 🔒 Don't commit this notebook with outputs."),
        nbformat.v4.new_code_cell(
            "import sys\nsys.path.insert(0, '..')  # the dashboard's data.py\n"
            "import pandas as pd, matplotlib.pyplot as plt\nimport data\n\n"
            f"FILTERS = {json.dumps(sel['params'])}  # same filters as the Patients tab\n"
            f"GROUP = {group!r}\n"
            "ids = data.select_ids(**FILTERS) if FILTERS else None\n"
            "df, source = data.load()\n"
            "print(f'{len(ids) if ids is not None else df.name.nunique()} patients (data: {source})')"),
        nbformat.v4.new_markdown_cell("## Summary"),
        nbformat.v4.new_code_cell(
            "s = data.cohort_summary(ids)\n"
            "pd.Series({k: s[k] for k in ['participants', 'days', 'valid_pct', 'median_days', 'night_rest', 'sri']})"),
        nbformat.v4.new_markdown_cell(
            "## Group comparison for every measure\n\nMedian of each person's valid days. "
            "Mann-Whitney p-value: below 0.05 is worth a closer look, not proof."),
        nbformat.v4.new_code_cell(
            "rows = []\n"
            "for m, spec in data.METRICS.items():\n"
            "    c = data.cohort_compare(GROUP, m, ids)\n"
            "    row = {'measure': spec['label']}\n"
            "    for l in c['levels']:\n"
            "        row[l['name']] = l['overall_median']\n"
            "        row[f\"n {l['name']}\"] = l['n']\n"
            "    row['p-value'] = c.get('p_value')\n"
            "    rows.append(row)\n"
            "pd.DataFrame(rows)"),
        nbformat.v4.new_markdown_cell("## Trajectories over the study (weekly median and middle 50%)"),
        nbformat.v4.new_code_cell(
            "fig, axes = plt.subplots(2, 4, figsize=(16, 6.5))\n"
            "for ax, (m, spec) in zip(axes.flat, data.METRICS.items()):\n"
            "    c = data.cohort_compare(GROUP, m, ids)\n"
            "    for color, l in zip(['#2563eb', '#d97706'], c['levels']):\n"
            "        ax.plot(l['weeks'], l['median'], color=color, lw=2, label=f\"{l['name']} (n={l['n']})\")\n"
            "        ax.fill_between(l['weeks'], l['q1'], l['q3'], color=color, alpha=0.15)\n"
            "    ax.set_title(spec['label']); ax.set_xlabel('study week')\n"
            "axes.flat[0].legend(fontsize=8)\nfor ax in list(axes.flat)[len(data.METRICS):]: ax.set_axis_off()\n"
            "plt.tight_layout(); plt.show()"),
        nbformat.v4.new_markdown_cell("## Your analysis\n\nAdd cells below, or ask Jupyter AI in the chat panel."),
        nbformat.v4.new_code_cell(""),
    ]
    return nb


@app.route("/notebooks")
def notebooks():
    return render_template("notebooks.html", active="notebooks",
                           notebooks=[] if data.PUBLIC_DEMO else _list_notebooks(),
                           jupyter_base=JUPYTER_BASE_URL, notebook_dirs=NOTEBOOK_DIRS)


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
    if data.PUBLIC_DEMO:  # only the synthetic demo may listen beyond this computer
        app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5050)), debug=False)
    else:
        app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5050)), debug=os.environ.get("FLASK_DEBUG", "1") == "1")
