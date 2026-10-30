.PHONY: setup up down logs test dev pull-model ps

setup:            ## first-time setup on macOS
	bash scripts/setup_mac.sh

up:               ## build and start everything
	docker compose up -d --build

down:
	docker compose down

ps:
	docker compose ps

logs:
	docker compose logs -f --tail=100 api sandbox scheduler

pull-model:
	ollama pull $$(grep ^LLM_MODEL .env | cut -d= -f2)

test:             ## run the test suite locally
	cd backend && python -m pytest -q

dev:              ## run the API without Docker (SQLite, simulated feed)
	cd backend && ADMIN_TOKEN=dev LLM_BASE_URL=http://localhost:11434/v1 uvicorn yonexis.api.main:app --reload --port 8000
