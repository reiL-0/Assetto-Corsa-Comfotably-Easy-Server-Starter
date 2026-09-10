.PHONY: install run dev-web web test lint clean

VENV := .venv
PY := $(VENV)/bin/python

# Create venv and install backend + dev deps (editable).
install:
	test -d $(VENV) || python3 -m venv $(VENV)
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e ".[dev]"

# Backend on http://127.0.0.1:8080 with autoreload.
run:
	$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload

# Build the React frontend into app/static so the backend can serve it.
web:
	cd web && npm install && npm run build

# Frontend dev server (hot reload), proxies /api + /healthz to :8080.
dev-web:
	cd web && npm run dev

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check .

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache acmanager.egg-info
	rm -f data/acmanager.db data/acmanager.db-wal data/acmanager.db-shm
	find . -type d -name __pycache__ -exec rm -rf {} +
