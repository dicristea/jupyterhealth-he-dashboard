import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
from markupsafe import escape
import pytest

os.environ["HE_DATA_FILE"] = "/nonexistent/no-data-here.rds"  # force the synthetic cohort
os.environ.pop("HE_PUBLIC_DEMO", None)  # tests start from the local default; demo tests switch it on

import data  # noqa: E402
from app import app  # noqa: E402


def test_falls_back_to_synthetic():
    df, source = data.load()
    assert source == "synthetic"
    assert {"night_rest_h", "sri", "rest_mid_6pm"} <= set(df.columns)


def test_configurable_synthetic_cohort():
    df = data.synthetic_cohort(n=3, seed=1, min_days=5, max_days=5)
    assert df["name"].nunique() == 3 and len(df) == 15
    assert {"class_5_min", "met_5min", "valid80", "non_wear_pct"} <= set(df.columns)
    assert df["class_5_min"].str.len().eq(288).all()


def test_night_rest_proxy_is_plausible():
    df, _ = data.load()
    assert 3 < df["night_rest_h"].median() < 10
    same_person_next = (df["name"].shift(-1) == df["name"]) & (df["days_since_start"].shift(-1) == df["days_since_start"] + 1)
    valid_day = df["valid80"].eq(1)
    valid_pair = same_person_next & valid_day & valid_day.shift(-1, fill_value=False)
    assert df.loc[~valid_pair, ["night_rest_h", "rest_mid_6pm"]].isna().all().all()


def test_he_status_does_not_turn_positive_or_missing_data_into_no_he():
    import pandas as pd
    assert data._he_status(pd.Series({"anyOHE": 1, "che_phes": "No", "anyhe": 1})) == "Overt"
    assert data._he_status(pd.Series({"anyOHE": 0, "che_phes": "Yes", "anyhe": 1})) == "Covert"
    assert data._he_status(pd.Series({"anyOHE": 0, "che_phes": "No", "anyhe": 0})) == "No"
    assert data._he_status(pd.Series({"anyOHE": 0, "che_phes": "No", "anyhe": 1})) is None
    assert data._he_status(pd.Series({"anyOHE": None, "che_phes": None, "anyhe": None})) is None


def test_cohort_summary_gives_each_patient_equal_weight():
    df, _ = data.load()
    valid = df[df["valid80"] == 1]
    per_person = valid.groupby("name")[["night_rest_h", "sri"]].median()
    summary = data.cohort_summary()
    assert summary["night_rest"] == data._fmt(per_person["night_rest_h"].median())
    assert summary["sri"] == data._fmt(per_person["sri"].median(), 0)


def test_pages_render():
    client = app.test_client()
    pid = data.patient_list()[0]["id"]
    for url in ["/", f"/patient/{pid}", "/cohort", "/notebooks", "/api/cohort?group=decomp&metric=night_rest_h"]:
        assert client.get(url).status_code == 200, url
    assert client.get("/patient/does-not-exist").status_code == 404
    assert client.get("/api/cohort?group=nope&metric=steps").status_code == 400


def test_selection_flows_to_cohort():
    ids = data.select_ids({"ctp": "Child B"})
    assert ids and all(r["ctp"] == "Child B" for r in data.patient_list() if r["id"] in ids)
    client = app.test_client()
    page = client.get("/cohort?ctp=Child+B")
    assert page.status_code == 200 and b"This selection vs rest of cohort" in page.data
    d = client.get("/api/cohort?ctp=Child+B&group=_selection&metric=steps").get_json()
    assert [l["n"] for l in d["levels"]] == [len(ids), len(data.patient_list()) - len(ids)]
    assert client.get("/api/cohort?group=_selection&metric=steps").status_code == 400  # needs a selection
    assert client.get("/cohort?he=Nobody").status_code == 200  # empty cohort still renders


def test_exports(tmp_path, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "NOTEBOOK_DIRS", [tmp_path])
    client = app.test_client()
    assert client.get("/cohort/report?ctp=Child+B&group=decomp").status_code == 200
    res = client.post("/api/cohort/notebook", json={"ctp": "Child B", "group": "_selection"})
    assert res.status_code == 200
    import nbformat
    nb = nbformat.read(tmp_path / res.get_json()["file"], as_version=4)
    source = "\n".join(c.source for c in nb.cells)
    assert "{'ctp': 'Child B'}" in source and "DEMO-" not in source  # filters stored, not patient IDs
    assert "Don't commit this notebook" in source and "public demo" not in source
    cohort = client.get("/cohort").data.decode()  # local mode: JupyterLab export plus the download, no Lite link
    assert 'id="exportNb"' in cohort and 'id="exportNbDownload"' in cohort and 'id="exportLite"' not in cohort
    res = client.get("/api/cohort/notebook.ipynb?ctp=Child+B&group=decomp")
    assert res.status_code == 200 and res.headers["Content-Disposition"] == 'attachment; filename="cohort-child-b.ipynb"'


def _no_disk_writes(monkeypatch):
    import nbformat
    monkeypatch.setattr(nbformat, "write", lambda *a, **k: pytest.fail("export wrote a file"))
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: pytest.fail("export made a folder"))


def test_public_demo_notebook_download(public_demo, tmp_path):
    import nbformat
    import app as app_module
    public_demo.setattr(app_module, "NOTEBOOK_DIRS", [tmp_path / "nb"])
    _no_disk_writes(public_demo)
    client = app.test_client()
    cohort = client.get("/cohort").data.decode()
    assert 'id="exportNbDownload"' in cohort and 'id="exportLite"' in cohort and "Binder" in cohort
    res = client.get("/api/cohort/notebook.ipynb?ctp=Child+B&group=_selection")
    assert res.status_code == 200 and res.mimetype == "application/x-ipynb+json"
    assert res.headers["Content-Disposition"] == 'attachment; filename="cohort-child-b.ipynb"'
    assert "Access-Control-Allow-Origin" not in res.headers  # no Origin, no CORS
    nb = nbformat.reads(res.data.decode(), as_version=4)
    nbformat.validate(nb)
    source = "\n".join(c.source for c in nb.cells)
    assert "{'ctp': 'Child B'}" in source and "GROUP = '_selection'" in source and "DEMO-" not in source
    assert "synthetic data" in nb.cells[0].source and "public demo" in nb.cells[0].source
    assert len(res.data) < 20_000
    assert not (tmp_path / "nb").exists()
    assert client.get("/api/cohort/notebook.ipynb?group=nope").status_code == 400
    assert client.get("/api/cohort/notebook.ipynb?q=" + "x" * 3000).status_code == 414


def test_notebook_lite_url_and_cors(public_demo):
    import base64
    import app as app_module
    _no_disk_writes(public_demo)
    client = app.test_client()
    token = base64.urlsafe_b64encode(b"ctp=Child+B&group=decomp").decode().rstrip("=")
    url = f"/api/cohort/notebook/{token}/cohort-child-b.ipynb"
    ok = client.get(url, headers={"Origin": "https://dicristea.github.io"})
    assert ok.status_code == 200 and ok.headers["Access-Control-Allow-Origin"] == "https://dicristea.github.io"
    assert "Origin" in ok.headers["Vary"] and "{'ctp': 'Child B'}" in ok.data.decode()
    for origin in ["https://evil.example", "https://dicristea.github.io.evil.example", "null"]:
        assert "Access-Control-Allow-Origin" not in client.get(url, headers={"Origin": origin}).headers
    public_demo.setattr(app_module, "NOTEBOOK_CORS_ORIGINS", {"https://lite.example.org"})
    assert client.get(url, headers={"Origin": "https://lite.example.org"}).headers["Access-Control-Allow-Origin"] == "https://lite.example.org"
    assert client.get(f"/api/cohort/notebook/{token}/x.html").status_code == 404
    assert client.get("/api/cohort/notebook/!!!/a.ipynb").status_code == 404


