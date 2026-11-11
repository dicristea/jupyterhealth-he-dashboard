#!/usr/bin/env bash
# Build the JupyterLite site (notebooks running in the browser on synthetic data) into _site/.
# Needs lite/requirements-build.txt installed in $PYTHON (default: python3). See docs/jupyterlite.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
OUT="${OUT:-$ROOT/_site}"
BUILD="$ROOT/.jupyterlite_build"
STAGE="$BUILD/contents"   # what visitors see; mirrors the repo layout
EXTS="$BUILD/labextensions"

# doit's task database would skip steps whose outputs we just deleted, so always build from scratch.
rm -rf "$BUILD" "$OUT" "$ROOT/lite/.jupyterlite.doit.db"
mkdir -p "$STAGE/notebooks" "$EXTS/@jupyterlite"

# Only these browser extensions; the jupytext one installed alongside needs a Jupyter server.
# open-url-parameter adds ?fromURL= (the dashboard's cohort export opens its notebook that way).
SHARE="$("$PYTHON" -c 'import sys; print(sys.prefix)')/share/jupyter/labextensions"
cp -R "$SHARE/@jupyterlite/pyodide-kernel-extension" "$EXTS/@jupyterlite/"
cp -R "$SHARE/jupyterlab_pygments" "$SHARE/jupyterlab-open-url-parameter" "$EXTS/"

# Synthetic cohort only, generated fresh (never committed). Study data is never read here.
"$PYTHON" "$ROOT/scripts/generate_synthetic_data.py" --output "$STAGE/synthetic_data/synthetic_he.csv.gz"

cp "$ROOT/data.py" "$STAGE/data.py"

# Whatever notebooks/*.py hold at build time; the .ipynb copies have no outputs.
# Kernel "python" is the Pyodide kernel's name, so JupyterLite doesn't ask which kernel to use.
KERNELSPEC='{"kernelspec": {"name": "python", "display_name": "Python (Pyodide)", "language": "python"}}'
for src in "$ROOT"/notebooks/*.py; do
  name="$(basename "$src" .py)"
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
echo "JupyterLite site: $OUT ($(du -sh "$OUT" | cut -f1))"
