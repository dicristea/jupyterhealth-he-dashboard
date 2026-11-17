"""Hosted de-identified study mode (HE_HOSTED_STUDY=1). Synthetic data only, no network."""
import gzip
import io
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

os.environ["HE_DATA_FILE"] = "/nonexistent/no-data-here.rds"
for var in ("HE_PUBLIC_DEMO", "HE_HOSTED_STUDY", "HE_DATA_URL", "HE_DATA_TOKEN"):
    os.environ.pop(var, None)

import app as app_module  # noqa: E402
import data  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
TOKEN = "ghp_SECRET_token_value"
ASSET_URL = "https://api.github.com/repos/owner/repo/releases/assets/123"


def _boot(**env) -> subprocess.CompletedProcess:
    """Import the app in a fresh interpreter with these env vars, as gunicorn would."""
    clean = {k: v for k, v in os.environ.items()
             if k not in {"HE_PUBLIC_DEMO", "HE_HOSTED_STUDY", "HE_DATA_URL", "HE_DATA_TOKEN", "DEMO_PASSWORD"}}
    return subprocess.run([sys.executable, "-c", "import app"], cwd=REPO, env={**clean, **env},
                          capture_output=True, text=True, timeout=120)


def test_refuses_to_boot_without_password():
    res = _boot(HE_HOSTED_STUDY="1", HE_DATA_URL=ASSET_URL)
    assert res.returncode != 0 and "refuses to start without DEMO_PASSWORD" in res.stderr


def test_demo_and_hosted_study_cannot_both_be_on():
    res = _boot(HE_HOSTED_STUDY="1", HE_PUBLIC_DEMO="1", DEMO_PASSWORD="pw", HE_DATA_URL=ASSET_URL)
    assert res.returncode != 0 and "can't both be on" in res.stderr


def test_refuses_to_boot_without_data_url():
    res = _boot(HE_HOSTED_STUDY="1", DEMO_PASSWORD="pw")
    assert res.returncode != 0 and "needs HE_DATA_URL" in res.stderr


def _gz_cache_bytes() -> bytes:
    """A small fake study cache: synthetic rows in the .v2.csv layout, gzipped."""
    df = data.synthetic_cohort(n=4, seed=3, min_days=20, max_days=20)
    return gzip.compress(df.to_csv(index=False).encode())


@pytest.fixture
def hosted(monkeypatch):
    payload = _gz_cache_bytes()
    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req)
        return io.BytesIO(payload)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(data, "HOSTED_STUDY", True)
    monkeypatch.setattr(data, "_read_rds", lambda path: pytest.fail("hosted mode ran R / read a local .rds"))
    monkeypatch.setenv("HE_DATA_URL", ASSET_URL)
    monkeypatch.setenv("HE_DATA_TOKEN", TOKEN)
    monkeypatch.setattr(app_module, "DEMO_USER", "study")
    monkeypatch.setattr(app_module, "DEMO_PASSWORD", "pw")
    data.load.cache_clear()
    yield seen
    data.load.cache_clear()


def _client(user="study", pw="pw"):
    """A test client that logged in through the login page (session cookie)."""
    c = app_module.app.test_client()
    c.post("/login", data={"username": user, "password": pw, "next": "/"})
    return c


def test_loader_downloads_once_and_serves_study_source(hosted):
    df, source = data.load()
    assert source == "study" and df["name"].nunique() == 4
    assert {"night_rest_h", "sri"} <= set(df.columns)
    data.load()
    assert len(hosted) == 1  # cached: one download per process
    req = hosted[0]
    assert req.full_url == ASSET_URL and req.get_header("Accept") == "application/octet-stream"
    # The token must not follow GitHub's redirect to the storage host.
    assert req.unredirected_hdrs.get("Authorization") == f"Bearer {TOKEN}"
    assert "Authorization" not in req.headers


def test_auth_enforced_and_healthz_open(hosted, monkeypatch):
    monkeypatch.setattr(app_module, "LOGIN_FAIL_DELAY_S", 0)
    anon = app_module.app.test_client()
    assert anon.get("/healthz").status_code == 200
    assert anon.get("/").status_code == 302
    assert anon.get("/api/study-data.csv.gz").status_code == 401
    assert anon.get("/lite/").status_code == 302
    assert b"De-identified study data" in anon.get("/login").data
    assert _client(pw="wrong").get("/").status_code == 302
    res = _client().get("/")
    assert res.status_code == 200 and res.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert b"password protected" in res.data and b"Log out" in res.data
    assert anon.get("/robots.txt").data == b"User-agent: *\nDisallow: /\n"


def test_fails_closed_without_password_at_request_time(hosted, monkeypatch):
    monkeypatch.setattr(app_module, "DEMO_PASSWORD", "")
    assert app_module.app.test_client().get("/").status_code == 503


def test_snapshot_on_study_data_shows_no_data_note_not_synthetic_rows(hosted):
    pid = data.patient_list()[0]["id"]
    assert data.load()[1] == "study"
    page = _client().get(f"/patient/{pid}").data.decode()
    for title in data.SNAPSHOT_SECTIONS.values():
        assert f"<summary>{title}" in page
    assert page.count(data.SNAPSHOT_NO_DATA) == 2  # Medications and Clinical history
    assert "cp-tag" not in page and "cp-med" not in page and "Lactulose" not in page