def test_notebook_export_keeps_filter_values_as_data():
    import ast
    import nbformat
    evil = "x'}\nimport os; os.system('echo pwned') #\"\"\"<script>alert(1)</script>"
    client = app.test_client()
    res = client.get("/api/cohort/notebook.ipynb", query_string={"q": evil, "group": "decomp"})
    assert res.status_code == 200
    nb = nbformat.reads(res.data.decode(), as_version=4)
    setup = nb.cells[1].source
    tree = ast.parse(setup)  # still valid Python
    assign = next(n for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "FILTERS")
    assert ast.literal_eval(assign.value) == {"q": evil.strip()}
    calls = [n.func for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert not any(isinstance(f, ast.Attribute) and f.attr == "system" for f in calls)
    assert "<script>" not in nb.cells[0].source and "\nimport os" not in nb.cells[0].source


def test_patient_profile_and_intraday():
    pid = data.patient_list()[0]["id"]
    d = data.patient_detail(pid)
    assert d["synthetic"] and d["mrn"] == data.synthetic_mrn(pid)
    assert [s["label"] for s in d["summary"]] == ["Sleep midpoint", "Total sleep", "Sleep efficiency", "Ring wear coverage"]
    assert [t["key"] for t in d["tiles"]] == data.PATIENT_TILES
    assert [t["key"] for t in d["more_tiles"]] == data.MORE_TILES
    assert all({"spark", "band", "trend", "n_obs", "pill", "tone", "why"} <= set(t) for t in d["tiles"] + d["more_tiles"])
    a = d["activity"]  # the Activity section: 24-hour profile, time per class, actogram
    assert len(a["hours"]) == data.SLOTS and len(a["met"]["p50"]) == data.SLOTS and len(a["rest_share"]) == data.SLOTS
    assert [c["label"] for c in a["classes"]] == data.ACTIVITY_CLASSES
    assert [r["day"] for r in a["actogram"]] == d["days"][-data.ACTOGRAM_DAYS:]
    assert all(r["cls"] == "" or len(r["cls"]) == data.SLOTS for r in a["actogram"])
    assert len(d["nights"]) == data.TIMING_NIGHTS and [n["day"] for n in d["nights"]] == d["days"][-30:]
    client = app.test_client()
    body = client.get(f"/patient/{pid}").data
    assert b"Synthetic \xc2\xb7 de-identified demo" in body
    assert b'id="timing"' in body and b'id="moreMeasures"' in body and b"not validated for diagnosing HE" in body
    assert b'id="actogram"' in body and b'id="profile"' in body and b"Activity explorer" not in body
    assert b"prompt a conversation" not in body and b"no calendar dates" not in body
    # The intraday API stays (notebooks and research use), though the profile no longer shows it.
    i = client.get(f"/api/patient/{pid}/intraday?start=2&days=3").get_json()
    assert [x["day"] for x in i["days"]] == [2, 3, 4] and len(i["days"][0]["met"]) == 288


def test_ehr_simulated_launch():
    client = app.test_client()
    assert client.get("/ehr").status_code == 200
    pid = data.patient_list()[1]["id"]
    res = client.get(f"/ehr/simulate?mrn={data.synthetic_mrn(pid)}")
    assert res.status_code == 302 and res.headers["Location"].endswith(f"/patient/{pid}")
    assert b"Opened from the EHR" in client.get(f"/patient/{pid}").data
    bad = client.get("/ehr/simulate?mrn=JH-NOPE")
    assert "/ehr?error=" in bad.headers["Location"]
    assert "/ehr?error=" in client.get("/ehr/launch").headers["Location"]  # missing iss


def test_smart_launch_round_trip(monkeypatch):
    import base64, hashlib, urllib.parse
    import app as app_module
    pid = data.patient_list()[2]["id"]
    seen = {}

    def fake_http(url, form=None, token=None):
        if url.endswith("/.well-known/smart-configuration"):
            return {"authorization_endpoint": "https://ehr.example/authorize", "token_endpoint": "https://ehr.example/token"}
        if url == "https://ehr.example/token":
            seen["form"] = form
            return {"access_token": "tok", "patient": "fhir-123"}
        assert url == "https://ehr.example/fhir/Patient/fhir-123" and token == "tok"
        return {"resourceType": "Patient", "identifier": [{"system": data.MRN_SYSTEM, "value": data.synthetic_mrn(pid)}]}

    monkeypatch.setattr(app_module, "_http_json", fake_http)
    monkeypatch.setitem(app_module.SMART, "client_id", "test-client")
    client = app.test_client()
    res = client.get("/ehr/launch?iss=https://ehr.example/fhir&launch=abc")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(res.headers["Location"]).query)
    assert q["client_id"] == ["test-client"] and q["launch"] == ["abc"] and q["aud"] == ["https://ehr.example/fhir"]
    res = client.get(f"/ehr/callback?code=xyz&state={q['state'][0]}")
    assert res.headers["Location"].endswith(f"/patient/{pid}")
    verifier = seen["form"]["code_verifier"]
    assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() == q["code_challenge"][0]


def test_patients_tab_filters_update_summary():
    client = app.test_client()
    all_page = client.get("/").data
    assert b"Filtered cohort" not in all_page
    rows = data.patient_list()
    ids = data.select_ids({"decomp": "Yes", "age_min": "50"})
    assert ids and all(r["decomp"] == "Yes" and r["age"] >= 50 for r in rows if r["id"] in ids)
    page = client.get("/?decomp=Yes&age_min=50").data.decode()
    assert "Filtered cohort" in page and "Decompensated: Yes · Age ≥ 50" in page
    assert f"<strong>{len(ids)}</strong> of {len(rows)} participants" in page
    assert f"Open these {len(ids)} as cohort" in page and "/cohort?decomp=Yes&amp;age_min=50" in page
    assert data.select_ids({"age_min": "not-a-number"}) == [r["id"] for r in rows]  # bad input ignored
    flagged = data.select_ids({"attention": "1"})
    assert all(r["attention"] for r in rows if r["id"] in flagged)


# ── Synthetic generator ─────────────────────────────────────────────────────

DAILY_COLUMNS = ["name", "days_since_start", "valid80", "non_wear_pct", "class_5_min", "met_5min",
                 "average_hrv", "average_heart_rate", "steps", "spo2"]


def test_clinical_snapshot_on_synthetic_patient():
    pid = data.patient_list()[0]["id"]
    d = data.patient_detail(pid)
    c = d["clinical"]
    groups = [g["group"] for g in c["groups"]]
    assert groups == ["Liver severity", "Decompensation", "Encephalopathy"]  # in order; no LFI
    assert all(f["value"] is not None for g in c["groups"] for f in g["fields"])  # no "—" grids
    assert c["not_recorded"] == ["Liver Frailty Index"]  # the generator leaves it empty
    assert [f["label"] for f in c["study"]] == ["Site", "Arm", "Etiology"]
    severity = {f["label"]: f["value"] for f in c["groups"][0]["fields"]}
    assert isinstance(severity["MELD"], int) and severity["Child-Pugh"].split(", class ")[1] in {"A", "B"}
    assert "Ascites medication" not in str(c)  # not in the synthetic data
    assert "Patient-reported" not in str(c) and "PSQI" not in str(c)
    assert d["headline"].startswith(f"{d['age']} y · {d['sex'].lower()} · Child-Pugh ")


def test_clinical_snapshot_hides_empty_fields_and_lists_them_once():
    row = pd.Series({"meld": 14.0, "decomp": 1.0, "medication_for_ascites": "Yes", "ctp_score": 8.0,
                     "total_bilirubin_t0": float("nan"), "liver_frailty_index": float("nan"), "whichsite": "WCM"})
    c = data.clinical_snapshot(row, list(row.index))
    assert [g["group"] for g in c["groups"]] == ["Liver severity", "Decompensation"]  # Frailty: nothing to show
    assert c["groups"][0]["fields"] == [{"label": "MELD", "value": 14}, {"label": "Child-Pugh", "value": "8, class B"}]
    assert c["groups"][1]["fields"] == [{"label": "Decompensated", "value": "Yes"}]  # medication: own dropdown
    assert c["not_recorded"] == ["Bilirubin (mg/dL)", "Liver Frailty Index"]  # present but empty; absent columns skipped
    assert c["study"] == [{"label": "Site", "value": "WCM"}]
    assert data.clinical_snapshot(row, []) == {"groups": [], "study": [], "not_recorded": []}
    assert data._ctp_text(pd.Series({"ctp_class": "Child C"})) == "class C"
    assert data.clinical_headline(pd.Series({"age_yrs": 70.0, "sex": "Male", "ctp_class": "Child B", "ctp_score": 7.0,
                                             "meld": 14.0, "previous_encephalopathy": "Yes",
                                             "medication_for_hepatic_enc": "Yes"})) == \
        "70 y · male · Child-Pugh B (7) · MELD 14 · prior HE · on HE medication"


def test_patient_page_shows_clinical_snapshot():
    pid = data.patient_list()[0]["id"]
    page = app.test_client().get(f"/patient/{pid}").data.decode()
    assert 'id="clinicalSnapshot"' in page and "At enrollment" in page
    for g in data.patient_detail(pid)["clinical"]["groups"]:
        assert f'<div class="cp-group">{g["group"]}</div>' in page
    assert "Not recorded at enrollment: Liver Frailty Index" in page and 'class="cp-study"' in page
    assert "Ascites medication" not in page  # not in the synthetic data
    assert "Baseline (at enrollment)" not in page  # the snapshot replaces the old baseline card


