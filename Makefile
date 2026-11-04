SHELL := /bin/bash
DC := docker compose

.DEFAULT_GOAL := help

.PHONY: help env gpu-check up down logs ps smoke health clean

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from .env.example if it does not exist
	@test -f .env || (cp .env.example .env && echo "created .env")

gpu-check: ## Verify the NVIDIA GPU is visible inside Docker
	@docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi

up: env ## Start the GPU serving tier and wait until healthy
	$(DC) up -d --wait vllm-gpu

down: ## Stop and remove containers (model cache is kept)
	$(DC) down

logs: ## Follow vLLM logs
	$(DC) logs -f vllm-gpu

ps: ## Show container status and health
	$(DC) ps

smoke: ## Run the end-to-end endpoint smoke test
	@python scripts/smoke.py

health: ## Query the vLLM health endpoint
	@curl -sf http://localhost:8010/health && echo " ok"

clean: ## Stop everything and delete the model cache volume
	$(DC) down -v
