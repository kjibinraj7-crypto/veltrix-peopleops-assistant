.PHONY: run dev test lint clean docker-build docker-up docker-down

# ─── Local development ────────────────────────────────────────────────────────
run:
	uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload

dev:
	uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload --log-level debug

# ─── Testing ──────────────────────────────────────────────────────────────────
test:
	pytest tests/ -v --tb=short

test-unit:
	pytest tests/ -v --tb=short -k "not test_chat_api"

test-integration:
	pytest tests/test_chat_api.py -v --tb=short

# ─── Docker ───────────────────────────────────────────────────────────────────
docker-build:
	docker compose build

docker-up:
	docker compose up

docker-up-d:
	docker compose up -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

docker-clean:
	docker compose down --volumes --rmi all

# ─── Code quality ─────────────────────────────────────────────────────────────
lint:
	ruff check src/ tests/

format:
	ruff format src/ tests/

# ─── Cleanup ──────────────────────────────────────────────────────────────────
clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache
	rm -rf vector_store/