def test_snapshot_has_three_dropdowns_with_synthetic_rows():
    pid = data.patient_list()[0]["id"]
    page = app.test_client().get(f"/patient/{pid}").data.decode()
    for title in data.SNAPSHOT_SECTIONS.values():
        assert f"<summary>{title}" in page
    assert '<details class="cp-section" id="cpStatus" open>' in page
    assert '<details class="cp-section" id="cpMedications">' in page and '<details class="cp-section" id="cpHistory">' in page
    assert '<span class="cp-tag">Synthetic list</span>' in page or '<span class="cp-tag">Synthetic events</span>' in page
    assert "Beta-blocker" in page and "Overt HE" in page  # enrollment fields in the dropdowns
    assert "Labs, medications over time" not in page


def test_synthetic_extras_deterministic_and_consistent_with_baseline():
    df, _ = data.load()
    seen_he = False
    for pid, g in df.groupby("name"):
        first, last = g.iloc[0], int(g["days_since_start"].max())
        a = data.synthetic_clinical_extras(pid, first, last)
        assert a == data.synthetic_clinical_extras(pid, first, last)  # same patient → same rows
        assert len(a["medications"]) <= 4 and len(a["history"]) <= 4
        days = [e["day"] for e in a["history"]]
        assert days == sorted(days) and all(1 <= d <= last for d in days)
        names = {m["name"] for m in a["medications"]}
        if first["he_meds"] == "Yes":
            seen_he = True
            assert names & {"Lactulose", "Rifaximin"}
        else:
            assert not names & {"Lactulose", "Rifaximin"}
        if first["ascites"] == 1:
            assert "Spironolactone" in names
        if first["decomp"] == 0:
            assert not any(e["kind"] == "Hospitalization" for e in a["history"])
    assert seen_he  # the default cohort exercises the HE branch


def test_snapshot_extras_never_generated_for_study_data():
    row = pd.Series({"he_meds": "Yes", "ascites": 1.0, "decomp": 1.0, "bb_use_t0": "Yes",
                     "medication_for_hepatic_enc": float("nan"), "previous_ascites": "No"})
    x = data.snapshot_extras("010", row, 180, "study", row.index)
    assert not x["synthetic"] and x["medications"] == [] and x["history"] == []  # no generated rows
    assert x["med_fields"] == [{"label": "Beta-blocker", "value": "Yes"}] and x["med_missing"] == ["HE medication"]
    assert x["history_fields"] == [{"label": "Ascites", "value": "No"}] and x["show_medications"] and x["show_history"]
    empty = data.snapshot_extras("010", row, 180, "study", [])
    assert not empty["show_medications"] and not empty["show_history"]


def test_synthetic_same_seed_same_output():
    a = data.synthetic_cohort(n=4, seed=11, min_days=3, max_days=6)
    pd.testing.assert_frame_equal(a, data.synthetic_cohort(n=4, seed=11, min_days=3, max_days=6))
    assert not a.equals(data.synthetic_cohort(n=4, seed=12, min_days=3, max_days=6))


def test_synthetic_schema_matches_what_the_dashboard_reads():
    df = data.harmonise_columns(data.synthetic_cohort(n=6, seed=3, min_days=4, max_days=8))
    baseline = [c for fields in data.CLINICAL_SNAPSHOT.values() for c, _ in fields if c in df and c != "ctp"]
    needed = set(DAILY_COLUMNS) | set(baseline) | set(data.GROUPS) | {"ctp_class", "anyhe", "age_yrs"}
    assert needed <= set(df.columns), needed - set(df.columns)
    numeric = ["days_since_start", "valid80", "non_wear_pct", "average_hrv", "average_heart_rate", "steps", "spo2",
               "age_yrs", "meld", "decomp", "anyhe", "anyOHE", "ascites", "phes_score", "ctp_score", "total_bilirubin_t0",
               "albumin_t0", "inr_t0"]
    assert all(pd.api.types.is_numeric_dtype(df[c]) for c in numeric)
    assert df["class_5_min"].str.fullmatch(r"[0-5]{288}").all()
    assert df["met_5min"].str.split(",").map(len).eq(288).all()
    assert set(df["valid80"]) <= {0, 1} and df["non_wear_pct"].between(0, 1).all()
    assert df.groupby("name")["days_since_start"].apply(lambda d: list(d) == list(range(len(d)))).all()  # day 0 first
    assert df.groupby("name")[baseline].nunique(dropna=False).le(1).all().all()  # baseline-only fields
    for col, spec in data.GROUPS.items():
        assert set(df[col].map(lambda v: int(v) if isinstance(v, float) else v)) <= set(spec["levels"]), col


def test_synthetic_overrides_take_effect():
    df = data.synthetic_cohort(n=5, seed=2, min_days=3, max_days=3, config={
        "decompensated_share": 1, "wear": {"invalid_day_share": 0}, "spo2": {"day_share": 0},
        "baseline": {"sex": {"Female": 1.0, "Male": 0.0}}, "decompensated": {"ctp_class": "Child C"}})
    assert df["decomp"].eq(1).all() and df["ctp_cat"].eq("Child B/C").all() and df["ctp_class"].eq("Child C").all()
    assert df["valid80"].eq(1).all() and df["spo2"].isna().all() and df["sex"].eq("Female").all()
    cfg = data.synthetic_config({"wear": {"gap_slots": 10}})
    assert cfg["wear"]["invalid_day_share"] == data.SYNTHETIC_DEFAULTS["wear"]["invalid_day_share"]  # rest of section kept
    assert data.SYNTHETIC_DEFAULTS["wear"]["gap_slots"] == 90  # defaults not mutated
    for bad in [{"nope": 1}, {"wear": {"nope": 1}}]:
        with pytest.raises(ValueError):
            data.synthetic_config(bad)


def test_dashboard_runs_on_a_small_generated_file(tmp_path, monkeypatch):
    path = tmp_path / "small.csv.gz"
    data.synthetic_cohort(n=6, seed=5, min_days=30, max_days=40).to_csv(path, index=False)
    monkeypatch.setattr(data, "DATA_SOURCE", "synthetic")
    monkeypatch.setattr(data, "SYNTHETIC_FILE", path)
    data.load.cache_clear(), data.patient_list.cache_clear()
    try:
        rows = data.patient_list()
        assert len(rows) == 6 and data.load()[1] == "synthetic"
        d = data.patient_detail(rows[0]["id"])
        assert d["tiles"] and d["nights"] and d["synthetic"]
        assert len(data.intraday(rows[0]["id"], 1, 3)["days"]) == 3
        assert data.cohort_summary()["participants"] == 6
        for group in data.GROUPS:
            data.cohort_compare(group, "night_rest_h")
        client = app.test_client()
        for url in ["/", f"/patient/{rows[0]['id']}", "/cohort", "/api/cohort?group=decomp&metric=steps"]:
            assert client.get(url).status_code == 200, url
    finally:
        data.load.cache_clear(), data.patient_list.cache_clear()


def test_generator_script_writes_data_and_settings(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"decompensated_share": 0}))
    out = tmp_path / "cohort.csv.gz"
    script = Path(data.__file__).parent / "scripts" / "generate_synthetic_data.py"
    subprocess.run([sys.executable, str(script), "--participants", "3", "--min-days", "2", "--max-days", "2",
                    "--seed", "4", "--config", str(cfg), "--output", str(out)], check=True, capture_output=True)
    df = pd.read_csv(out, dtype={"class_5_min": str})
    settings = json.loads((tmp_path / "cohort.settings.json").read_text())
    assert len(df) == 6 and df["decomp"].eq(0).all() and settings["seed"] == 4
    regen = data.synthetic_cohort(settings["participants"], settings["seed"], settings["min_days"],
                                  settings["max_days"], settings["config"])
    assert regen["class_5_min"].tolist() == df["class_5_min"].tolist()


@pytest.fixture
def public_demo(monkeypatch, tmp_path):
    # Study data "present" and requested, plus a synthetic file path: demo mode must use none of them.
    fake_study = tmp_path / "study.rds"
    fake_study.write_text("not really R data")
    monkeypatch.setattr(data, "PUBLIC_DEMO", True)
    monkeypatch.setattr(data, "DATA_FILE", fake_study)
    monkeypatch.setattr(data, "SYNTHETIC_FILE", tmp_path / "synthetic_he.csv.gz")
    monkeypatch.setattr(data, "_read_rds", lambda path: pytest.fail("demo mode read study data"))
    data.load.cache_clear()
    yield monkeypatch
    data.load.cache_clear()


def test_public_demo_serves_only_generated_synthetic_data(public_demo):
    for source in ["auto", "synthetic"]:
        public_demo.setattr(data, "DATA_SOURCE", source)
        data.load.cache_clear()
        df, label = data.load()
        assert label == "synthetic" and df["name"].str.startswith("DEMO-").all()


