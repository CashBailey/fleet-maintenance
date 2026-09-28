# Third-party notices

Fleetline depends on the software below. The package version remains the source of the complete copyright and license text; this file is an attribution index, not a replacement for those licenses. A complete machine-readable dependency inventory can be generated with `bash scripts/sbom.sh`.

## Python runtime

| Component | Version | License | Project |
| --- | ---: | --- | --- |
| Django | 5.2.17 | BSD-3-Clause | <https://www.djangoproject.com/> |
| Django REST framework | 3.18.0 | BSD-3-Clause | <https://www.django-rest-framework.org/> |
| drf-spectacular | 0.30.0 | BSD-3-Clause | <https://github.com/tfranzel/drf-spectacular> |
| Gunicorn | 23.0.0 | MIT | <https://gunicorn.org/> |
| Pillow | 12.3.0 | MIT-CMU | <https://python-pillow.org/> |
| Psycopg and psycopg-binary | 3.3.5 | LGPL-3.0-only | <https://www.psycopg.org/psycopg3/> |
| pypdf | 6.16.2 | BSD-3-Clause | <https://pypdf.readthedocs.io/> |
| pypdfium2 / PDFium | 5.13.0 | Apache-2.0 OR BSD-3-Clause; wheel includes PDFium dependency notices | <https://pypdfium2.readthedocs.io/> |
| WhiteNoise | 6.12.0 | MIT | <https://whitenoise.readthedocs.io/> |

## Browser runtime

| Component | Version | License | Project |
| --- | ---: | --- | --- |
| Lucide React | 1.40.0 | ISC | <https://lucide.dev/> |
| React | 19.2.8 | MIT | <https://react.dev/> |
| React DOM | 19.2.8 | MIT | <https://react.dev/> |
| React Router DOM | 7.18.3 | MIT | <https://reactrouter.com/> |

## Development, build, test, and audit tools

| Component | Version | License | Project |
| --- | ---: | --- | --- |
| axe-core Playwright integration | 4.13.0 | MPL-2.0 | <https://github.com/dequelabs/axe-core-npm> |
| ESLint and first-party React plugins | 10.9.1 / 7.1.1 / 0.5.6 | MIT | <https://eslint.org/> |
| Playwright Test | 1.62.1 | Apache-2.0 | <https://playwright.dev/> |
| TypeScript | 6.0.3 | Apache-2.0 | <https://www.typescriptlang.org/> |
| typescript-eslint | 8.69.0 | MIT | <https://typescript-eslint.io/> |
| Vite and its React plugin | 8.2.2 / 6.1.1 | MIT | <https://vite.dev/> |
| coverage.py | 7.13.4 | Apache-2.0 | <https://coverage.readthedocs.io/> |
| CycloneDX Python | 7.2.2 | Apache-2.0 | <https://github.com/CycloneDX/cyclonedx-python> |
| django-stubs | 5.2.9 | MIT | <https://github.com/typeddjango/django-stubs> |
| mypy | 1.19.1 | MIT | <https://www.mypy-lang.org/> |
| pip-audit | 2.10.0 | Apache-2.0 | <https://github.com/pypa/pip-audit> |
| Ruff | 0.15.4 | MIT | <https://docs.astral.sh/ruff/> |

The `@types/*`, `@eslint/js`, and `globals` packages used by the build are MIT licensed. Their exact versions and all transitive JavaScript packages are recorded in `package-lock.json` and the npm CycloneDX inventory.

The tables above highlight direct dependencies. Exact versions and artifact hashes for all transitive Python packages are recorded in `requirements.lock` and `requirements-dev.lock`; the generated Python CycloneDX inventory covers the complete production lock.

## Deployment platform components

| Component | Version used by repository configuration | License | Project |
| --- | ---: | --- | --- |
| PostgreSQL | 16.15 | PostgreSQL License | <https://www.postgresql.org/> |
| Caddy container | 2.11 series | Apache-2.0 | <https://caddyserver.com/> |
| Python container | 3.12 series | PSF-2.0 | <https://www.python.org/> |
| Node.js build container | 22 series | MIT | <https://nodejs.org/> |
| Tesseract OCR and English trained data | Installed in the application image | Apache-2.0 | <https://github.com/tesseract-ocr/tesseract> |
| Leptonica (Tesseract image library) | Distribution runtime dependency | BSD-2-Clause | <http://www.leptonica.org/> |

No third-party source code or visual asset has been copied into the application by this notice. External service payload fixtures, when present, remain factual interoperability data and must not include vendor secrets or customer data.
