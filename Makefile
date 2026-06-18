# Format: PYTHON_VERSION:ANSIBLE_CORE_VERSION:HASHIVAULT_MODULE_VERSION
MATRIX := 3.14:2.20.5:5.6.0 3.12:2.19.3:5.4.0

COMPOSE := docker compose -f docker/docker-compose.yml

# Default to first matrix pair for single-pair targets
PAIR ?= $(firstword $(MATRIX))
_PY  = $(word 1,$(subst :, ,$(PAIR)))
_AC  = $(word 2,$(subst :, ,$(PAIR)))
_HV  = $(word 3,$(subst :, ,$(PAIR)))

.PHONY: help build test test-unit test-int bench demo shell clean

# Extra args forwarded to bench/benchmark.py, e.g. BENCH_ARGS='-n 500 -p copy,stat'
BENCH_ARGS ?=

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
	@printf "  bench       Benchmark fast plugins vs stock ansible-core (no external services)\n"
	@printf "  demo        Tour the summary, AFLP_DISABLE/AFLP_STRICT env vars, and a mini benchmark\n"
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
	@printf "  make shell PAIR=3.12:2.19.3:5.4.0   # drop into a specific (non-default) image\n"
	@printf "  make bench                           # default benchmark (PAIR image)\n"
	@printf "  make bench BENCH_ARGS='-n 500 -p copy,stat'  # custom size/plugins\n"
	@printf "  make demo                            # see the summary + AFLP_DISABLE contrast\n"

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

bench:
	PYTHON_VERSION=$(_PY) ANSIBLE_CORE_VERSION=$(_AC) HASHIVAULT_MODULE_VERSION=$(_HV) \
		$(COMPOSE) run --rm --no-deps --entrypoint "" controller \
		python bench/benchmark.py $(BENCH_ARGS)

demo:
	@printf "\n========================================================================\n"
	@printf " Normal run — fast plugins active. Watch the per-action summary at the end\n"
	@printf " (lineinfile shows one fallback: the mode arg forces the stock module).\n"
	@printf "========================================================================\n"
	PYTHON_VERSION=$(_PY) ANSIBLE_CORE_VERSION=$(_AC) HASHIVAULT_MODULE_VERSION=$(_HV) \
		$(COMPOSE) run --rm --no-deps --entrypoint "" controller \
		ansible-playbook -i tests/integration/inventory/local.ini bench/playbooks/demo.yml
	@printf "\n========================================================================\n"
	@printf " Same play with AFLP_DISABLE=1 — every task forced to stock ansible-core.\n"
	@printf " The summary now reports fast=0 fallback=N for every action.\n"
	@printf "========================================================================\n"
	PYTHON_VERSION=$(_PY) ANSIBLE_CORE_VERSION=$(_AC) HASHIVAULT_MODULE_VERSION=$(_HV) \
		$(COMPOSE) run --rm --no-deps -e AFLP_DISABLE=1 --entrypoint "" controller \
		ansible-playbook -i tests/integration/inventory/local.ini bench/playbooks/demo.yml
	@printf "\n========================================================================\n"
	@printf " Same play with AFLP_STRICT=1 — fallbacks are forbidden. The run is\n"
	@printf " EXPECTED to fail at the lineinfile-with-mode task (an unintended fallback).\n"
	@printf "========================================================================\n"
	@PYTHON_VERSION=$(_PY) ANSIBLE_CORE_VERSION=$(_AC) HASHIVAULT_MODULE_VERSION=$(_HV) \
		$(COMPOSE) run --rm --no-deps -e AFLP_STRICT=1 --entrypoint "" controller \
		ansible-playbook -i tests/integration/inventory/local.ini bench/playbooks/demo.yml \
		|| printf "\n  ^ Expected: AFLP_STRICT refused the fallback task and failed the run.\n"
	@printf "\n========================================================================\n"
	@printf " And the payoff — fast vs stock ansible-core for copy and stat (50 iters):\n"
	@printf "========================================================================\n"
	PYTHON_VERSION=$(_PY) ANSIBLE_CORE_VERSION=$(_AC) HASHIVAULT_MODULE_VERSION=$(_HV) \
		$(COMPOSE) run --rm --no-deps --entrypoint "" controller \
		python bench/benchmark.py -n 50 -p copy,stat

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
