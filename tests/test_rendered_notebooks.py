"""Read-only notebook pages: the render script's safety checks and the /notebooks/view/<name> routes."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

os.environ["HE_DATA_FILE"] = "/nonexistent/no-data-here.rds"  # force the synthetic cohort
os.environ.pop("HE_PUBLIC_DEMO", None)

import app as app_module  # noqa: E402
import data  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "render_notebooks.py"
_spec = importlib.util.spec_from_file_location("render_notebooks", SCRIPT)
render_notebooks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(render_notebooks)

NAME = "he_trajectories_phenotypes"
PAGE = "<html><head></head><body><p>rendered on synthetic data</p></body></html>"


@pytest.fixture
def rendered(monkeypatch, tmp_path):
    """Demo mode with a fake pre-rendered page for NAME only."""
    monkeypatch.setattr(app_module, "RENDERED_DIR", tmp_path)
    monkeypatch.setattr(data, "PUBLIC_DEMO", True)
    data.load.cache_clear()
    (tmp_path / f"{NAME}.html").write_text(PAGE)
    yield tmp_path
    data.load.cache_clear()


def test_render_script_and_app_agree_on_names():
    assert render_notebooks.NOTEBOOKS == list(app_module.RENDERED_NOTEBOOKS)
    assert sorted(render_notebooks.NOTEBOOKS) == sorted(p.stem for p in (ROOT / "notebooks").glob("*.py"))


def test_demo_serves_the_rendered_page_in_a_sandboxed_frame(rendered):
    client = app_module.app.test_client()
    view = client.get(f"/notebooks/view/{NAME}")
    assert view.status_code == 200
    html = view.data.decode()
    assert f'src="/notebooks/view/{NAME}/page"' in html and 'sandbox="allow-top-navigation-by-user-activation"' in html
    assert "Synthetic data." in html and "Public demo" in html  # inside the dashboard chrome
    page = client.get(f"/notebooks/view/{NAME}/page")
    assert page.status_code == 200 and page.data.decode() == PAGE
    csp = page.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "sandbox" in csp and "script-src" not in csp
    assert page.headers["X-Frame-Options"] == "SAMEORIGIN"


def test_demo_page_not_rendered_says_not_available(rendered):
    client = app_module.app.test_client()
    view = client.get("/notebooks/view/deid_oura_data_exploration")
    assert view.status_code == 404 and "Not available" in view.data.decode()
    assert client.get("/notebooks/view/deid_oura_data_exploration/page").status_code == 404


def test_only_allowlisted_names_are_served(rendered):
    (rendered / "secret.html").write_text("not a notebook")
    client = app_module.app.test_client()
    for name in ["secret", f"{NAME}.html", "..%2Fapp.py", "../app.py", "%2e%2e", f"{NAME}%00"]:
        assert client.get(f"/notebooks/view/{name}").status_code == 404, name
        assert client.get(f"/notebooks/view/{name}/page").status_code == 404, name


def test_local_mode_never_serves_rendered_pages(rendered, monkeypatch):
    monkeypatch.setattr(data, "PUBLIC_DEMO", False)
    client = app_module.app.test_client()
    view = client.get(f"/notebooks/view/{NAME}")
    assert view.status_code == 404 and "public demo only" in view.data.decode()
    assert client.get(f"/notebooks/view/{NAME}/page").status_code == 404


@pytest.mark.parametrize("env", [{}, {"HE_DATA_SOURCE": "auto"}, {"HE_DATA_SOURCE": "study"},
                                 {"HE_DATA_SOURCE": "synthetic", "HE_DATA_FILE": "/x.rds"},
                                 {"HE_DATA_SOURCE": "synthetic", "JH_DATA_DIR": "/data"},
                                 {"HE_DATA_SOURCE": "synthetic", "HE_SYNTHETIC_FILE": "/cache.csv"}])
def test_render_script_refuses_anything_but_synthetic(env, monkeypatch, tmp_path):
    monkeypatch.setattr(render_notebooks, "STUDY_DIRS", [tmp_path])
    with pytest.raises(SystemExit, match="refusing"):
        render_notebooks.check_synthetic_only(env)


def test_render_script_refuses_when_a_study_file_is_in_reach(monkeypatch, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "study.rds").write_bytes(b"")  # never opened, only found
    monkeypatch.setattr(render_notebooks, "STUDY_DIRS", [tmp_path])
    with pytest.raises(SystemExit, match="study .rds"):
        render_notebooks.check_synthetic_only({"HE_DATA_SOURCE": "synthetic"})
    (tmp_path / "sub" / "study.rds").unlink()
    render_notebooks.check_synthetic_only({"HE_DATA_SOURCE": "synthetic"})


def test_render_script_exits_before_running_anything(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HE_", "JH_"))}
    res = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True)
    assert res.returncode != 0 and "HE_DATA_SOURCE=synthetic" in res.stderr


def test_clean_html_drops_scripts_and_adds_the_banner():
    out = render_notebooks.clean_html('<html><head><script src="https://cdn.example/x.js"></script></head>'
                                      '<body class="jp-Notebook"><SCRIPT>alert(1)</SCRIPT><p>hi</p></body></html>')
    assert "<script" not in out.lower() and "cdn.example" not in out
    assert out.index("Read-only.") < out.index("<p>hi</p>") and 'href="/notebooks" target="_top"' in out
    assert "not clinical findings" in out and '<base target="_top">' in out


def test_render_produces_a_static_page(monkeypatch, tmp_path):
    pytest.importorskip("nbclient")
    nb_dir, out_dir = tmp_path / "notebooks", tmp_path / "out"
    nb_dir.mkdir(), out_dir.mkdir()
    (nb_dir / "tiny.py").write_text("# %% [markdown]\n# # Tiny\n\n# %%\nprint('hello from', 'synthetic')\n")
    monkeypatch.setattr(render_notebooks, "NOTEBOOK_DIR", nb_dir)
    monkeypatch.setattr(render_notebooks, "OUT_DIR", out_dir)
    html = render_notebooks.render("tiny").read_text()
    assert "hello from synthetic" in html and "print" in html  # outputs and code both shown
    assert "<script" not in html and "Read-only." in html and "In&nbsp;[" not in html
