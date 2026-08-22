# Adapted from fullstack-solution-template-for-agentcore
# Makefile for code quality and formatting

RED := \033[0;31m
GREEN := \033[0;32m
YELLOW := \033[1;33m
NC := \033[0m

all: lint

# Run all linting and formatting with auto-fix
lint: ruff-lint format

# Run ruff linting checks and fix issues automatically
ruff-lint:
	ruff check --fix

# Format Python code
format:
	ruff format

# CI/CD version - checks only, no modifications
lint-cicd:
	@echo "Running code quality checks..."
	@if ! ruff check; then \
		echo -e "$(RED)ERROR: Ruff linting failed!$(NC)"; \
		echo -e "$(YELLOW)Please run 'make ruff-lint' locally to fix these issues.$(NC)"; \
		exit 1; \
	fi
	@if ! ruff format --check; then \
		echo -e "$(RED)ERROR: Code formatting check failed!$(NC)"; \
		echo -e "$(YELLOW)Please run 'make format' locally to fix these issues.$(NC)"; \
		exit 1; \
	fi
	@echo -e "$(GREEN)All code quality checks passed!$(NC)"

# Deploy CDK stacks
deploy:
	./scripts/deploy.sh deploy

# Synth CDK stacks
synth:
	cdk synth

# Run test scripts
test-agent:
	python scripts/test_agent.py

test-gateway:
	python scripts/test_gateway.py

test-memory:
	python scripts/test_memory.py

# Validate the control-library against its catalog (+ checkov if installed)
validate-controls:
	python scripts/validate_control_library.py

# Run control-library / policy_loader unit tests
test-controls:
	python -m pytest tests/ -q

# The same shell gate CI runs (.github/workflows/shell-checks.yml). The self-checks
# need bash 4+ for associative arrays and macOS ships 3.2 as /bin/bash, so prefer
# Homebrew's when it is there. Override with: make check-shell BASH=/path/to/bash
BASH ?= $(shell command -v /opt/homebrew/bin/bash 2>/dev/null || command -v bash)

check-shell:
	shellcheck --severity=warning $$(git ls-files '*.sh')
	$(BASH) scripts/check-deploy-config.sh
	$(BASH) scripts/check-workshop-flow.sh

.PHONY: all lint ruff-lint format lint-cicd deploy synth test-agent test-gateway test-memory validate-controls test-controls check-shell
