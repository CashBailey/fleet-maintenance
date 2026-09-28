#!/usr/bin/env bash
set -euo pipefail

repo_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$repo_root"

cyclonedx_py="${CYCLONEDX_PY:-.venv/bin/cyclonedx-py}"
python_bin="${PYTHON_BIN:-.venv/bin/python}"
if [[ ! -x "$cyclonedx_py" || ! -x "$python_bin" ]]; then
  echo "Install requirements-dev.lock into .venv before generating SBOMs." >&2
  exit 1
fi
command -v npm >/dev/null || { echo "npm is required to generate the JavaScript SBOM." >&2; exit 1; }

mkdir -p artifacts
python_tmp="artifacts/.sbom-python.cdx.json.tmp"
npm_tmp="artifacts/.sbom-npm.cdx.json.tmp"
trap 'rm -f -- "$python_tmp" "$npm_tmp"' EXIT

"$cyclonedx_py" requirements requirements.lock \
  --output-reproducible \
  --output-format JSON \
  --output-file "$python_tmp"
npm sbom --package-lock-only --sbom-format cyclonedx --sbom-type application >"$npm_tmp"

"$python_bin" - "$python_tmp" "$npm_tmp" <<'PY'
import json
import sys

for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as stream:
        bom = json.load(stream)
    if bom.get("bomFormat") != "CycloneDX" or not bom.get("components"):
        raise SystemExit(f"invalid or empty CycloneDX document: {path}")
PY

mv "$python_tmp" artifacts/sbom-python.cdx.json
mv "$npm_tmp" artifacts/sbom-npm.cdx.json
trap - EXIT
echo "Wrote artifacts/sbom-python.cdx.json and artifacts/sbom-npm.cdx.json"
