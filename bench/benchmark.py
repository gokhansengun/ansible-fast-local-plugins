#!/usr/bin/env python3
"""Benchmark the fast local action plugins against stock ansible-core.

For each plugin, runs bench/playbooks/bench.yml twice — once with the fast
action plugins enabled (the repo's ./ansible.cfg) and once forced to stock
ansible-core (bench/ansible_stock.cfg) — then reports the per-task wall-clock
for each and the resulting speedup.

The figure reported for a plugin is:

    time(bench_iterations=N) - time(bench_iterations=0)

i.e. the cost of the N timed loop iterations with ansible startup and per-run
setup subtracted out, so it isolates the plugin's own per-invocation cost. The
N>0 run is repeated --repeat times and the median delta is reported.

This script needs only ansible-core and the standard file/command/template
modules; it does not require any of the Docker Compose services. Run it inside
the controller image (see `make bench`) or anywhere ansible-playbook is on PATH.
"""
from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH_DIR = os.path.join(REPO_ROOT, 'bench')
PLAYBOOK = os.path.join(BENCH_DIR, 'playbooks', 'bench.yml')
INVENTORY = os.path.join(REPO_ROOT, 'tests', 'integration', 'inventory', 'local.ini')
FAST_CFG = os.path.join(REPO_ROOT, 'ansible.cfg')
STOCK_CFG = os.path.join(BENCH_DIR, 'ansible_stock.cfg')

ALL_PLUGINS = ['stat', 'copy', 'template', 'file', 'command', 'tempfile']

MODES = (
    # (label, config path, expect_fast)
    ('stock', STOCK_CFG, False),
    ('fast', FAST_CFG, True),
)


def _run_playbook(plugin: str, cfg: str, iterations: int, expect_fast: bool) -> float:
    """Run bench.yml once and return its wall-clock seconds. Aborts on failure."""
    env = dict(os.environ)
    env['ANSIBLE_CONFIG'] = cfg
    # Quieten output; we only care about the exit code and our own timing.
    env.setdefault('ANSIBLE_STDOUT_CALLBACK', 'default')
    cmd = [
        'ansible-playbook', '-i', INVENTORY, PLAYBOOK,
        '-e', f'bench_plugin={plugin}',
        '-e', f'bench_iterations={iterations}',
        '-e', f'bench_expect_fast={str(expect_fast).lower()}',
    ]
    start = time.perf_counter()
    proc = subprocess.run(cmd, cwd=REPO_ROOT, env=env,
                          capture_output=True, text=True)
    elapsed = time.perf_counter() - start
    if proc.returncode != 0:
        sys.stderr.write(
            f'\nbenchmark run failed: plugin={plugin} mode='
            f'{"fast" if expect_fast else "stock"} iterations={iterations}\n'
            f'--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n'
        )
        raise SystemExit(1)
    return elapsed


def _measure(plugin: str, cfg: str, expect_fast: bool, iterations: int,
             repeat: int, warmup: int) -> float:
    """Return the median per-task time for one plugin in one mode."""
    # One overhead sample (iterations=0): ansible startup + setup, no timed work.
    overhead = _run_playbook(plugin, cfg, 0, expect_fast)
    for _ in range(warmup):
        _run_playbook(plugin, cfg, iterations, expect_fast)
    deltas = []
    for _ in range(repeat):
        total = _run_playbook(plugin, cfg, iterations, expect_fast)
        deltas.append(max(total - overhead, 0.0))
    return statistics.median(deltas)


def _fmt(seconds: float) -> str:
    return f'{seconds:8.3f}s'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-n', '--iterations', type=int, default=200,
                        help='timed loop iterations per plugin (default: 200)')
    parser.add_argument('-r', '--repeat', type=int, default=3,
                        help='timed repetitions; median is reported (default: 3)')
    parser.add_argument('-w', '--warmup', type=int, default=1,
                        help='discarded warmup runs per mode (default: 1)')
    parser.add_argument('-p', '--plugins', default=','.join(ALL_PLUGINS),
                        help='comma-separated plugins to benchmark '
                             f'(default: {",".join(ALL_PLUGINS)})')
    args = parser.parse_args()

    plugins = [p.strip() for p in args.plugins.split(',') if p.strip()]
    unknown = [p for p in plugins if p not in ALL_PLUGINS]
    if unknown:
        parser.error(f'unknown plugin(s): {", ".join(unknown)}; '
                     f'choose from {", ".join(ALL_PLUGINS)}')

    print(f'AFLP benchmark — {args.iterations} iterations, '
          f'median of {args.repeat} (warmup {args.warmup})')
    print(f'{"plugin":<10} {"stock":>10} {"fast":>10} {"speedup":>10}')
    print('-' * 44)

    rows = []
    for plugin in plugins:
        times = {}
        for label, cfg, expect_fast in MODES:
            times[label] = _measure(plugin, cfg, expect_fast,
                                    args.iterations, args.repeat, args.warmup)
        speedup = times['stock'] / times['fast'] if times['fast'] > 0 else float('inf')
        rows.append((plugin, times['stock'], times['fast'], speedup))
        print(f'{plugin:<10} {_fmt(times["stock"])} {_fmt(times["fast"])} '
              f'{speedup:9.1f}x')

    total_stock = sum(r[1] for r in rows)
    total_fast = sum(r[2] for r in rows)
    total_speedup = total_stock / total_fast if total_fast > 0 else float('inf')
    print('-' * 44)
    print(f'{"TOTAL":<10} {_fmt(total_stock)} {_fmt(total_fast)} '
          f'{total_speedup:9.1f}x')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
