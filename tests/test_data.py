import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
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
    assert len(d["summary"]) == 4 and d["profile"]["met"]["p50"] and abs(sum(c["share"] for c in d["time_in_class"]) - 1) < 0.01
    assert all({"spark", "band", "trend", "n_obs"} <= set(t) for t in d["tiles"])
    client = app.test_client()
    body = client.get(f"/patient/{pid}").data
    assert b"Synthetic \xc2\xb7 de-identified demo" in body
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

DAILY_COLUMNS = ["name", "days_since_start", "day_type", "valid80", "non_wear_pct", "class_5_min", "met_5min",
                 "average_hrv", "average_heart_rate", "steps", "spo2"]


def test_synthetic_same_seed_same_output():
    a = data.synthetic_cohort(n=4, seed=11, min_days=3, max_days=6)
    pd.testing.assert_frame_equal(a, data.synthetic_cohort(n=4, seed=11, min_days=3, max_days=6))
    assert not a.equals(data.synthetic_cohort(n=4, seed=12, min_days=3, max_days=6))


def test_synthetic_schema_matches_what_the_dashboard_reads():
    df = data.synthetic_cohort(n=6, seed=3, min_days=4, max_days=8)
    baseline = [c for c, _ in data.BASELINE_FIELDS]
    needed = set(DAILY_COLUMNS) | set(baseline) | set(data.GROUPS) | {"ctp_class", "anyhe", "age_yrs"}
    assert needed <= set(df.columns), needed - set(df.columns)
    numeric = ["days_since_start", "valid80", "non_wear_pct", "average_hrv", "average_heart_rate", "steps", "spo2",
               "age_yrs", "meld", "decomp", "anyhe", "anyOHE", "ascites", "phes_score", "psqiglobal", "cldq_total"]
    assert all(pd.api.types.is_numeric_dtype(df[c]) for c in numeric)
    assert df["class_5_min"].str.fullmatch(r"[0-5]{288}").all()
    assert df["met_5min"].str.split(",").map(len).eq(288).all()
    assert set(df["valid80"]) <= {0, 1} and df["non_wear_pct"].between(0, 1).all()
    assert df.groupby("name")["days_since_start"].apply(lambda d: list(d) == list(range(1, len(d) + 1))).all()
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
        assert d["tiles"] and d["profile"]["met"]["p50"] and d["synthetic"]
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
    assert "https://github.com/dicristea/ehr-wearable-he-dashboard/blob/main/docs/synthetic-data.md" in nbs
    for name in ["deid_oura_data_exploration", "he_trajectories_phenotypes"]:
        assert f'href="/notebooks/view/{name}"' in nbs
        assert (f'href="https://dicristea.github.io/ehr-wearable-he-dashboard/lab/index.html'
                f'?path=notebooks/{name}.ipynb"') in nbs
        assert (f'href="https://mybinder.org/v2/gh/dicristea/ehr-wearable-he-dashboard/main'
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
    sources = sorted(p.stem for p in (Path(app_module.__file__).parent / "notebooks").glob("*.py"))
    assert sorted(nb["name"] for nb in app_module.DEMO_NOTEBOOKS) == sources


def test_local_notebooks_tab_is_unchanged(monkeypatch, tmp_path):
    import app as app_module
    monkeypatch.setattr(app_module, "NOTEBOOK_DIRS", [tmp_path])
    monkeypatch.setattr(app_module, "JUPYTER_ROOT", tmp_path.parent)
    (tmp_path / "mine.ipynb").write_text("{}")
    nbs = app.test_client().get("/notebooks").data.decode()
    assert f"{app_module.JUPYTER_BASE_URL}/lab/tree/{tmp_path.name}/mine.ipynb" in nbs and "Open in JupyterLab" in nbs
    assert "mybinder.org" not in nbs and "Run in your browser" not in nbs and "/notebooks/view/" not in nbs


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
    import base64
    import app as app_module
    monkeypatch.setattr(app_module, "DEMO_PASSWORD", "s3cret")
    client = app.test_client()

    def basic(user: str, pw: str) -> dict:
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}

    for url in ["/", "/cohort", "/api/cohort", "/static/style.css"]:
        res = client.get(url)
        assert res.status_code == 401 and "Basic" in res.headers["WWW-Authenticate"], url
    assert client.get("/", headers=basic("demo", "wrong")).status_code == 401
    assert client.get("/", headers=basic("someone", "s3cret")).status_code == 401
    assert client.get("/", headers={"Authorization": "Bearer s3cret"}).status_code == 401
    assert client.get("/", headers=basic("demo", "s3cret")).status_code == 200
    assert client.get("/healthz").status_code == 200
    monkeypatch.setattr(app_module, "DEMO_USER", "henry")
    assert client.get("/", headers=basic("henry", "s3cret")).status_code == 200
