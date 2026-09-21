# Everyday commands. `make check` is what CI (or a pre-push hook) should run.
PY := .venv/bin

.PHONY: install lint typecheck test test-unit e2e test-js test-graph audit check serve

install:            ## dev dependencies, the e2e browser, and the JS lint/test tools
	uv pip install --python $(PY)/python -e ".[dev]"
	$(PY)/python -m playwright install chromium
	npm install

lint:               ## ruff (Python) and eslint (dashboard JS)
	$(PY)/ruff check docrot tests
	npm run --silent lint

typecheck:
	$(PY)/mypy

test-unit:          ## fast: no browser, no services
	$(PY)/pytest -m "not e2e and not neo4j"

e2e:                ## the dashboard in headless Chromium
	$(PY)/pytest -m e2e

test-js:
	npm test

test-graph:         ## needs `docker compose up -d`; wipes the local graph
	$(PY)/pytest -m neo4j

audit:              ## known vulnerabilities in the Python and JS dependencies
	$(PY)/python -m pip_audit --progress-spinner off
	npm audit --omit=dev

test:               ## everything except the live graph, with coverage
	$(PY)/pytest --cov --cov-report=term-missing:skip-covered
	npm test

check: lint typecheck test

serve:
	$(PY)/docrot serve
