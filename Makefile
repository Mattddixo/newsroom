# Common tasks. Run from ~/docker/newsroom.
COMPOSE := docker compose

.DEFAULT_GOAL := help
.PHONY: help setup init host-setup up down restart logs ps status shell ingest-now retag config-check outlets unmatched ownership funding backup-db migrate test audit verify lint

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

setup: ## Everything, once: make setup CONTACT_EMAIL=you@example.org
	CONTACT_EMAIL="$(CONTACT_EMAIL)" TAILSCALE_IP="$(TAILSCALE_IP)" ./scripts/init-host.sh
	./scripts/host-setup.sh
	docker build --target test -t newsroom:test .
	$(COMPOSE) up -d --build --wait
	./scripts/verify-binding.sh
	$(COMPOSE) exec worker newsroom config check

init: ## Storage dirs + .env (make init CONTACT_EMAIL=you@example.org)
	CONTACT_EMAIL="$(CONTACT_EMAIL)" TAILSCALE_IP="$(TAILSCALE_IP)" ./scripts/init-host.sh

host-setup: ## UFW rules + Docker-waits-for-Tailscale drop-in (sudo)
	./scripts/host-setup.sh

up: ## Build, start, and wait until both containers are healthy
	$(COMPOSE) up -d --build --wait

down: ## Stop
	$(COMPOSE) down

restart: ## Restart both containers
	$(COMPOSE) restart

logs: ## Follow logs
	$(COMPOSE) logs -f --tail=200

ps: ## Status and health
	$(COMPOSE) ps

status: ## Overview: ingestion, ownership, funding, backups
	$(COMPOSE) exec worker newsroom status

shell: ## Shell in the worker container
	$(COMPOSE) exec worker sh

ingest-now: ## Fetch new articles now
	$(COMPOSE) exec worker newsroom ingest

retag: ## Recompute tags after editing config/tags.yaml
	$(COMPOSE) exec worker newsroom retag

config-check: ## Validate config/outlets.yaml and config/tags.yaml
	$(COMPOSE) exec worker newsroom config check

outlets: ## List outlets with article counts
	$(COMPOSE) exec worker newsroom outlets list

unmatched: ## Outlets without a Wikidata match, with candidates
	$(COMPOSE) exec worker newsroom outlets unmatched

ownership: ## Re-resolve ownership for every outlet now
	$(COMPOSE) exec worker newsroom ownership resolve --all

funding: ## Look up funding records now and apply config/public_funding.yaml
	$(COMPOSE) exec worker newsroom funding refresh

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