def test_public_demo_refuses_study_source_and_mrn_map(public_demo):
    public_demo.setattr(data, "DATA_SOURCE", "study")
    with pytest.raises(RuntimeError, match="synthetic data only"):
        data.load()
    public_demo.setattr(data, "DATA_SOURCE", "auto")
    public_demo.setenv("HE_MRN_MAP", "/somewhere/mrn_map.csv")
    data.load.cache_clear()
    with pytest.raises(RuntimeError, match="HE_MRN_MAP"):
        data.load()


def test_public_demo_pages_hide_local_only_features(public_demo, tmp_path):
    import app as app_module
    public_demo.setattr(app_module, "NOTEBOOK_DIRS", [tmp_path])
    (tmp_path / "secret_outputs.ipynb").write_text("{}")
    client = app.test_client()
    res = client.get("/")
    home = res.data.decode()
    assert res.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert "Public demo · synthetic data<span" in home and "Local only" not in home
    assert 'role="tooltip"' in home and "nothing you see describes a real person" in home
    assert 'class="banner"' not in home
    cohort = client.get("/cohort").data.decode()
    assert 'id="exportNb"' not in cohort and 'id="exportPdf"' in cohort
    nbs = client.get("/notebooks").data.decode()
    assert "secret_outputs" not in nbs and "localhost" not in nbs and "Open in JupyterLab" not in nbs
    assert client.post("/api/cohort/notebook", json={"group": "decomp"}).status_code == 404
    assert not list(tmp_path.glob("cohort_*.ipynb"))
    assert client.get("/ehr/launch?iss=http://169.254.169.254/latest").status_code == 404
    assert client.get("/ehr/callback?code=x&state=y").status_code == 404
    ehr = client.get("/ehr").data.decode()
    assert "Connection settings" not in ehr and "Simulate launch" in ehr
    report = client.get("/cohort/report?group=decomp").data.decode()
    assert "Synthetic demo data" in report and "De-identified study data" not in report
    assert "Sep 29" not in ehr


def test_public_demo_simulated_launch_and_shared_log(public_demo):
    import app as app_module
    public_demo.setattr(app_module, "LAUNCH_LOG", [])
    client = app.test_client()
    pid = data.patient_list()[0]["id"]
    assert client.get(f"/ehr/simulate?mrn={data.synthetic_mrn(pid)}").headers["Location"].endswith(f"/patient/{pid}")
    client.get("/ehr/simulate?mrn=typed-by-a-stranger")
    assert [entry["patient"] for entry in app_module.LAUNCH_LOG] == [pid]


def test_public_demo_notebooks_tab_links_to_viewer_jupyterlite_and_binder(public_demo):
    nbs = app.test_client().get("/notebooks").data.decode()
    assert "Nothing runs on this server" in nbs and "not clinical findings" in nbs
    assert "https://github.com/dicristea/jupyterhealth-he-dashboard/blob/main/docs/synthetic-data.md" in nbs
    for name in ["oura_data_exploration", "he_trajectories_phenotypes"]:
        assert f'href="/notebooks/view/{name}"' in nbs
        assert (f'href="https://dicristea.github.io/jupyterhealth-he-dashboard/lab/index.html'
                f'?path=notebooks/{name}.ipynb"') in nbs
        assert (f'href="https://mybinder.org/v2/gh/dicristea/jupyterhealth-he-dashboard/main'
                f'?urlpath=lab/tree/notebooks/{name}.ipynb"') in nbs
    assert nbs.index("View (read-only)") < nbs.index("Run in your browser") < nbs.index("Run on Binder")
    assert "localhost" not in nbs and "127.0.0.1" not in nbs and "Open in JupyterLab" not in nbs


def test_public_demo_notebook_links_follow_env(public_demo):
    import app as app_module
    public_demo.setattr(app_module, "NOTEBOOK_REPO", "someone/fork")
    public_demo.setattr(app_module, "NOTEBOOK_REF", "demo-day")
    public_demo.setattr(app_module, "JUPYTERLITE_URL", "https://example.org/lite")
    nbs = app.test_client().get("/notebooks").data.decode()
    assert "https://mybinder.org/v2/gh/someone/fork/demo-day?urlpath=lab/tree/notebooks/he_trajectories_phenotypes.ipynb" in nbs
    assert "https://example.org/lite/lab/index.html?path=notebooks/he_trajectories_phenotypes.ipynb" in nbs
    assert "https://github.com/someone/fork/blob/demo-day/docs/synthetic-data.md" in nbs
    assert "dicristea" not in nbs


def test_demo_notebook_list_matches_the_notebook_sources():
    import app as app_module
    # The speech notebook is local-only (needs recordings and Praat; scripts/render_notebooks.NOT_RENDERED).
    sources = sorted(p.stem for p in (Path(app_module.__file__).parent / "notebooks").glob("*.py")
                     if p.stem != "speech_features_starter")
    assert sorted(nb["name"] for nb in app_module.NOTEBOOKS) == sources


def test_local_notebooks_tab_cards_and_other_notebooks(monkeypatch, tmp_path):
    import app as app_module
    monkeypatch.setattr(app_module, "NOTEBOOK_DIRS", [tmp_path])
    monkeypatch.setattr(app_module, "JUPYTER_ROOT", tmp_path.parent)
    (tmp_path / "mine.ipynb").write_text("{}")
    nbs = app.test_client().get("/notebooks").data.decode()
    for nb in app_module.NOTEBOOKS:
        assert str(escape(nb["title"])) in nbs and f'href="/notebooks/code/{nb["name"]}"' in nbs
    assert f'href="/notebooks/open/{tmp_path.name}/mine.ipynb"' in nbs and "Your notebooks" in nbs
    assert "doesn't seem to be running" not in nbs and "jupyterNotice" not in nbs
    assert "mybinder.org" not in nbs and "Run in your browser" not in nbs and "/notebooks/view/" not in nbs


