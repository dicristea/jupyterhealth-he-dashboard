"""The analysis notebooks (and the data overview) run end to end on synthetic data (no R, no study file). About 15 s together.
The speech notebook runs with no recordings, and its feature functions work on an in-memory tone."""
from pathlib import Path

import jupytext
import numpy as np
import pytest
from nbclient import NotebookClient

NOTEBOOKS = Path(__file__).resolve().parent.parent / "notebooks"


def _execute(name: str):
    nb = jupytext.read(NOTEBOOKS / f"{name}.py")
    # Executed in memory only: an .ipynb with outputs is never written.
    NotebookClient(nb, timeout=600, kernel_name="python3", resources={"metadata": {"path": str(NOTEBOOKS)}}).execute()
    return "".join(o.get("text", "") for c in nb.cells if c.cell_type == "code" for o in c.outputs)


# deid_data_exploration is private-repo only (export-ignore); the public copy of these tests skips it.
EXPLORATIONS = [n for n in ["deid_data_exploration", "synthetic_data_exploration"] if (NOTEBOOKS / f"{n}.py").exists()]


@pytest.mark.parametrize("name", [*EXPLORATIONS, "he_trajectories_phenotypes"])
def test_notebook_runs_on_synthetic_data(name, monkeypatch):
    monkeypatch.setenv("HE_DATA_SOURCE", "synthetic")
    text = _execute(name)
    assert "Data source: synthetic" in text and "SYNTHETIC DATA" in text
    assert "Main nights:" in text  # the Oura sleep-record measures ran


def test_data_overview_runs_on_synthetic_data(monkeypatch):
    monkeypatch.setenv("HE_DATA_SOURCE", "synthetic")
    text = _execute("data_overview")
    assert "Data source: synthetic" in text and "SYNTHETIC DATA" in text and "Main nights:" in text
    src = "\n".join(c.source for c in jupytext.read(NOTEBOOKS / "data_overview.py").cells if c.cell_type == "code")
    for raw_rows in (".head(", ".sample(", ".tail("):  # run on study data by researchers: aggregates only
        assert raw_rows not in src, raw_rows


def test_the_two_explorations_differ_only_in_data_and_wording():
    if len(EXPLORATIONS) < 2:
        pytest.skip("public repo: only the synthetic exploration")
    code = [[c.source for c in jupytext.read(NOTEBOOKS / f"{n}.py").cells if c.cell_type == "code"] for n in EXPLORATIONS]
    assert [c.replace('SOURCE = os.environ.get("HE_DATA_SOURCE", "auto")  # study data when it can be found', 'SOURCE = "synthetic"')
            for c in code[0]] == code[1]
    synthetic_text = (NOTEBOOKS / "synthetic_data_exploration.py").read_text().lower()
    assert "de-identified" not in synthetic_text and "deid" not in synthetic_text


@pytest.mark.parametrize("name", [*EXPLORATIONS, "he_trajectories_phenotypes", "data_overview"])
def test_notebooks_open_the_study_file_only_through_data_py(name):
    # data.py's R step is the one place calendar dates are reduced to clock times; a notebook with its own
    # conversion could write a dated cache.
    src = "\n".join(c.source for c in jupytext.read(NOTEBOOKS / f"{name}.py").cells if c.cell_type == "code")
    assert "_read_rds(" in src or "notebook_table(" in src
    for own_loader in ("readRDS", "Rscript", "R_TO_CSV", "pyreadr", "write.csv"):
        assert own_loader not in src, own_loader


def _have_speech_stack() -> bool:
    try:
        import librosa, parselmouth, soundfile  # noqa: F401
    except ImportError:
        return False
    return True


speech = pytest.mark.skipif(not _have_speech_stack(), reason="speech deps missing: pip install -r requirements-speech.txt")


@speech
def test_speech_notebook_runs_with_no_recordings(tmp_path, monkeypatch):
    monkeypatch.setenv("HE_SPEECH_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("HE_SPEECH_OUTPUT_DIR", str(tmp_path / "out"))
    text = _execute("speech_features_starter")
    assert "0 audio files found" in text and "No recordings yet" in text
    assert not (tmp_path / "out").exists()


@speech
def test_speech_feature_functions_on_a_tone():
    nb = jupytext.read(NOTEBOOKS / "speech_features_starter.py")
    ns: dict = {}
    # Only the cells that import, set PARAMS and define the feature functions; not the loop over files.
    for cell in nb.cells:
        src = cell.source
        if cell.cell_type == "code" and ("import parselmouth" in src or "PARAMS = {" in src or "def extract_features" in src):
            exec(src, ns)
    p = ns["PARAMS"]
    sr = p["sample_rate"]
    tone = (0.5 * np.sin(2 * np.pi * 150 * np.arange(sr) / sr)).astype(np.float32)
    y = np.concatenate([tone, np.zeros(sr // 2, dtype=np.float32), tone])  # 1 s, 0.5 s silence, 1 s

    timing = ns["timing_features"](y, sr, p)
    assert timing["duration_s"] == 2.5 and timing["pause_count"] == 1
    assert abs(ns["praat_features"](y, sr, p)["f0_mean_hz"] - 150) < 2
    assert len(ns["mfcc_features"](y, sr, p)) == 4 * p["n_mfcc"]
