# Common tasks. Run from ~/docker/newsroom.
COMPOSE := docker compose

.DEFAULT_GOAL := help
.PHONY: help init up down restart logs ps shell backup-db migrate test audit verify lint

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

init: ## One-time host setup: /storage dirs + .env
	./scripts/init-host.sh

up: ## Build and start
	$(COMPOSE) up -d --build

down: ## Stop
	$(COMPOSE) down

restart: ## Restart both containers
	$(COMPOSE) restart

logs: ## Follow logs
	$(COMPOSE) logs -f --tail=200

ps: ## Status and health
	$(COMPOSE) ps

shell: ## Shell in the worker container
	$(COMPOSE) exec worker sh

backup-db: ## Write a SQLite snapshot now
	$(COMPOSE) exec worker newsroom backup

migrate: ## Apply database migrations (the worker also does this on start)
	$(COMPOSE) exec worker newsroom migrate

test: ## Lint + tests in a throwaway build stage
	docker build --target test -t newsroom:test .

audit: ## pip-audit the locked dependencies
	docker build --target audit --no-cache-filter audit -t newsroom:audit .

verify: ## Check the port is reachable on Tailscale only
	./scripts/verify-binding.sh

lint: ## Lint/format check with local uv (dev machines)
	uv run ruff check . && uv run ruff format --check .