def test_notebook_open_refreshes_ipynb_and_starts_lab(monkeypatch, tmp_path):
    import app as app_module
    src_dir = tmp_path / "notebooks"
    src_dir.mkdir()
    (src_dir / "demo_nb.py").write_text("# %% [markdown]\n# # Hi\n\n# %%\nx = 1\n")
    monkeypatch.setattr(app_module, "NOTEBOOK_SRC", src_dir)
    monkeypatch.setattr(app_module, "JUPYTER_ROOT", tmp_path)
    monkeypatch.setattr(app_module, "_ensure_lab", lambda: "tok")
    client = app.test_client()
    res = client.get("/notebooks/open/notebooks/demo_nb.ipynb")
    assert res.status_code == 302 and res.headers["Location"] == \
        f"{app_module.JUPYTER_BASE_URL}/lab/tree/notebooks/demo_nb.ipynb?token=tok"
    assert (src_dir / "demo_nb.ipynb").is_file()  # made from the .py
    assert client.get("/notebooks/open/notebooks/demo_nb.ipynb", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/notebooks/open/../etc.ipynb").status_code == 404
    assert client.get("/notebooks/open/notebooks/demo_nb.py").status_code == 404
    monkeypatch.setattr(app_module, "_ensure_lab", lambda: None)
    assert "lab_error=1" in client.get("/notebooks/open/notebooks/demo_nb.ipynb").headers["Location"]


def test_public_demo_notebooks_never_mention_the_study_file(public_demo):
    client = app.test_client()
    pages = [client.get("/notebooks").data.decode()]
    import app as app_module
    for nb in app_module.NOTEBOOKS:
        pages.append(client.get(f"/notebooks/view/{nb['name']}").data.decode())
        pages.append((Path(app_module.__file__).parent / "notebooks" / f"{nb['name']}.py").read_text())
    for text in pages:
        assert "de-identified" not in text.lower() and "deid" not in text.lower() and "Nov 2026" not in text


def test_local_default_is_not_demo():
    import app as app_module
    data.load.cache_clear()
    assert not data.PUBLIC_DEMO
    assert 'app.run(host="127.0.0.1"' in Path(app_module.__file__).read_text()
    res = app.test_client().get("/")
    assert b"Local only" in res.data and "X-Robots-Tag" not in res.headers


def test_default_data_file_needs_exactly_one_rds(tmp_path):
    with pytest.raises(FileNotFoundError, match="found 0"):
        data.default_data_file(tmp_path)
    (tmp_path / "study.rds").write_text("")
    (tmp_path / "._study.rds").write_text("")  # macOS metadata file, ignored
    assert data.default_data_file(tmp_path) == tmp_path / "study.rds"
    (tmp_path / "other.rds").write_text("")
    with pytest.raises(FileNotFoundError, match="found 2"):
        data.default_data_file(tmp_path)


def test_study_source_without_a_file_explains_why(monkeypatch, tmp_path):
    monkeypatch.setattr(data, "DATA_SOURCE", "study")
    monkeypatch.setattr(data, "DATA_FILE", None)
    monkeypatch.setattr(data, "DEFAULT_DATA_DIR", tmp_path)
    data.load.cache_clear()
    with pytest.raises(FileNotFoundError, match="found 0.*HE_DATA_FILE"):
        data.load()
    data.load.cache_clear()


def test_healthz_answers_without_loading_data(monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module.data, "load", lambda: pytest.fail("health check loaded data"))
    res = app.test_client().get("/healthz")
    assert res.status_code == 200 and res.data == b"ok"


def test_demo_password_is_off_by_default():
    import app as app_module
    assert app_module.DEMO_PASSWORD == ""
    assert app.test_client().get("/").status_code == 200


def test_demo_password_protects_every_page_but_healthz(monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "DEMO_PASSWORD", "s3cret")
    monkeypatch.setattr(app_module, "LOGIN_FAIL_DELAY_S", 0)
    client = app.test_client()

    def log_in(user: str, pw: str, nxt: str = "/"):
        return client.post("/login", data={"username": user, "password": pw, "next": nxt})

    for url in ["/", "/cohort", "/patient/DEMO-01"]:
        res = client.get(url)
        assert res.status_code == 302 and res.headers["Location"].startswith("/login?next="), url
    assert client.get("/api/cohort").status_code == 401  # APIs answer, they don't redirect
    assert client.post("/api/cohort/notebook", json={}).status_code == 401
    assert client.get("/login").status_code == 200 and client.get("/healthz").status_code == 200
    assert client.get("/static/style.css").status_code == 200  # the login page needs it; code only
    assert log_in("demo", "wrong").status_code == 401
    assert log_in("someone", "s3cret").status_code == 401
    assert client.get("/", headers={"Authorization": "Bearer s3cret"}).status_code == 302
    res = log_in("demo", "s3cret", "/cohort?group=sex")
    assert res.status_code == 302 and res.headers["Location"] == "/cohort?group=sex"
    cookie = res.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert client.get("/").status_code == 200
    monkeypatch.setattr(app_module, "DEMO_USER", "henry")  # a new user or password logs everyone out
    assert client.get("/").status_code == 302
    log_in("henry", "s3cret")
    assert client.get("/").status_code == 200
    assert client.get("/logout").status_code == 302 and client.get("/").status_code == 302


def test_login_redirect_stays_on_this_site(monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "DEMO_PASSWORD", "s3cret")
    client = app.test_client()
    for evil in ["https://evil.example/", "//evil.example/x", "/\\evil.example", "javascript:alert(1)", ""]:
        res = client.post("/login", data={"username": "demo", "password": "s3cret", "next": evil})
        assert res.headers["Location"] == "/", evil


# ── Study-format columns: R step, column mapping, sleep ──────────────────────

SLEEP_COLUMNS = ["type", "sleep_phase_5_min", "num_sleep_phase_5_min", "time_in_bed_s", "total_sleep_duration",
                 "awake_time", "latency", "rem_sleep_duration", "rem_sleep_dur_pct", "restless_periods",
                 "breathing_disturbance_index", "average_breath", "bedtime_start_h18", "bedtime_end_h18"]
NEW_BASELINE = ["total_bilirubin_t0", "albumin_t0", "inr_t0", "ctp_score", "current_ascites", "previous_ascites",
                "previous_encephalopathy", "bb_use_t0", "medication_for_hepatic_enc", "whichsite", "whicharm"]
DATE_RE = r"(?:19|20)\d\d-[01]\d-[0-3]\d"


def _no_dates(df: pd.DataFrame) -> None:
    assert not any(pd.api.types.is_datetime64_any_dtype(df[c]) for c in df)
    text = df.select_dtypes(exclude="number")
    assert not any(text[c].astype(str).str.contains(DATE_RE).any() for c in text)


def test_r_step_text_keeps_no_dates():
    r = data.R_TO_CSV
    assert "x$bedtime_start <- NULL" in r and "x$bedtime_end <- NULL" in r and "x$movement_30_sec <- NULL" in r
    assert 'stop("date columns would leave the R step' in r and "haven::as_factor" in r and "met_5min" in r
    assert data.CACHE_VERSION == "v3"


R_FAKE = r"""
args <- commandArgs(trailingOnly = TRUE)
tz <- "America/New_York"
x <- tibble::tibble(
  name = c("a", "a", "b"), days_since_start = c(0L, 1L, 0L),
  bedtime_start = as.POSIXct(c("2026-01-10 22:30:00", "2026-03-07 23:00:00", NA), tz = tz),
  bedtime_end = as.POSIXct(c("2026-01-11 06:15:00", "2026-03-08 07:00:00", NA), tz = tz),
  sleep_phase_5_min = c("4412", "4123", NA), type = c("long_sleep", "sleep", NA),
  movement_30_sec = c(Inf, 1e296, NA),
  met_val = list(rep(1, 1440), rep(2, 1440), 1:3),
  sleep_phase5_val = list(1:4, 1:4, integer(0)),
  sex = haven::labelled(c(1, 2, 1), c(Female = 1, Male = 2)))
if (length(args) > 1) x$visit_date <- as.Date("2026-01-10")
saveRDS(x, args[1])
"""


def _rscript_ready() -> bool:
    import shutil
    if not shutil.which("Rscript"):
        return False
    ok = subprocess.run(["Rscript", "-e", "quit(status = !all(sapply(c('haven', 'tibble'), requireNamespace)))"],
                        capture_output=True)
    return ok.returncode == 0


@pytest.mark.skipif(not _rscript_ready(), reason="needs Rscript with haven and tibble")
def test_r_step_turns_bedtimes_into_clock_hours_and_drops_dates(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "CACHE_DIR", tmp_path / "cache")
    make = tmp_path / "make.R"
    make.write_text(R_FAKE)
    rds = tmp_path / "fake.rds"
    subprocess.run(["Rscript", str(make), str(rds)], check=True, capture_output=True)
    df = data._read_rds(rds)
    assert (tmp_path / "cache" / "fake.v3.csv").exists()
    assert {"bedtime_start", "bedtime_end", "movement_30_sec", "met_val", "sleep_phase5_val"}.isdisjoint(df.columns)
    assert {"bedtime_start_h18", "bedtime_end_h18", "time_in_bed_s", "met_5min", "type"} <= set(df.columns)
    _no_dates(df)
    assert df["bedtime_start_h18"].tolist()[:2] == [4.5, 5.0]  # 22:30 and 23:00, hours after 18:00
    assert df["bedtime_end_h18"].tolist()[:2] == [12.25, 13.0]  # 06:15 and 07:00 on the wake day
    assert df["time_in_bed_s"].tolist()[:2] == [7.75 * 3600, 7 * 3600]  # the DST night lasted 7 real hours
    assert df.loc[2, ["bedtime_start_h18", "bedtime_end_h18", "time_in_bed_s"]].isna().all()
    assert df["sleep_phase_5_min"].tolist()[:2] == ["4412", "4123"]  # kept as text, not a number
    assert df["sex"].tolist() == ["Female", "Male", "Female"] and df["met_5min"].isna().tolist() == [False, False, True]
    with_date = tmp_path / "dated.rds"
    subprocess.run(["Rscript", str(make), str(with_date), "date"], check=True, capture_output=True)
    with pytest.raises(subprocess.CalledProcessError):  # an unexpected date column stops the conversion
        data._read_rds(with_date)
    assert not (tmp_path / "cache" / "dated.v3.csv").exists()


def test_ascites_derived_from_current_and_previous():
    df = pd.DataFrame({"current_ascites": ["Slight", "Moderate", "Absent", "Absent", None, None, "Absent"],
                       "previous_ascites": ["No", None, "Yes", "No", "Yes", "No", None]})
    assert data.derive_ascites(df).tolist()[:5] == [1.0, 1.0, 1.0, 0.0, 1.0]
    assert data.derive_ascites(df).iloc[5:].isna().all()  # a "No" alone doesn't rule ascites out
    only_current = pd.DataFrame({"current_ascites": ["Slight", "Absent"]})
    assert data.derive_ascites(only_current).iloc[0] == 1 and pd.isna(data.derive_ascites(only_current).iloc[1])


def test_harmonise_columns_drops_duplicate_day_and_missing_codes():
    df = pd.DataFrame({"name": ["a", "a", "a", "b"], "days_since_start": [0, 1, 1, 0], "valid80": [1, 0, 1, 1],
                       "steps": [10, 20, 30, 40], "inr_t0": [1.1, 1.1, 1.1, -999.0],
                       "current_ascites": ["Absent"] * 3 + ["Slight"], "previous_ascites": ["No"] * 3 + ["No"]})
    out = data.harmonise_columns(df)
    assert out[["name", "days_since_start"]].values.tolist() == [["a", 0], ["a", 1], ["b", 0]]
    assert out["steps"].tolist() == [10, 30, 40]  # of the duplicated day, the valid-wear copy is kept
    assert pd.isna(out["inr_t0"].iloc[-1]) and out["ascites"].tolist() == [0.0, 0.0, 1.0]
    same = data.harmonise_columns(df.assign(valid80=1))
    assert same["steps"].tolist() == [10, 20, 40]  # otherwise the first in file order
    assert "ascites" not in data.harmonise_columns(df.drop(columns=["current_ascites", "previous_ascites"]))


def test_study_like_columns_missing_or_renamed_dont_break_the_app(tmp_path, monkeypatch):
    # The Nov 2026 file: no ascites/day_type/psqiglobal/cldq_*/robust/frail, days from 0, one duplicated day.
    df = data.synthetic_cohort(n=5, seed=9, min_days=30, max_days=30)
    df = df.drop(columns=["psqiglobal", "cldq_total"])
    df = pd.concat([df, df.iloc[[3]]], ignore_index=True)
    path = tmp_path / "study_like.csv"
    df.to_csv(path, index=False)
    monkeypatch.setattr(data, "DATA_SOURCE", "synthetic")
    monkeypatch.setattr(data, "SYNTHETIC_FILE", path)
    data.load.cache_clear(), data.patient_list.cache_clear()
    try:
        loaded, _ = data.load()
        assert len(loaded) == 150 and not loaded.duplicated(["name", "days_since_start"]).any()
        assert "grp_ascites" in loaded and set(loaded["ascites"].dropna()) <= {0.0, 1.0}
        assert loaded["sleep_phase_5_min"].dropna().map(type).eq(str).all()
        pid = data.patient_list()[0]["id"]
        d = data.patient_detail(pid)
        assert d["days"][0] == 0 and len(d["days"]) == 30
        assert [x["day"] for x in data.intraday(pid, 0, 3)["days"]] == [0, 1, 2]
        assert data.select_ids({"ascites": "Yes"}) is not None
        client = app.test_client()
        for url in ["/", f"/patient/{pid}", "/cohort", "/api/cohort?group=ascites&metric=steps"]:
            assert client.get(url).status_code == 200, url
    finally:
        data.load.cache_clear(), data.patient_list.cache_clear()


def test_synthetic_sleep_columns_match_the_cache_format():
    df = data.synthetic_cohort(n=12, seed=4, min_days=60, max_days=60)
    assert set(SLEEP_COLUMNS + NEW_BASELINE) <= set(df.columns) and "movement_30_sec" not in df
    assert "bedtime_start" not in df and "ascites" not in df  # as in the study cache
    _no_dates(df)
    rec = df[df["type"].notna()]
    none = df[df["type"].isna()]
    assert 0.04 < len(none) / len(df) < 0.14  # ~8% of days have no sleep record
    assert none[SLEEP_COLUMNS].isna().all().all() and none["average_hrv"].isna().all()
    assert rec["type"].eq("long_sleep").mean() > 0.7 and set(rec["type"]) <= {"long_sleep", "sleep", "late_nap", "rest"}
    hyp = rec["sleep_phase_5_min"]
    assert hyp.str.fullmatch(r"[1-4]+").all() and hyp.str.len().eq(rec["num_sleep_phase_5_min"]).all()
    assert (rec["time_in_bed_s"] - 300 * rec["num_sleep_phase_5_min"]).abs().le(150).all()
    asleep = hyp.str.count("1") + hyp.str.count("2") + hyp.str.count("3")
    assert rec["total_sleep_duration"].eq(300 * asleep).all()
    assert (rec["total_sleep_duration"] + rec["awake_time"]).eq(rec["time_in_bed_s"]).all()
    assert rec["rem_sleep_duration"].eq(300 * hyp.str.count("3")).all()
    assert rec["latency"].eq(300 * (hyp.str.len() - hyp.str.lstrip("4").str.len())).all()
    assert rec["rem_sleep_dur_pct"].dropna().between(0, 1).all()
    span = (rec["bedtime_end_h18"] - rec["bedtime_start_h18"]) * 3600
    assert (span - rec["time_in_bed_s"]).abs().le(60).all() and (span > 0).all()
    main = rec[rec["type"].eq("long_sleep")]
    assert main["bedtime_start_h18"].between(0, 12).all()
    assert 0.2 < rec["breathing_disturbance_index"].notna().mean() < 0.6
    for col in NEW_BASELINE:
        assert df.groupby("name")[col].nunique(dropna=False).eq(1).all(), col
    assert set(df["current_ascites"]) <= {"Absent", "Slight", "Moderate"} and set(df["whichsite"]) <= {"WCM", "CUMC"}


def test_synthetic_sleep_is_worse_when_decompensated():
    df = data.synthetic_cohort(n=40, seed=7, min_days=40, max_days=40)
    main = df[df["type"].eq("long_sleep")]
    by = main.groupby("decomp")
    eff = (main["total_sleep_duration"] / main["time_in_bed_s"]).groupby(main["decomp"]).median()
    deep = (main["sleep_phase_5_min"].str.count("1") / main["sleep_phase_5_min"].str.len()).groupby(main["decomp"]).mean()
    assert by["bedtime_start_h18"].median()[1] > by["bedtime_start_h18"].median()[0] + 0.5  # later
    assert by["total_sleep_duration"].median()[1] < by["total_sleep_duration"].median()[0]  # shorter
    assert eff[1] < eff[0] - 0.05 and deep[1] < deep[0]  # more fragmented, less deep
    assert by["latency"].median()[1] > by["latency"].median()[0]
    non_main = df["type"].isin(["sleep", "late_nap", "rest"]).groupby(df["decomp"]).mean()
    assert non_main[1] > non_main[0]
    people = data.harmonise_columns(df).groupby("name").first()
    assert people.groupby("decomp")["ctp_score"].max()[0] <= 6 and people.groupby("decomp")["ctp_score"].min()[1] >= 7
    assert people.groupby("decomp")["total_bilirubin_t0"].median()[1] > people.groupby("decomp")["total_bilirubin_t0"].median()[0]


def test_parse_hypnogram():
    assert data.parse_hypnogram("4412233") == {"deep": 5.0, "light": 10.0, "rem": 10.0, "awake": 10.0}
    for bad in ["", None, float("nan"), "12x4", 1234]:
        out = data.parse_hypnogram(bad)
        assert set(out) == {"deep", "light", "rem", "awake"} and all(pd.isna(v) for v in out.values())
    df = data.synthetic_cohort(n=2, seed=1, min_days=5, max_days=5).dropna(subset=["sleep_phase_5_min"])
    for _, r in df.iterrows():
        m = data.parse_hypnogram(r["sleep_phase_5_min"])
        assert sum(m.values()) == 5 * r["num_sleep_phase_5_min"] and m["rem"] * 60 == r["rem_sleep_duration"]


# ── Patient-page sleep measures ──────────────────────────────────────────────

def _night_rows(**cols) -> pd.DataFrame:
    n = len(next(iter(cols.values())))
    base = {"name": ["a"] * n, "days_since_start": list(range(n)), "type": ["long_sleep"] * n,
            "time_in_bed_s": [8 * 3600.0] * n, "total_sleep_duration": [7 * 3600.0] * n,
            "bedtime_start_h18": [5.0] * n, "bedtime_end_h18": [13.0] * n, "sleep_phase_5_min": ["2" * 96] * n}
    return pd.DataFrame({**base, **cols})


def test_main_night_rule():
    df = _night_rows(type=["long_sleep", "sleep", "late_nap", None, "long_sleep", "long_sleep", "long_sleep"],
                     time_in_bed_s=[8 * 3600.0] * 4 + [2.5 * 3600, 17 * 3600, 8 * 3600],
                     total_sleep_duration=[7 * 3600.0] * 6 + [0.5 * 3600])
    n = data.sleep_nights(df)
    assert n["main_sleep"].tolist() == [True, False, False, False, False, False, False]  # type, 3–16 h in bed, 1–14 h asleep
    assert n.loc[1:, ["sleep_mid_h18", "sleep_tst_h", "sleep_eff_pct", "sleep_waso_min"]].isna().all().all()  # never 0
    no_sleep_columns = data.sleep_nights(pd.DataFrame({"name": ["a"], "days_since_start": [0]}))
    assert not no_sleep_columns["main_sleep"].any() and no_sleep_columns["sleep_tst_h"].isna().all()


def test_sleep_timing_across_midnight_uses_hours_after_18():
    # 23:30 → 07:30 and 00:30 → 08:30: midpoints 03:30 and 04:30, not a 20-hour jump at midnight
    df = _night_rows(bedtime_start_h18=[5.5, 6.5], bedtime_end_h18=[13.5, 14.5])
    n = data.sleep_nights(df)
    assert n["sleep_mid_h18"].tolist() == [9.5, 10.5]
    assert [data._clock(18 + h) for h in n["sleep_mid_h18"]] == ["03:30", "04:30"]
    # Bedtimes alternating 23:30 / 00:30 move by an hour, so their SD is ~30 min (it would be ~12 h on a 0–24 clock)
    alt = _night_rows(bedtime_start_h18=[5.5, 6.5] * 7, bedtime_end_h18=[13.5, 14.5] * 7)
    sd = data.bedtime_sd_min(alt.join(data.sleep_nights(alt)))
    assert sd.iloc[:6].isna().all() and 29 < sd.iloc[-1] < 33  # needs 7 nights in the 14-day window


def test_efficiency_and_waso_from_the_night_record():
    # 4 slots awake before sleep, one 3-slot awakening, 2 slots awake at the end: WASO counts only the middle one
    hyp = "4444" + "1" * 30 + "444" + "2" * 50 + "3" * 7 + "44"
    tib = len(hyp) * 300.0
    df = _night_rows(sleep_phase_5_min=[hyp], time_in_bed_s=[tib], total_sleep_duration=[87 * 300.0],
                     bedtime_start_h18=[5.0], bedtime_end_h18=[5.0 + tib / 3600])
    n = data.sleep_nights(df).iloc[0]
    assert n["sleep_waso_min"] == 15 and n["sleep_eff_pct"] == pytest.approx(100 * 87 / 96)
    assert n["sleep_tst_h"] == pytest.approx(87 * 300 / 3600)
    bad = data.sleep_nights(df.assign(sleep_phase_5_min=["12x4"])).iloc[0]
    assert bad["main_sleep"] and pd.isna(bad["sleep_waso_min"]) and bad["sleep_eff_pct"] > 0  # stages need a clean record


def test_status_uses_recent_window_and_minimum_nights():
    days = range(0, 60)
    base = pd.Series([7.0 + 0.1 * (d % 5) for d in days], index=days, dtype=float)
    status, last7, q1, med, q3, n = data._status(base, 59)
    assert n == 14
    assert status == "typical" and q1 <= last7 <= q3
    later = base.copy()
    later.loc[50:] = 9.0
    assert data._status(later, 59)[0] == "above"  # the recent window isn't part of the usual range
    sparse = later.copy()
    sparse.loc[46:] = float("nan")
    sparse.loc[[50, 53, 56]] = 9.0  # 3 nights in the last 14 days
    assert data._status(sparse, 59)[0] == "insufficient"
    sparse.loc[58] = 9.0  # the 4th
    assert data._status(sparse, 59)[0] == "above"
    short_history = later.loc[33:]  # 13 nights before the recent window
    assert data._status(short_history, 59)[0] == "insufficient"
    assert data._status(later.loc[32:], 59)[0] == "above"  # 14 are enough


def test_tiles_say_direction_in_words_and_not_enough_nights():
    days = list(range(60))
    vals = pd.Series([9.5] * 46 + [11.0] * 14, index=days, dtype=float)
    weeks = [d // 7 + 1 for d in days]
    t = data._tile("sleep_mid_h18", vals, 59, weeks)
    assert t["pill"] == "Later than usual" and t["tone"] == "worse" and t["last7"] == "05:00" and t["range"] == ["03:30", "03:30"]
    t = data._tile("sleep_tst_h", pd.Series([6.0] * 46 + [9.0] * 14, index=days), 59, weeks)
    assert t["pill"] == "Longer than usual" and t["tone"] == "shift"  # long sleep is a change, not "better"
    t = data._tile("sleep_eff_pct", pd.Series([85.0] * 10 + [float("nan")] * 50, index=days), 59, weeks)
    assert t["pill"] == "Not enough nights" and t["tone"] == "none" and t["last7"] is None and t["range"] is None
    assert "needs 4" in t["why"]
    assert data._tile("steps", pd.Series([5000.0] * 60, index=days), 59, weeks)["pill"] == "Usual range"


def test_attention_flags_worse_sleep_in_words():
    days = range(60)
    full = pd.DataFrame({k: float("nan") for k in data.SLEEP_TILES}, index=days)
    full["sleep_mid_h18"] = [9.0 + 0.05 * (d % 3) for d in days]
    full.loc[46:, "sleep_mid_h18"] = 10.4
    assert data._attention(full, 59) == ["Sleep midpoint 1 h 21 min later than usual"]  # vs the usual median 9.05
    full.loc[46:, "sleep_mid_h18"] = 7.0  # earlier is not flagged
    assert data._attention(full, 59) == []
    assert data._amount("sleep_eff_pct", -8.2) == "8 points" and data._amount("sleep_waso_min", 25) == "25 min"


def test_patient_payload_sleep_on_synthetic_data():
    df, _ = data.load()
    for pid in df["name"].unique()[:6]:
        d = data.patient_detail(pid)
        g = df[df["name"] == pid].set_index("days_since_start")
        mid = next(t for t in d["tiles"] if t["key"] == "sleep_mid_h18")
        assert mid["clock"] and mid["pill"] in {"Earlier than usual", "Later than usual", "Usual range", "Not enough nights"}
        assert d["mid_band"] == mid["band"]
        s = d["series"]["sleep_tst_h"]
        assert len(s["values"]) == len(d["days"]) and s["noun"] == "night"
        for day, v, shade in zip(d["days"], s["values"], s["shade"]):
            main = day in g.index and bool(g.at[day, "main_sleep"])
            assert (v is None) == (not main) and shade == (not main)  # missing night → missing, never 0
        assert d["series"]["steps"]["noun"] == "day"
        for n in d["nights"]:
            if n["type"] is None:
                continue
            assert n["start"] < n["end"] and set(n["hyp"]) <= set("1234")
            assert n["main"] == (n["type"] == "long_sleep" and n["tst"] >= 1)
    rows = {r["id"]: r for r in data.patient_list()}
    r = rows[df["name"].iloc[0]]
    assert {"sleep_mid", "sleep_mid_clock", "sleep_tst", "sleep_eff", "spark"} <= set(r) and "night_rest" not in r
    assert all(isinstance(f, str) and "than usual" in f for row in rows.values() for f in row["attention"])


# --- Cohort: sleep measures, test display, minimums (cohort_statistician.md section 5) ---

def test_fmt_p_never_says_zero():
    assert data.fmt_p(1e-10) == "p < 0.001" and data.fmt_p(0.0004) == "p < 0.001"
    assert data.fmt_p(0.0123) == "p = 0.012" and data.fmt_p(0.5) == "p = 0.50"
    client = app.test_client()
    for group in data.GROUPS:
        for metric in data.COHORT_METRICS:
            res = client.get(f"/api/cohort?group={group}&metric={metric}")
            assert res.status_code == 200, (group, metric)
            d = res.get_json()
            assert d.get("p_text") != "p = 0" and "p = 0<" not in json.dumps(d), (group, metric)
            assert d["direction"]


def test_sleep_measures_in_cohort_compare_and_api():
    client = app.test_client()
    for key in data.SLEEP_METRICS:
        d = client.get(f"/api/cohort?group=decomp&metric={key}").get_json()
        assert len(d["levels"]) == 2 and all(lv["n"] > 0 and lv["nights"] > 0 for lv in d["levels"]), key
        assert d["noun"] == "nights" and d["basis"] == "main nights"
    d = data.cohort_compare("decomp", "sleep_eff_pct")
    assert d["effect_r"] < 0 and d["p_value"] < 0.05 and "lower" in d["direction"]
    assert d["direction"].startswith("Decompensated tend lower (r = ")
    assert client.get("/api/cohort?group=decomp").get_json()["metric_key"] == "sleep_mid_h18"  # the default


def test_sleep_and_nightly_vitals_ignore_valid80(monkeypatch):
    df, source = data.load()
    before = {m: data.cohort_compare("decomp", m) for m in ["sleep_mid_h18", "average_hrv", "steps"]}
    changed = df.copy()
    changed.loc[changed["main_sleep"].eq(True).to_numpy().nonzero()[0][::5], "valid80"] = 0
    monkeypatch.setattr(data, "load", lambda: (changed, source))
    for m in ["sleep_mid_h18", "average_hrv"]:
        assert data.cohort_compare("decomp", m) == before[m], m
    assert data.cohort_compare("decomp", "steps") != before["steps"]  # activity measures still use valid days


def test_person_needs_14_values(monkeypatch):
    df, source = data.load()
    pid = df.loc[df["grp_decomp"].eq("Compensated"), "name"].iloc[0]

    def with_nights(n: int) -> dict:
        d = df.copy()
        mine = d.index[(d["name"] == pid) & d["sleep_tst_h"].notna()]
        d.loc[mine[n:], "sleep_tst_h"] = float("nan")
        monkeypatch.setattr(data, "load", lambda: (d, source))
        return data.cohort_compare("decomp", "sleep_tst_h")

    out13, out14 = with_nights(13), with_nights(14)
    assert out13["excluded"] == 1 and out13["levels"][0]["excluded"] == 1
    assert out14["excluded"] == 0 and out14["levels"][0]["n"] == out13["levels"][0]["n"] + 1


def _few_in_level(monkeypatch, group: str, level: str, other: str, keep: int) -> None:
    """Synthetic cohort with only `keep` people left in `level` of a Compare-by group (the rest moved to `other`)."""
    df, source = data.load()
    col = f"grp_{group}"
    ids = sorted(df.loc[df[col] == level, "name"].unique())[keep:]
    df = df.assign(**{col: df[col].mask(df["name"].isin(ids), other)})
    monkeypatch.setattr(data, "load", lambda: (df, source))


def test_small_group_gets_no_test(monkeypatch):
    _few_in_level(monkeypatch, "ascites", "Ascites", "No ascites", keep=3)
    d = data.cohort_compare("ascites", "sleep_mid_h18")
    assert min(lv["n"] for lv in d["levels"]) < data.MIN_TEST
    assert "p_value" not in d and "p_text" not in d and d["direction"].startswith("Too few patients for a test")
    assert all(lv["people"] for lv in d["levels"])  # still drawn


def test_three_level_group_uses_kruskal_wallis(monkeypatch):
    d = data.cohort_compare("meld_band", "sleep_eff_pct")
    assert [lv["name"] for lv in d["levels"]] == ["MELD <10", "MELD 10–14", "MELD ≥15"]
    assert d["test"] == "Kruskal-Wallis" and len(d["pairwise"]) == 2 and "h" in d
    assert all("p" not in pair for pair in d["pairwise"]) and d["p_text"].startswith("p ")
    groups = {**data.GROUPS, "sex3": {"label": "Three", "levels": {"A": "A", "B": "B", "C": "C"}}}
    df, source = data.load()
    ids = sorted(df["name"].unique())
    d3 = df.assign(grp_sex3=df["name"].map({p: "ABC"[i % 3] for i, p in enumerate(ids)}))
    monkeypatch.setattr(data, "GROUPS", groups)
    monkeypatch.setattr(data, "load", lambda: (d3, source))
    out = data.cohort_compare("sex3", "steps")
    assert out["test"] == "Kruskal-Wallis" and [p["level"] for p in out["pairwise"]] == ["B", "C"]


def test_untested_level_is_named(monkeypatch):
    _few_in_level(monkeypatch, "ctp_class", "Child C", "Child B", keep=1)
    d = data.cohort_compare("ctp_class", "sleep_mid_h18")
    assert d["untested_levels"] == ["Child C"] and d["test"] == "Mann-Whitney"


def test_effect_r_sign():
    lo, hi = pd.Series([1.0, 2, 3, 4, 5]), pd.Series([6.0, 7, 8, 9, 10])
    assert data._effect_r(lo, hi) == 1 and data._effect_r(hi, lo) == -1


def test_groups_labelled_and_menu_on_page():
    for key, spec in data.GROUPS.items():
        assert key == "sex" or "enrollment" in spec["label"], key
    assert {k for keys in data.GROUP_MENU.values() for k in keys} == set(data.GROUPS)
    summary = data.cohort_summary()
    assert {"bb_use_t0", "ctp_class", "meld_band", "ascites_now", "etiology_group"} <= set(summary["groups"])
    page = app.test_client().get("/cohort").data.decode()
    for label in ["Encephalopathy", "Liver severity", "Medications", "Demographics", "Sleep (Oura)", "Activity",
                  "Physiology"]:
        assert f'<optgroup label="{label}">' in page, label
    assert '<option value="sleep_mid_h18" selected>' in page
    assert "This file has no sleep stages" not in page and "median sleep midpoint" in page
    assert "a difference worth a closer look" not in page and "1 in 20" in page
    assert data.cohort_summary()["sleep_mid"] is not None


def test_derived_groups():
    df = pd.DataFrame({"meld": [6, 10, 14, 15, None], "current_ascites": ["Absent", "Slight", "Moderate", None, "Absent"],
                       "cirrhosis_etiology": ["Alcohol", "NAFLD", "Hepatitis C", None, "MASLD"]})
    g = data.derive_groups(df)
    assert list(g["meld_band"].fillna("-")) == ["<10", "10–14", "10–14", "≥15", "-"]
    assert list(g["ascites_now"].fillna("-")) == ["Absent", "Present", "Present", "-", "Absent"]
    assert list(g["etiology_group"].fillna("-")) == ["Alcohol", "MASLD", "Other", "-", "MASLD"]


def test_report_and_notebook_include_sleep_measures(tmp_path, monkeypatch):
    client = app.test_client()
    report = client.get("/cohort/report?group=decomp").data.decode()
    for key in data.SLEEP_METRICS:
        assert f'"{key}"' in report, key
    assert "This file has no sleep stages" not in report and "p_text" in report
    assert report.index("\"sleep_mid_h18\"") < report.index("\"steps\"") < report.index("\"average_heart_rate\"")
    import app as app_module
    import matplotlib
    matplotlib.use("Agg")
    monkeypatch.setattr("matplotlib.pyplot.show", lambda *a, **k: None)
    nb = app_module._build_cohort_notebook({"ids": None, "label": "All patients", "params": {}}, "meld_band")
    code = "\n".join(c.source for c in nb.cells if c.cell_type == "code")
    assert "COHORT_METRICS" in code
    monkeypatch.chdir(Path(__file__).resolve().parents[1] / "notebooks")
    ns: dict = {}
    for cell in nb.cells:
        if cell.cell_type == "code" and cell.source.strip():
            exec(cell.source, ns)  # the exported notebook runs on synthetic data


# --- Patient page: recent window, info popover, nightly vitals on main nights ---

def test_window_param_is_validated():
    client = app.test_client()
    pid = data.patient_list()[0]["id"]
    for raw, expect in [("7", 7), ("14", 14), ("", 14), ("30", 14), ("abc", 14), ("-7", 14)]:
        res = client.get(f"/patient/{pid}?window={raw}")
        assert res.status_code == 200, raw
        page = res.data.decode()
        assert f'href="?window={expect}" class="on" aria-current="true"' in page, raw
        assert f"median of the last {expect} study days" in page, raw
    with pytest.raises(ValueError):
        data.patient_detail(pid, 30)


def test_info_popover_present():
    pid = data.patient_list()[0]["id"]
    page = app.test_client().get(f"/patient/{pid}").data.decode()
    assert 'class="info-btn"' in page and 'aria-controls="statsInfoTip"' in page and 'role="tooltip"' in page
    assert "3–16 h in bed and 1–14 h asleep" in page and "≥80% ring wear" in page and "never counted as zero" in page
    assert "HRV, heart rate, SpO₂ (main nights)" in page


def test_nightly_vitals_use_main_nights():
    df, _ = data.load()
    assert data.nightly("average_hrv") and data.nightly("spo2") and data.nightly("sleep_mid_h18")
    assert not data.nightly("steps") and not data.nightly("sri")
    for pid in df["name"].unique():
        g = df[df["name"] == pid].set_index("days_since_start")
        d = data.patient_detail(pid)
        hrv = dict(zip(d["days"], d["series"]["average_hrv"]["values"]))
        main_invalid = g.index[g["main_sleep"].eq(True) & g["valid80"].ne(1) & g["average_hrv"].notna()]
        not_main = g.index[g["main_sleep"].ne(True) & g["average_hrv"].notna()]
        if len(main_invalid) and len(not_main):
            assert all(hrv[day] is not None for day in main_invalid)
            assert all(hrv[day] is None for day in not_main)
            assert d["series"]["average_hrv"]["noun"] == "night"
            return
    pytest.fail("no synthetic patient with both cases")


def test_window_changes_recent_value_and_bedtime_sd():
    nan = float("nan")
    bed = pd.Series([1.0] * 50 + [1.0, 3.0, 1.0, 3.0, nan, nan, nan], index=range(57))  # 4 nights in the last 7
    per_night, (value, n) = data._bed_sd(bed, 7)
    assert n == 4 and value == pytest.approx(pd.Series([1.0, 3, 1, 3]).std() * 60)
    _, (value3, n3) = data._bed_sd(bed.where(bed.index != 53), 7)
    assert n3 == 3 and pd.isna(value3)  # 7-day window needs 4 nights
    _, (v14, n14) = data._bed_sd(bed, 14)
    assert n14 == 11 and pd.notna(v14)  # 14-day window needs 7
    pid = data.patient_list()[0]["id"]
    d7, d14 = data.patient_detail(pid, 7), data.patient_detail(pid, 14)
    assert d7["window"] == 7 and d14["window"] == 14
    sd7 = next(t for t in d7["tiles"] if t["key"] == "sleep_bed_sd_min")
    assert "last 7 days" in sd7["why"] and "in the 7 study days" in d7["metrics"]["sleep_bed_sd_min"]["help"]
