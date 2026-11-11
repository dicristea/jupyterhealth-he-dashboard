# JupyterLite: the notebooks in the browser

[JupyterLite](https://jupyterlite.readthedocs.io) is JupyterLab running entirely in the visitor's browser. Python is [Pyodide](https://pyodide.org) (CPython compiled to WebAssembly), so **code runs on the visitor's computer, never on a server of ours**. The site is static files on GitHub Pages:

- Notebooks: `https://dicristea.github.io/ehr-wearable-he-dashboard/lab/index.html?path=notebooks/<name>.ipynb`, with `<name>` = `deid_oura_data_exploration` or `he_trajectories_phenotypes`. The public demo's Notebooks tab links here (`JUPYTERLITE_URL` in `app.py` is the site root).
- Cohort export: `…/lab/index.html?fromURL=<notebook URL>&fromURLToFolder=notebooks` fetches a notebook from the demo and opens it in `notebooks/` (the [`jupyterlab-open-url-parameter`](https://github.com/jupyterlab-contrib/jupyterlab-open-url-parameter) extension, which JupyterLite doesn't ship on its own).

Only synthetic data is in it. The file layout matches the repo, so the notebooks run unchanged:

```
/data.py
/notebooks/deid_oura_data_exploration.ipynb   (from notebooks/*.py, no outputs)
/notebooks/he_trajectories_phenotypes.ipynb
/synthetic_data/synthetic_he.csv.gz            (generated at build time, generator defaults)
/synthetic_data/synthetic_he.settings.json     (its recipe)
```

`HE_DATA_SOURCE` is unset in the browser, so the notebooks run in `auto` mode, find no `.rds`, and read `synthetic_data/synthetic_he.csv.gz` through `data.synthetic_table()`. Files they write (`notebook_outputs/`) and any edits stay in that browser's storage (IndexedDB), visible only to that visitor.

## Files

| | |
|---|---|
| `lite/jupyter_lite_config.json` | build settings: JupyterLab only, no source maps, only the extensions below |
| `lite/jupyter-lite.json` | runtime settings: preload numpy, pandas, scipy, scikit-learn, matplotlib when the kernel starts, so the first cell just works |
| `lite/overrides.json` | start the Pyodide kernel without asking (exported notebooks name the desktop `python3` kernel) |
| `lite/requirements-build.txt` | pinned build tools: `jupyterlite-core` 0.8.6, `jupyterlite-pyodide-kernel` 0.8.6 (Pyodide 314.0.6, Python 3.14), `jupyterlab-open-url-parameter` 0.4.0, `jupytext` 1.19.6 |
| `scripts/build_jupyterlite.sh` | generates the synthetic data, converts the notebooks, copies `data.py`, builds into `_site/` |
| `.github/workflows/jupyterlite.yml` | builds and deploys on every push to `main`, only in the public repo |

In the browser: numpy 2.4, pandas 3.0, scipy 1.18, scikit-learn 1.8, matplotlib 3.10 (Pyodide's own builds). Every import in `data.py` and both notebooks is either in Python's standard library or one of these; `pyreadr` isn't, and only the study-data path asks for it.

## Rebuild locally

```bash
python3 -m venv .venv-lite                       # separate from the app's .venv
.venv-lite/bin/pip install -r lite/requirements-build.txt
PYTHON=.venv-lite/bin/python scripts/build_jupyterlite.sh
python3 -m http.server 8000 --directory _site    # → http://127.0.0.1:8000/lab/index.html?path=notebooks/he_trajectories_phenotypes.ipynb
```

The build uses whatever is in `notebooks/*.py` at that moment and never reads study data. `_site/`, `.jupyterlite_build/` and `.jupyterlite_cache/` are git-ignored. Use `127.0.0.1` or `localhost`: the service worker that gives the kernel access to the files needs a secure origin. (`http.server` sometimes drops requests when the page loads dozens of files at once; reload if the lab doesn't appear.)

## Enable GitHub Pages (once, in the public repo)

1. On github.com open `dicristea/ehr-wearable-he-dashboard` → **Settings** → **Pages**.
2. Under **Build and deployment → Source**, choose **GitHub Actions**.
3. **Actions** tab → **JupyterLite** → **Run workflow** (or push to `main`). The build takes a few minutes. The URL appears on the `deploy` job and under Settings → Pages.
4. Open `https://dicristea.github.io/ehr-wearable-he-dashboard/lab/index.html?path=notebooks/he_trajectories_phenotypes.ipynb` and **Run → Run All Cells**.

The workflow's jobs have `if: github.repository == 'dicristea/ehr-wearable-he-dashboard'`, so in the private repo they're skipped.

## Cost of the first visit

- The site itself: **about 20 MB** on disk (`_site/`), of which about 11 MB is downloaded to open a notebook (18 MB is JupyterLab's code, mostly loaded on demand; the synthetic CSV is 1.4 MB).
- Pyodide and the scientific packages come from the jsDelivr CDN: **about 90 MB** the first time (the kernel also fetches the small `comm` package from PyPI). The browser caches them afterwards.
- Timing measured with headless Chrome against a local build, fast network: Run All takes about 20 s for each notebook, including the package download. On ordinary Wi-Fi expect 30–60 s before the first cell finishes; later visits are much faster.

## Limitations

- **Memory**: everything lives in the browser tab. The trajectories notebook took the Pyodide heap to about 270 MB, which is fine on a laptop and may fail on phones or tablets. Use Binder or a local clone for anything bigger than the synthetic cohort.
- **Speed**: WebAssembly is slower than desktop Python, and there are no threads (scikit-learn runs single-threaded).
- **No R, no study data**: `Rscript` and `.rds` reading don't exist here, so `HE_DATA_SOURCE=study` can't work. This is by design.
- **Needs a normal browser tab**: private windows (Firefox especially) may block the service worker; the notebook then can't read `data.py` or the CSV. JupyterLite logs "Pyodide contents will NOT be synced" in the console when that happens.
- **Edits are per browser**: a visitor who changed a notebook keeps seeing their copy after we redeploy. To get the published version back, clear the site's data in the browser settings for `dicristea.github.io`.
- **Internet access** to `cdn.jsdelivr.net` and `pypi.org` is needed while the kernel starts.

## How it was verified

Built with the pinned tools in a clean virtualenv (Python 3.13, macOS), served locally, and driven with Playwright in headless Google Chrome: each notebook opened by its `?path=` URL, **Run All Cells**, then the outputs checked. Both notebooks ran top to bottom with no errors (13 of 13 and 28 of 28 code cells, plots rendered), reading `/drive/synthetic_data/synthetic_he.csv.gz`. A cohort export opened through `?fromURL=` from the app in demo mode (with `NOTEBOOK_CORS_ORIGINS` set to the local origin) landed in `notebooks/` and ran. What still has to be checked on the live site: GitHub Pages serving (HTTPS, the `/ehr-wearable-he-dashboard/` path prefix), the CORS fetch from `he-dashboard-synthetic-demo.onrender.com`, and Firefox and Safari.
