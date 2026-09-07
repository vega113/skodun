"""Bounded hermetic concurrency experiment through the shipped review CLI.

This harness only creates its own benign Git workload and fake provider. It has
no live-provider mode: a real pilot must use the existing shared authority and
its explicit maintenance/capacity policy, never this fixture database.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import platform
import resource
from pathlib import Path
import subprocess
import sys
import time

from skodun import provenance, runner, services
from skodun.store import Store

SOURCE = Path(__file__).resolve().parents[1] / 'src'


def peak_overlap(intervals):
    events = [(start, 1) for start, end in intervals if end >= start]
    events += [(end, -1) for start, end in intervals if end >= start]
    active = peak = 0
    for _, change in sorted(events):
        active += change
        peak = max(peak, active)
    return peak


def provider_activity(events):
    starts, ends = {}, {}
    valid = True
    for row in events:
        identifier, at = row.get('invocation_id'), row.get('time_ns')
        target = starts if row.get('kind') == 'start' else ends if row.get('kind') == 'end' else None
        if not isinstance(identifier, str) or not identifier or type(at) is not int or at < 0 or target is None or identifier in target:
            valid = False
            continue
        target[identifier] = at
    intervals = [(at, ends[key]) for key,at in starts.items() if key in ends and ends[key] >= at]
    complete = valid and starts.keys() == ends.keys() and len(intervals) == len(starts)
    return {'launches':len(starts), 'complete':complete,
            'peak':peak_overlap(intervals) if complete else None}


def _git(repo, *args):
    env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}
    return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null',
        '-c', 'commit.gpgsign=false', '-c', 'user.name=Skodun pilot fixture',
        '-c', 'user.email=fixture@example.invalid', '-C', str(repo), *args],
        text=True, stderr=subprocess.PIPE, env=env, timeout=20).strip()


def _workload(root):
    repo = root / 'source'
    repo.mkdir()
    _git(repo, 'init', '--initial-branch=main')
    (repo / 'README.md').write_text('A benign fixture for foreground review scheduling.\n')
    (repo / '.skodun.toml').write_text('''[[reviewers]]
name = "fixture"
provider = "xai"
model = "fixture"
role = "finder"
[defaults]
context_pack = false
timeout_sec = 10
timeout_retries = 0
degraded_retries = 0
''')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-m', 'Create the frozen benchmark baseline.')
    worktrees = []
    for index in range(4):
        path = root / f'lane-{index + 1}'
        _git(repo, 'worktree', 'add', '-b', f'codex/pilot-{index + 1}', str(path))
        (path / 'README.md').write_text(
            'A benign fixture for foreground review scheduling.\n'
            f'Lane {index + 1} contains its own complete, unchanged review diff.\n')
        worktrees.append(path)
    return repo, worktrees


def _fake_provider(root):
    executable = root / 'fixture-provider'
    executable.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys, time, uuid
if '--version' in sys.argv:
    print('skodun-pilot-fixture/v1')
    raise SystemExit(0)
path = pathlib.Path(os.environ['SKODUN_PILOT_EVENTS'])
invocation_id = uuid.uuid4().hex
def event(kind):
    value = {{'kind':kind, 'pid':os.getpid(), 'invocation_id':invocation_id, 'time_ns':time.monotonic_ns(),
             'worktree':pathlib.Path.cwd().name}}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try: os.write(fd, (json.dumps(value) + '\\n').encode())
    finally: os.close(fd)
event('start')
time.sleep(float(os.environ['SKODUN_PILOT_DELAY']))
print(json.dumps({{'structuredOutput':{{'summary':'Fixture completed.','findings':[]}},
                  'stopReason':'EndTurn'}}))
event('end')
''')
    executable.chmod(0o700)
    return executable


def _environment(root, database, binary, events, delay, profile):
    fg, provider, legacy = profile
    env = {key:value for key,value in os.environ.items() if not key.startswith('SKODUN_')}
    env.update(PYTHONPATH=str(SOURCE), PYTHONDONTWRITEBYTECODE='1',
        SKODUN_DB=str(database), SKODUN_CONFIG=str(root / 'absent-global.toml'),
        SKODUN_GROK_BIN=str(binary), SKODUN_SECURITY_PASS='0', SKODUN_SKEPTIC_PASS='0',
        SKODUN_LEGACY_FG_LOCK='1' if legacy else '0',
        SKODUN_REVIEW_FG_CAPACITY=str(fg), SKODUN_REVIEW_MACHINE_CAPACITY=str(fg),
        SKODUN_PROVIDER_MAX_IN_FLIGHT=str(provider),
        SKODUN_ADMISSION_WAIT_SECONDS='30', SKODUN_LOCK_POLL_SECONDS='0.02',
        SKODUN_PILOT_EVENTS=str(events), SKODUN_PILOT_DELAY=str(delay))
    return env


def _one(worktree, trial, index, env):
    out, err = trial / f'lane-{index}.json', trial / f'lane-{index}.stderr.txt'
    cmd = [sys.executable, '-m', 'skodun', 'review', '--repo', str(worktree),
        '--reviewer', 'fixture', '--fresh', '--json', '--request-key', f'{trial.name}-{index}',
        '--max-queue-seconds', '30', '--max-review-seconds', '10',
        '--max-provider-wait-seconds', '10', '--max-wall-seconds', '45']
    # The same ownership-checked watchdog used by Skodun supervises our CLI
    # children. It owns any timeout cleanup; no PID/group is guessed here.
    result = runner.run_with_watchdog(cmd, 50, worktree, out, err, env=env)
    try:
        payload = json.loads(out.read_text())
    except (OSError, ValueError):
        payload = None
    return {'returncode': result.rc, 'timed_out': result.timed_out,
            'elapsed_seconds': result.duration_sec, 'result': payload,
            'stdout': out.name, 'stderr': err.name}


def _trial(root, repo, worktrees, database, binary, delay, profile, ordinal):
    fg, provider, legacy = profile
    trial = root / f'trial-{ordinal}-fg{fg}-provider{provider}-legacy{int(legacy)}'
    trial.mkdir()
    events = trial / 'provider-events.jsonl'
    env = _environment(root, database, binary, events, delay, profile)
    started_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    start = time.monotonic()
    cpu_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_one, path, trial, i + 1, env) for i,path in enumerate(worktrees)]
        rows = [future.result() for future in futures]
    elapsed = time.monotonic() - start
    cpu_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    completed_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    parsed_events = [json.loads(line) for line in events.read_text().splitlines()] if events.exists() else []
    activity = provider_activity(parsed_events)
    results = [row['result'] for row in rows if isinstance(row['result'], dict)]
    request_ids = [row['ids']['request_id'] for row in results]
    queue, layers = [], []
    review_records = request_executions = 0
    with Store.open_readonly(database) as store:
        for rid in request_ids:
            review_records += store._c.execute(
                "SELECT COUNT(*) FROM reviews WHERE json_extract(artifact_json,'$.request_id')=?", (rid,)).fetchone()[0]
            request_executions += len(store.get_request(rid)['executions'])
            status, text = services.svc_queue(store, request_id=rid, output='json')
            if status:
                raise RuntimeError('fixture queue inspection failed')
            queue.append(json.loads(text))
            snapshot = store.request_budget(rid)
            if snapshot is not None:
                layers.extend(snapshot['capacity_layers'])
        active = store._c.execute("SELECT COUNT(*) FROM capacity_admissions WHERE status IN ('queued','admitted','running')").fetchone()[0]
    gates = []
    for path in worktrees:
        result = subprocess.run([sys.executable, '-m', 'skodun', 'gate', '--repo', str(path)],
            env=env, text=True, capture_output=True, timeout=20)
        gates.append(result.returncode)
    for row in results:
        if row['identity']['requested']['diff_hash'] != row['identity']['observed']['diff_hash']:
            raise RuntimeError('fixture review did not retain its frozen diff identity')
    (trial / 'queue.json').write_text(json.dumps(queue, indent=2, sort_keys=True) + '\n')
    return {'profile': {'foreground':fg, 'provider':provider, 'legacy_dual_hold':legacy},
        'effective_capacity': min((layer['effective_capacity'] for layer in layers), default=None),
        'capacity_layers_observed': layers,
        'sample_count': 4, 'sample_denominator':'requested reviews',
        'elapsed_sample_count':1, 'elapsed_denominator':'one four-request trial',
        'worktree_count':4, 'elapsed_seconds':elapsed,
        'window':{'started_at':started_at,'completed_at':completed_at},
        'small_sample':True, 'elapsed_unit':'seconds',
        'timing_method':'monotonic trial wall time; not sum of overlapping attempts',
        'trustworthy_completed':sum(row['execution']['exit_code'] in (0, 1)
            and row['coverage']['trustworthy'] is True for row in results),
        'unique_request_count':len(set(request_ids)),
        'review_record_count':review_records, 'request_execution_count':request_executions,
        'unique_diff_count':len({row['identity']['requested']['diff_hash'] for row in results}),
        'provider_launches':activity['launches'], 'provider_peak':activity['peak'],
        'provider_intervals_complete':activity['complete'],
        'gate_codes':gates, 'active_admissions_after':active,
        'unknown_token_cost': True, 'external_gate_lock_wait_ms':None,
        'pressure': {'observed_provider_peak':activity['peak'],
                     'database_bytes_after':database.stat().st_size,
                     'review_cli_children_user_seconds':cpu_after.ru_utime - cpu_before.ru_utime,
                     'review_cli_children_system_seconds':cpu_after.ru_stime - cpu_before.ru_stime,
                     'sqlite_busy_count':None, 'host_peak_memory_bytes':None},
        'results':rows, 'artifact_directory':trial.name}


def run_fixture(output, *, delay_seconds=2.0, profiles=((1, 2, False), (2, 2, False))):
    if type(delay_seconds) not in (int, float) or not 0 < delay_seconds <= 5 or not math.isfinite(delay_seconds):
        raise ValueError('fixture delay must be in (0,5] seconds')
    if not profiles or len(profiles) > 8:
        raise ValueError('provide one to eight bounded fixture profiles')
    for fg, provider, legacy in profiles:
        if type(fg) is not int or type(provider) is not int or not 1 <= fg <= 2 or not 1 <= provider <= 2 or type(legacy) is not bool:
            raise ValueError('fixture capacities must be one or two and legacy must be boolean')
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    repo, worktrees = _workload(root)
    binary = _fake_provider(root)
    database = root / 'fixture-authority.db'
    with Store.open(database):
        pass
    report = {'schema_version':'foreground-pilot/v1', 'provider_kind':'hermetic',
        'worktree_count':4, 'delay_seconds':delay_seconds, 'build':provenance.code_provenance(),
        'environment':{'python':platform.python_version(),'platform':platform.platform()},
        'base_head':_git(repo, 'rev-parse', 'HEAD'), 'trials':[],
        'limitations':['Fake provider scheduling is not a real-provider speedup measurement.',
                      'No installed authority, provider, live profile or production default was changed.',
                      'Small samples; token/spend, host-memory and SQLite contention totals are unknown.']}
    for i,profile in enumerate(profiles):
        report['trials'].append(_trial(root, repo, worktrees, database, binary, delay_seconds, profile, i + 1))
    (root / 'report.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='new output directory')
    parser.add_argument('--delay-seconds', type=float, default=2)
    parser.add_argument('--with-limiter-controls', action='store_true',
                        help='also run legacy-lock and provider-cap serialization controls')
    args = parser.parse_args()
    profiles = ((1, 2, False), (2, 2, False))
    if args.with_limiter_controls:
        profiles += ((2, 2, True), (2, 1, False))
    report = run_fixture(args.output, delay_seconds=args.delay_seconds, profiles=profiles)
    print(json.dumps({'report':str(args.output.resolve() / 'report.json'),
                      'trials':[{key:row[key] for key in ('sample_count','elapsed_seconds','provider_peak','trustworthy_completed')}
                                for row in report['trials']]}, indent=2))


if __name__ == '__main__':
    main()