def test_pages_work_and_local_only_features_are_off(hosted):
    c = _client()
    pid = data.patient_list()[0]["id"]
    for url in ["/", f"/patient/{pid}", "/cohort", "/ehr", "/notebooks"]:
        assert c.get(url).status_code == 200, url
    assert c.post("/api/cohort/notebook", json={}).status_code == 404
    assert c.get("/ehr/launch?iss=https://example.org").status_code == 404
    nb = c.get("/notebooks").data
    assert b"mybinder.org" not in nb and b"github.io" not in nb and b"/notebooks/view/" not in nb
    assert c.get("/notebooks/view/deid_oura_data_exploration/page").status_code == 404
    assert b"Connect Medplum" not in c.get("/ehr").data
    cohort = c.get("/cohort").data
    assert b'"/lite/lab/index.html"' in cohort and b"mybinder.org" not in cohort


def test_notebooks_tab_links_this_sites_jupyterlite(hosted, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "LITE_DIR", tmp_path)
    c = _client()
    assert b"wasn't built" in c.get("/notebooks").data  # no Lite in this deployment: say so, no dead links
    (tmp_path / "index.html").write_text("lite")
    nb = c.get("/notebooks").data
    for name in ("deid_oura_data_exploration", "he_trajectories_phenotypes"):
        assert f"/lite/lab/index.html?path=notebooks/{name}.ipynb".encode() in nb
        assert f"/lite/files/notebooks/{name}.ipynb".encode() in nb


def test_lite_is_served_behind_login(hosted, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "LITE_DIR", tmp_path)
    (tmp_path / "index.html").write_text("root")
    (tmp_path / "lab").mkdir()
    (tmp_path / "lab" / "index.html").write_text("lab")
    (tmp_path / "kernel.wasm").write_bytes(b"\0asm")
    (tmp_path / "jupyter-lite.json").write_text("{}")
    anon = app_module.app.test_client()
    assert anon.get("/lite/lab/index.html").status_code == 302
    c = _client()
    assert c.get("/lite/").data == b"root"
    assert c.get("/lite/lab").headers["Location"].endswith("/lite/lab/")
    assert c.get("/lite/lab/").data == b"lab"
    assert c.get("/lite/kernel.wasm").mimetype == "application/wasm"
    assert c.get("/lite/jupyter-lite.json").mimetype == "application/json"
    assert c.get("/lite/../app.py").status_code == 404
    assert c.get("/lite/nope.js").status_code == 404
    assert "no-cache" in c.get("/lite/").headers["Cache-Control"]


def test_study_data_endpoint_serves_the_downloaded_file_to_logged_in_visitors(hosted):
    data.load()
    c = _client()
    res = c.get("/api/study-data.csv.gz")
    assert res.status_code == 200 and res.mimetype == "application/gzip"
    assert res.headers["Cache-Control"] == "no-store"
    assert gzip.decompress(res.data).startswith(b"name,")
    assert app_module.app.test_client().get("/api/study-data.csv.gz").status_code == 401


def test_study_data_endpoint_is_off_outside_hosted_mode():
    assert app_module.app.test_client().get("/api/study-data.csv.gz").status_code == 404
    assert app_module.app.test_client().get("/lite/").status_code == 404


def test_cohort_export_in_hosted_mode_points_at_the_site(hosted):
    c = _client()
    nb = c.get("/api/cohort/notebook.ipynb?group=decomp").data
    assert b"password-protected study site" in nb


def test_lite_study_table_fetches_from_the_site(monkeypatch, tmp_path):
    """data.py inside the hosted Lite: the marker plus Pyodide → fetch the cache from the site, same post-processing."""
    import types
    marker = tmp_path / "hosted_study.json"
    marker.write_text('{"data_url": "/api/study-data.csv.gz"}')
    payload = _gz_cache_bytes()
    asked = []

    def fake_get(url):
        asked.append(url)
        return payload

    monkeypatch.setattr(data, "LITE_MARKER", marker)
    monkeypatch.setattr(data, "_browser_get", fake_get)
    monkeypatch.setattr(data, "sys", types.SimpleNamespace(platform="emscripten"))
    data.lite_study_table.cache_clear()
    data.load.cache_clear()
    try:
        df, source = data.load()
        assert source == "study" and df["name"].nunique() == 4 and "night_rest_h" in df
        raw, origin = data.lite_study_table()
        assert "study site" in origin and asked == ["/api/study-data.csv.gz"]
    finally:
        data.lite_study_table.cache_clear()
        data.load.cache_clear()


def test_hosted_lite_needs_pyodide_and_the_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(data, "LITE_MARKER", tmp_path / "hosted_study.json")
    assert not data.in_hosted_lite()  # desktop Python, no marker: local behaviour unchanged
    (tmp_path / "hosted_study.json").write_text("{}")
    assert not data.in_hosted_lite()  # marker alone isn't enough off Pyodide


def test_download_failure_is_loud_and_hides_token(monkeypatch, tmp_path):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    import urllib.error
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as exc:
        data.fetch_hosted_data(ASSET_URL + "?sig=SIGNED", TOKEN, tmp_path)
    msg = str(exc.value)
    assert "404" in msg and TOKEN not in msg and "SIGNED" not in msg


def test_speech_code_page_and_card_in_hosted_mode(hosted):
    anon = app_module.app.test_client().get("/notebooks/code/speech_features_starter")
    assert anon.status_code == 302 and "/login" in anon.headers["Location"]
    c = _client()
    res = c.get("/notebooks/code/speech_features_starter")
    assert res.status_code == 200 and b"extract_features" in res.data
    nbs = c.get("/notebooks")
    assert b"Speech features" in nbs.data and b"Upload recordings" in nbs.data
