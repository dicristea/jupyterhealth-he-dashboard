# Public synthetic demo only (docs/hosting.md). Never build this image with study data in reach.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app

# Only what the web app imports; the notebook stack (JupyterLab, scikit-learn, matplotlib) isn't needed.
RUN pip install --no-cache-dir \
    flask==3.1.3 pandas==3.0.6 numpy==2.5.4 scipy==1.18.1 nbformat==5.11.1 gunicorn==27.0.0

# Allowlist, together with .dockerignore: app code only. No notebooks, scripts, tests or data folders.
COPY app.py data.py ./
COPY templates/ templates/
COPY static/ static/

RUN useradd --create-home demo
USER demo

# HE_PUBLIC_DEMO is set here, not with ENV, so a host setting can't switch it off.
# One worker keeps the in-memory launch log and the session key consistent across requests.
# --timeout 120: booting (pandas, scipy, building the cohort) takes ~1.5 s of CPU, which on Render's free 0.1 CPU
# can pass gunicorn's default 30 s worker timeout and kill the worker in a loop. Render sets PORT (10000).
CMD exec env HE_PUBLIC_DEMO=1 gunicorn --bind "0.0.0.0:${PORT}" --workers 1 --threads 4 --timeout 120 --access-logfile - app:app
