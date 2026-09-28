# Dependency and supply-chain policy

Fleetline uses mature dependencies only where the standard library or platform does not cover the requirement. Runtime dependencies must use licenses compatible with commercial distribution. GPL, AGPL, source-available, and dependencies without a clear license require written legal approval before adoption; copied third-party code or assets are not accepted as a shortcut.

## Pinning and review

- JavaScript dependencies use exact versions in `package.json`; `package-lock.json` locks the complete install graph with registry integrity hashes. CI installs it with `npm ci`.
- Python direct dependencies are reviewed in `requirements.txt` and `requirements-dev.txt`. `requirements.lock` is the complete production graph and `requirements-dev.lock` is the complete development/test graph; both are generated for Python 3.12 with `make lock`, include distribution hashes, and are installed with `pip --require-hashes --only-binary=:all:` so unreviewed build dependencies cannot enter through source distributions. Regenerate and review both locks whenever either input manifest changes.
- Container images and CI actions must be pinned to an exact patch tag or immutable commit/digest. Any intentionally broader image tag is reviewed during each release.
- A dependency change includes the manifest and lock/inventory update in the same change. Review covers maintenance activity, necessity, license, known vulnerabilities, download scripts, and overlap with existing code.
- Major upgrades are isolated from feature changes. Emergency security upgrades may be expedited but still run the complete verification workflow.

## Technical-document extraction dependencies

The searchable truck-document library uses `pypdf` 6.16.2 (BSD-3-Clause) for
embedded text, `pypdfium2` 5.13.0 (Apache-2.0 OR BSD-3-Clause, with the PDFium
binary notices shipped in the wheel) to render scanned pages, and Pillow 12.3.0
(MIT-CMU) for bounded PNG output. These are direct, hash-locked runtime
dependencies and appear in `THIRD_PARTY_NOTICES.md` and the generated SBOM.

The supported image installs the local Tesseract engine and English trained data
from the distribution package. Tesseract is Apache-2.0 and Leptonica is
BSD-2-Clause. No Poppler, Ghostscript, OCRmyPDF, or other GPL/AGPL OCR renderer
is bundled. Do not replace this path or add additional language data, model
weights, or a remote OCR/LLM service without the normal license, provenance,
privacy, security, and operational review. The exact package/archive notices in
the built production image remain release artifacts alongside the SBOM.

## Automated gates

CI installs the checked-in hash-locked dependency graphs, runs `pip-audit` and `npm audit` against production dependencies, generates CycloneDX inventories from the production locks, and then calls the repository's complete `scripts/verify.sh` workflow. Audit or verification failures fail CI; exceptions require a documented risk decision with an owner and expiry date.

Run the dependency inventory locally after installing development dependencies:

```bash
bash scripts/sbom.sh
```

The command writes `artifacts/sbom-python.cdx.json` and `artifacts/sbom-npm.cdx.json`. The Python inventory represents the complete production lock; the npm inventory represents the complete lockfile, including build and test packages. These generated artifacts are CI outputs rather than hand-maintained source files.

## Release checks

Before release, confirm that dependency audits pass, inspect material license changes, review the SBOMs, rebuild from clean manifests, and retain the SBOMs with the release artifacts. Security reports go through the repository security process; do not disclose unresolved vulnerabilities in public issue trackers.
