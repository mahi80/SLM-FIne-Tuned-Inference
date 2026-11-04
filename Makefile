SHELL := /bin/bash
DC := docker compose

.DEFAULT_GOAL := help

.PHONY: help env gpu-check build up down logs ps smoke smoke-gateway metrics health health-gateway clean

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

env: ## Create .env and config/tenants.yaml from their examples
	@test -f .env || (cp .env.example .env && echo "created .env")
	@test -f config/tenants.yaml || (cp config/tenants.example.yaml config/tenants.yaml && echo "created config/tenants.yaml")

gpu-check: ## Verify the NVIDIA GPU is visible inside Docker
	@docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi

build: env ## Build the gateway and simulator images
	$(DC) build

up: env ## Start the platform and wait until healthy
	$(DC) up -d --wait

down: ## Stop and remove containers (model cache is kept)
	$(DC) down

logs: ## Follow logs from all services
	$(DC) logs -f --tail=50

ps: ## Show container status and health
	$(DC) ps

smoke: ## Smoke-test the GPU tier directly
	@python scripts/smoke.py

smoke-gateway: ## Smoke-test both tiers through the gateway
	@python scripts/smoke_gateway.py

metrics: ## Print a sample of gateway metrics
	@curl -s http://localhost:8020/metrics | grep -E '^gateway_' | head -20

health: ## Query the GPU tier health directly
	@curl -sf http://localhost:8010/health && echo " ok"

health-gateway: ## Query the gateway health endpoint
	@curl -sf http://localhost:8020/health && echo " ok"

clean: ## Stop everything and delete volumes (model cache included)
	$(DC) down -v
