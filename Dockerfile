# Public synthetic demo only (docs/hosting.md). Never build this image with study data in reach.

# ── Stage 1: run the notebooks on synthetic data and export read-only HTML ─────────────────────────────
# Study data can't exist here: the build context holds code only (.dockerignore) and the generator makes
# the cohort from its default seed. The notebook stack stays in this stage; only the HTML moves on.
FROM python:3.13-slim AS notebooks
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HE_DATA_SOURCE=synthetic HE_PUBLIC_DEMO=1
WORKDIR /build
COPY requirements-notebooks.txt ./
RUN pip install --no-cache-dir -r requirements-notebooks.txt
COPY data.py ./
COPY scripts/ scripts/
COPY notebooks/ notebooks/
# A notebook that fails gets no page ("not available" in the dashboard); the build carries on.
# render_notebooks.py still fails the build if it finds anything but synthetic data in reach.
RUN python scripts/generate_synthetic_data.py && python scripts/render_notebooks.py

# ── Stage 2: the web app ───────────────────────────────────────────────────────────────────────────────
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app

# Only what the web app imports; the notebook stack (JupyterLab, scikit-learn, matplotlib) isn't needed.
RUN pip install --no-cache-dir \
    flask==3.1.3 pandas==3.0.6 numpy==2.5.4 scipy==1.18.1 nbformat==5.11.1 gunicorn==27.0.0

# App code only, plus the pre-rendered notebook pages. No notebook sources, scripts, tests or data.
COPY app.py data.py ./
COPY templates/ templates/
COPY static/ static/
COPY --from=notebooks /build/rendered_notebooks/ rendered_notebooks/

RUN useradd --create-home demo
USER demo

# HE_PUBLIC_DEMO is set here, not with ENV, so a host setting can't switch it off.
# One worker keeps the in-memory launch log and the session key consistent across requests.
# --timeout 120: booting (pandas, scipy, building the cohort) takes ~1.5 s of CPU, which on Render's free 0.1 CPU
# can pass gunicorn's default 30 s worker timeout and kill the worker in a loop. Render sets PORT (10000).
CMD exec env HE_PUBLIC_DEMO=1 gunicorn --bind "0.0.0.0:${PORT}" --workers 1 --threads 4 --timeout 120 --access-logfile - app:app
