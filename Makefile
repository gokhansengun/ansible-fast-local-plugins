# Format: PYTHON_VERSION:ANSIBLE_CORE_VERSION:HASHIVAULT_MODULE_VERSION
MATRIX := 3.12:2.19.3:5.4.0 3.14:2.20.5:5.6.0

COMPOSE := docker compose -f docker/docker-compose.yml

# Default to first matrix pair for single-pair targets
PAIR ?= $(firstword $(MATRIX))
_PY  = $(word 1,$(subst :, ,$(PAIR)))
_AC  = $(word 2,$(subst :, ,$(PAIR)))
_HV  = $(word 3,$(subst :, ,$(PAIR)))

.PHONY: help build test test-unit test-int shell clean

help:
	@printf "ansible-fast-local-plugins — test harness\n"
	@printf "\n"
	@printf "Usage:\n"
	@printf "  make <target> [MATRIX='py:ac:hv ...'] [PAIR='py:ac:hv']\n"
	@printf "\n"
	@printf "Targets:\n"
	@printf "  build       Build the controller Docker image for every matrix pair\n"
	@printf "  test        Run unit + integration tests for every matrix pair\n"
	@printf "  test-unit   Run unit tests only (no external services, uses --no-deps)\n"
	@printf "  test-int    Run integration tests only (starts full Docker Compose stack)\n"
	@printf "  shell       Open a bash shell inside the controller container\n"
	@printf "  clean       Tear down all Compose stacks and delete local build artifacts\n"
	@printf "  help        Show this message\n"
	@printf "\n"
	@printf "Matrix format:  PYTHON_VERSION:ANSIBLE_CORE_VERSION:HASHIVAULT_MODULE_VERSION\n"
	@printf "Current matrix: $(MATRIX)\n"
	@printf "\n"
	@printf "Examples:\n"
	@printf "  make test                            # all pairs\n"
	@printf "  make test MATRIX='3.12:2.19.3:5.4.0'  # one specific pair\n"
	@printf "  make test-unit                       # fast, no Docker services\n"
	@printf "  make shell PAIR=3.14:2.20.5:5.6.0   # drop into a specific image\n"

build:
	@$(call foreach_pair, \
		PYTHON_VERSION=$$py ANSIBLE_CORE_VERSION=$$ac HASHIVAULT_MODULE_VERSION=$$hv \
			$(COMPOSE) build controller)

test:
	@failed=0; \
	for pair in $(MATRIX); do \
		py=$$(echo $$pair | cut -d: -f1); \
		ac=$$(echo $$pair | cut -d: -f2); \
		hv=$$(echo $$pair | cut -d: -f3); \
		echo ""; \
		echo "==> py=$$py  ansible-core=$$ac  hashivault=$$hv"; \
		echo "------------------------------------------------------------"; \
		PYTHON_VERSION=$$py ANSIBLE_CORE_VERSION=$$ac HASHIVAULT_MODULE_VERSION=$$hv \
		PYTEST_CMD="pytest tests/ -v --cov=ansible/plugins/action_plugins --cov-report=term-missing --cov-report=html:/test-results/coverage-py$$py-ansible$$ac --cov-fail-under=80" \
			$(COMPOSE) up --build -d; \
		cid=$$(PYTHON_VERSION=$$py ANSIBLE_CORE_VERSION=$$ac HASHIVAULT_MODULE_VERSION=$$hv \
			$(COMPOSE) ps -q controller 2>/dev/null | head -1); \
		PYTHON_VERSION=$$py ANSIBLE_CORE_VERSION=$$ac HASHIVAULT_MODULE_VERSION=$$hv \
			$(COMPOSE) logs --follow controller || true; \
		rc=$$(docker inspect --format='{{.State.ExitCode}}' "$$cid" 2>/dev/null || echo 1); \
		[ "$$rc" = "0" ] || failed=1; \
		PYTHON_VERSION=$$py ANSIBLE_CORE_VERSION=$$ac HASHIVAULT_MODULE_VERSION=$$hv \
			$(COMPOSE) down -v --remove-orphans; \
	done; \
	exit $$failed

