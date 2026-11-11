"""Both analysis notebooks run end to end on synthetic data (no R, no study file). About 15 s together."""
from pathlib import Path

import jupytext
import pytest
from nbclient import NotebookClient

NOTEBOOKS = Path(__file__).resolve().parent.parent / "notebooks"


@pytest.mark.parametrize("name", ["deid_oura_data_exploration", "he_trajectories_phenotypes"])
def test_notebook_runs_on_synthetic_data(name, monkeypatch):
    monkeypatch.setenv("HE_DATA_SOURCE", "synthetic")
    nb = jupytext.read(NOTEBOOKS / f"{name}.py")
    # Executed in memory only: an .ipynb with outputs is never written.
    NotebookClient(nb, timeout=600, kernel_name="python3", resources={"metadata": {"path": str(NOTEBOOKS)}}).execute()
    text = "".join(o.get("text", "") for c in nb.cells if c.cell_type == "code" for o in c.outputs)
    assert "Data source: synthetic" in text and "SYNTHETIC DATA" in text
