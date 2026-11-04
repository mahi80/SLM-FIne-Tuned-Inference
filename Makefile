SHELL := /bin/bash
DC := docker compose

.DEFAULT_GOAL := help

.PHONY: help env gpu-check build up down logs ps smoke smoke-gateway metrics health health-gateway clean \
        adapter-data adapter-train adapters adapter-eval adapter-smoke

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
	-@python scripts/load_adapters.py 2>/dev/null || true

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

adapter-data: ## Generate the synthetic triage dataset (deterministic)
	python -m finetune.dataset --out finetune/data

adapter-train: adapter-data ## Train adapter v1 and v2, then serve them (takes the GPU)
	$(DC) stop vllm-gpu
	$(DC) --profile train build train
	MSYS_NO_PATHCONV=1 $(DC) --profile train run --rm train -m finetune.train_qlora \
		--data finetune/data/train-v1.jsonl --out /adapters/triage-v1 --epochs 4
	MSYS_NO_PATHCONV=1 $(DC) --profile train run --rm train -m finetune.train_qlora \
		--data finetune/data/train-v2.jsonl --out /adapters/triage-v2 --epochs 4
	$(DC) up -d --wait vllm-gpu gateway
	@$(MAKE) adapters

adapters: ## Load every adapter under adapters/ into the running vLLM server
	@python scripts/load_adapters.py

adapter-eval: ## Score base vs adapters on the held-out triage set
	@python -m finetune.evaluate

adapter-smoke: ## Check the adapter aliases end to end through the gateway
	@python scripts/smoke_adapters.py

clean: ## Stop everything and delete volumes (model cache included)
	$(DC) down -v
