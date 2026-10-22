.PHONY: up down migrate serve lint test test-db
up:        ; docker compose up -d db
down:      ; docker compose down
migrate:   ; .venv/bin/llmgw migrate
serve:     ; .venv/bin/llmgw serve
lint:      ; .venv/bin/ruff check src tests
test:      ; .venv/bin/pytest -q
# Needs POSTGRES_PASSWORD exported (set -a; . ./.env; set +a). Uses a throwaway database named llmgw_test.
test-db:   ; docker compose exec -T db psql -U llmgw -c "DROP DATABASE IF EXISTS llmgw_test" -c "CREATE DATABASE llmgw_test" && \
	TEST_DATABASE_URL=postgresql://llmgw:$$POSTGRES_PASSWORD@127.0.0.1:5437/llmgw_test .venv/bin/pytest -q
