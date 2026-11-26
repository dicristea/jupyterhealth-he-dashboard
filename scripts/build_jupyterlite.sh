#!/usr/bin/env bash
# Build the JupyterLite site (notebooks running in the browser on synthetic data) into _site/.
# Needs lite/requirements-build.txt installed in $PYTHON (default: python3). See docs/jupyterlite.md.
# LITE_VARIANT=hosted-study builds the hosted study site's copy instead (Dockerfile.deid): no data at all, not even
# synthetic, plus a hosted_study.json marker that makes data.py fetch the study data from the site at run time.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
OUT="${OUT:-$ROOT/_site}"
BUILD="$ROOT/.jupyterlite_build"
STAGE="$BUILD/contents"   # what visitors see; mirrors the repo layout
EXTS="$BUILD/labextensions"
VARIANT="${LITE_VARIANT:-public}"
case "$VARIANT" in public|hosted-study) ;; *) echo "LITE_VARIANT must be public or hosted-study" >&2; exit 2;; esac

# doit's task database would skip steps whose outputs we just deleted, so always build from scratch.
rm -rf "$BUILD" "$OUT" "$ROOT/lite/.jupyterlite.doit.db"
mkdir -p "$STAGE/notebooks" "$EXTS/@jupyterlite"

# Only these browser extensions; the jupytext one installed alongside needs a Jupyter server.
# open-url-parameter adds ?fromURL= (the dashboard's cohort export opens its notebook that way).
SHARE="$("$PYTHON" -c 'import sys; print(sys.prefix)')/share/jupyter/labextensions"
cp -R "$SHARE/@jupyterlite/pyodide-kernel-extension" "$EXTS/@jupyterlite/"
cp -R "$SHARE/jupyterlab_pygments" "$SHARE/jupyterlab-open-url-parameter" "$EXTS/"

# Public: synthetic cohort only, generated fresh (never committed). Study data is never read here, in either variant.
if [ "$VARIANT" = public ]; then
  "$PYTHON" "$ROOT/scripts/generate_synthetic_data.py" --output "$STAGE/synthetic_data/synthetic_he.csv.gz"
else
  cat > "$STAGE/hosted_study.json" <<'EOF'
{"data_url": "/api/study-data.csv.gz",
 "note": "Hosted study site JupyterLite: data.py fetches the study data from the site while you are logged in. No data is stored in this build."}
EOF
fi

cp "$ROOT/data.py" "$STAGE/data.py"

# Whatever notebooks/*.py hold at build time; the .ipynb copies have no outputs.
# Kernel "python" is the Pyodide kernel's name, so JupyterLite doesn't ask which kernel to use.
KERNELSPEC='{"kernelspec": {"name": "python", "display_name": "Python (Pyodide)", "language": "python"}}'
for src in "$ROOT"/notebooks/*.py; do
  name="$(basename "$src" .py)"
  # Praat (parselmouth) has no Pyodide build, so the speech notebook can't run in the browser.
  if [ "$name" = "speech_features_starter" ]; then continue; fi
  # Each site gets its own exploration: synthetic on the public site, de-identified on the study site.
  if [ "$VARIANT" = "hosted-study" ] && [ "$name" = "synthetic_data_exploration" ]; then continue; fi
  if [ "$VARIANT" != "hosted-study" ] && [ "$name" = "deid_data_exploration" ]; then continue; fi
  "$PYTHON" -m jupytext --quiet --to ipynb --update-metadata "$KERNELSPEC" \
    --output "$STAGE/notebooks/$name.ipynb" "$src"
done

cd "$ROOT/lite"
"$PYTHON" -m jupyter lite build --contents "$STAGE" --output-dir "$OUT" \
  --FederatedExtensionAddon.extra_labextensions_path="$EXTS"

# The contents index gives *.csv.gz Python's guess "text/csv", and JupyterLite reads any "text" mimetype
# as text, so the kernel would get a mangled gzip (BadGzipFile). Mark it binary.
"$PYTHON" - "$OUT/api/contents" <<'EOF'
import json, sys
from pathlib import Path
for index in Path(sys.argv[1]).rglob("all.json"):
    listing = json.loads(index.read_text())
    entries = listing.get("content") or []
    for entry in entries:
        if entry.get("type") == "file" and entry["name"].endswith(".gz"):
            entry["mimetype"] = "application/gzip"
    index.write_text(json.dumps(listing))
EOF
if [ "$VARIANT" = hosted-study ]; then
  # The public settings name the synthetic data; this copy runs on the study data.
  "$PYTHON" - "$OUT/jupyter-lite.json" <<'EOF'
import json, sys
from pathlib import Path
p = Path(sys.argv[1])
cfg = json.loads(p.read_text())
cfg["jupyter-config-data"]["appName"] = "HE notebooks (de-identified study data)"
p.write_text(json.dumps(cfg, indent=2))
EOF
  # Belt and braces: the hosted copy must not contain any data file.
  if find "$OUT" \( -name '*.csv' -o -name '*.csv.gz' -o -name '*.rds' -o -name '*.parquet' \) | grep -q .; then
    echo "hosted-study Lite build contains a data file; refusing" >&2
    exit 1
  fi
fi
echo "JupyterLite site ($VARIANT): $OUT ($(du -sh "$OUT" | cut -f1))"
