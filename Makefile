.PHONY: install lock build run seed test test-e2e verify migrate sbom

install:
	python3 -m venv .venv
	.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.lock
	npm ci

lock:
	uv pip compile requirements.txt --generate-hashes --universal --python-version 3.12 --output-file requirements.lock
	uv pip compile requirements-dev.txt --generate-hashes --universal --python-version 3.12 --output-file requirements-dev.lock

build:
	npm run build
	cd backend && DJANGO_SECRET_KEY=build-only-static-collection-key-not-valid-at-runtime-0123456789 ../.venv/bin/python manage.py collectstatic --noinput

run:
	./scripts/run-prod.sh

seed:
	cd backend && ../.venv/bin/python manage.py seed_demo

migrate:
	cd backend && ../.venv/bin/python manage.py migrate

test:
	cd backend && DJANGO_SECRET_KEY=test-only-secret-key-not-valid-for-any-deployment-0123456789 ../.venv/bin/coverage run manage.py test && ../.venv/bin/coverage report

test-e2e:
	./scripts/test-e2e.sh

verify:
	./scripts/verify.sh

sbom:
	bash scripts/sbom.sh
