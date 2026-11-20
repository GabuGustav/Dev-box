.PHONY: install dev db-up migrate test lint format run
install:
	pip install -e ".[dev]"
dev:
	uvicorn app.main:app --reload
db-up:
	docker compose up -d db
migrate:
	alembic upgrade head
test:
	pytest
lint:
	ruff check .
format:
	ruff format .
run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000
