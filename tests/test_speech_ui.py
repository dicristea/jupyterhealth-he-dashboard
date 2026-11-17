"""Speech work-in-progress UI: Notebooks card, read-only code page, patient placeholder, shared dialog."""
import os

import pytest

os.environ["HE_DATA_FILE"] = "/nonexistent/no-data-here.rds"
for var in ("HE_PUBLIC_DEMO", "HE_HOSTED_STUDY"):
    os.environ.pop(var, None)

import app as app_module  # noqa: E402
import data  # noqa: E402

MODAL_TITLE = b"Speech recordings aren't connected yet"


@pytest.fixture(params=["local", "demo"])
def client(request, monkeypatch, tmp_path):
    if request.param == "demo":
        monkeypatch.setattr(data, "PUBLIC_DEMO", True)
        monkeypatch.setattr(data, "SYNTHETIC_FILE", tmp_path / "none.csv.gz")
        monkeypatch.setattr(data, "_read_rds", lambda path: pytest.fail("demo mode read study data"))
    data.load.cache_clear()
    yield app_module.app.test_client()
    data.load.cache_clear()


def test_notebooks_tab_has_speech_card_and_dialog(client):
    res = client.get("/notebooks")
    assert res.status_code == 200
    assert b"Speech features" in res.data and b"In progress \xc2\xb7 awaiting recordings" in res.data
    assert b"data-speech-open" in res.data and b"Upload recordings" in res.data
    assert MODAL_TITLE in res.data and b'aria-modal="true"' in res.data
    assert b"/notebooks/code/speech_features_starter" in res.data


def test_code_page_shows_source_without_running_it(client):
    res = client.get("/notebooks/code/speech_features_starter")
    assert res.status_code == 200
    assert b"extract_features" in res.data and b"PARAMS" in res.data
    assert b"<strong>Pitch (F0):</strong>" in res.data and b"**" not in res.data  # notes rendered, not raw Markdown
    assert b'class="tok-keyword">def</span>' in res.data and b'id="codeToggle"' in res.data
    assert b"jupytext:" not in res.data  # header dropped
    assert client.get("/notebooks/code/not_a_notebook").status_code == 404
    assert client.get("/notebooks/code/..%2Fapp").status_code == 404


def test_patient_page_has_speech_placeholder(client):
    pid = data.patient_list()[0]["id"]
    res = client.get(f"/patient/{pid}")
    assert res.status_code == 200
    assert b'id="speechPlaceholder"' in res.data and b"No speech recordings yet" in res.data
    assert b"How this will work" in res.data and MODAL_TITLE in res.data
    assert res.data.count(b'id="speechModal"') == 1


def test_code_cells_split_markdown_and_code():
    cells = app_module._code_cells("# ---\n# jupytext: x\n# ---\n\n# %% [markdown]\n# # Title\n# text\n\n# %%\nx = 1\n")
    assert cells == [{"kind": "markdown", "text": "# Title\ntext"}, {"kind": "code", "text": "x = 1"}]


def test_both_images_ship_the_speech_source():
    root = app_module.HERE
    for name in ("Dockerfile", "Dockerfile.deid"):
        assert "COPY notebooks/speech_features_starter.py notebooks/" in (root / name).read_text()
    assert set(app_module.CODE_PAGES) == {"speech_features_starter"}


def test_code_page_escapes_html():
    out = str(app_module._py_html('x = "<script>"  # <b>'))
    assert "<script>" not in out and "&lt;script&gt;" in out and "&lt;b&gt;" in out
    md = str(app_module._md_html("# T\n- **a** <i>\n> `q`"))
    assert "<h2>T</h2>" in md and "<strong>a</strong> &lt;i&gt;" in md and "<blockquote><code>q</code></blockquote>" in md
