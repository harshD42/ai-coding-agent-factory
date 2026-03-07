.PHONY: up down restart logs test lint format build clean help

PROFILE ?= laptop
COMPOSE  = docker compose --profile $(PROFILE)

# ── Stack ──────────────────────────────────────────────────────────────────────

up:          ## Start the stack (PROFILE=laptop|gpu-shared|gpu)
	$(COMPOSE) up -d

down:        ## Stop and remove containers
	$(COMPOSE) down

restart:     ## Restart orchestrator (picks up code changes)
	docker compose restart orchestrator

rebuild:     ## Rebuild and restart all custom images
	$(COMPOSE) up -d --build

logs:        ## Follow orchestrator logs
	docker compose logs -f orchestrator

logs-all:    ## Follow all container logs
	$(COMPOSE) logs -f

ps:          ## Show container status
	docker compose ps

# ── Development ────────────────────────────────────────────────────────────────

test:        ## Run test suite
	docker compose run --rm orchestrator python -m pytest /app/../tests -v

test-unit:   ## Run unit tests only (no integration)
	docker compose run --rm orchestrator python -m pytest /app/../tests/unit -v

lint:        ## Check code style with ruff
	docker compose run --rm orchestrator python -m ruff check /app

format:      ## Auto-format code with ruff
	docker compose run --rm orchestrator python -m ruff format /app

typecheck:   ## Run mypy type checking
	docker compose run --rm orchestrator python -m mypy /app --ignore-missing-imports

# ── Operations ─────────────────────────────────────────────────────────────────

index:       ## Re-index the workspace codebase
	curl -s -X POST http://localhost:9000/v1/index | python -m json.tool

status:      ## Show system status
	curl -s http://localhost:9000/health | python -m json.tool

pull-models: ## Pull Ollama models (laptop profile)
	docker exec aicaf-ollama-1 ollama pull qwen2.5-coder:7b
	docker exec aicaf-ollama-1 ollama pull nomic-embed-text

# ── Cleanup ────────────────────────────────────────────────────────────────────

clean:       ## Remove containers and volumes (DESTRUCTIVE — loses ChromaDB/Redis data)
	docker compose down -v

clean-images: ## Remove built images (forces full rebuild)
	docker rmi aicaf-orchestrator aicaf-executor 2>/dev/null || true

# ── Help ───────────────────────────────────────────────────────────────────────

help:        ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help