test-unit:
	@$(call foreach_pair, \
		PYTHON_VERSION=$$py ANSIBLE_CORE_VERSION=$$ac HASHIVAULT_MODULE_VERSION=$$hv \
			$(COMPOSE) run --rm --no-deps --entrypoint "" controller \
			pytest tests/unit/ -v -m "not integration" \
				--cov=ansible/plugins/action_plugins \
				--cov-report=term-missing \
				--cov-fail-under=80)

test-int:
	@failed=0; \
	for pair in $(MATRIX); do \
		py=$$(echo $$pair | cut -d: -f1); \
		ac=$$(echo $$pair | cut -d: -f2); \
		hv=$$(echo $$pair | cut -d: -f3); \
		echo ""; \
		echo "==> py=$$py  ansible-core=$$ac  hashivault=$$hv"; \
		echo "------------------------------------------------------------"; \
		PYTHON_VERSION=$$py ANSIBLE_CORE_VERSION=$$ac HASHIVAULT_MODULE_VERSION=$$hv \
		PYTEST_CMD="pytest tests/integration/ -v -m integration" \
			$(COMPOSE) up --build -d; \
		cid=$$(PYTHON_VERSION=$$py ANSIBLE_CORE_VERSION=$$ac HASHIVAULT_MODULE_VERSION=$$hv \
			$(COMPOSE) ps -q controller 2>/dev/null | head -1); \
		PYTHON_VERSION=$$py ANSIBLE_CORE_VERSION=$$ac HASHIVAULT_MODULE_VERSION=$$hv \
			$(COMPOSE) logs --follow controller || true; \
		rc=$$(docker inspect --format='{{.State.ExitCode}}' "$$cid" 2>/dev/null || echo 1); \
		[ "$$rc" = "0" ] || failed=1; \
		PYTHON_VERSION=$$py ANSIBLE_CORE_VERSION=$$ac HASHIVAULT_MODULE_VERSION=$$hv \
			$(COMPOSE) down -v --remove-orphans; \
	done; \
	exit $$failed

shell:
	PYTHON_VERSION=$(_PY) ANSIBLE_CORE_VERSION=$(_AC) HASHIVAULT_MODULE_VERSION=$(_HV) \
		$(COMPOSE) run --rm --no-deps --entrypoint bash controller

clean:
	@for pair in $(MATRIX); do \
		py=$$(echo $$pair | cut -d: -f1); \
		ac=$$(echo $$pair | cut -d: -f2); \
		hv=$$(echo $$pair | cut -d: -f3); \
		PYTHON_VERSION=$$py ANSIBLE_CORE_VERSION=$$ac HASHIVAULT_MODULE_VERSION=$$hv \
			$(COMPOSE) down -v --remove-orphans 2>/dev/null || true; \
	done
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache htmlcov .coverage

# Helper: iterate over MATRIX pairs, fail-fast on first error
define foreach_pair
	for pair in $(MATRIX); do \
		py=$$(echo $$pair | cut -d: -f1); \
		ac=$$(echo $$pair | cut -d: -f2); \
		hv=$$(echo $$pair | cut -d: -f3); \
		echo ""; \
		echo "==> py=$$py  ansible-core=$$ac  hashivault=$$hv"; \
		echo "------------------------------------------------------------"; \
		$(1) || exit 1; \
	done
endef

# Helper: iterate and accumulate failures (all pairs run even if one fails)
define foreach_pair_block
	for pair in $(MATRIX); do \
		py=$$(echo $$pair | cut -d: -f1); \
		ac=$$(echo $$pair | cut -d: -f2); \
		hv=$$(echo $$pair | cut -d: -f3); \
		echo ""; \
		echo "==> py=$$py  ansible-core=$$ac  hashivault=$$hv"; \
		echo "------------------------------------------------------------"; \
		$(1); \
	done; \
	exit $$failed
endef
