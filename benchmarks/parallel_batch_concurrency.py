"""Measure opt-in batch workers using frozen fixtures and the shipped CLI.

All providers are local fixtures. This experiment cannot accept a live provider
or replace the shared-authority pilot required for production activation.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
import time

from benchmarks.foreground_concurrency import _environment, _fake_provider, _git, _workload, provider_activity
from skodun import provenance, runner, services
from skodun.store import Store


def run_fixture(output, *, delay_seconds=1.0, provider_capacity=2):
    if type(provider_capacity) is not int or provider_capacity not in (1, 2):
        raise ValueError('fixture provider capacity must be 1 or 2')
    if type(delay_seconds) not in (int, float) or not 0 < delay_seconds <= 5:
        raise ValueError('fixture delay must be in (0,5] seconds')
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    repo, lanes = _workload(root)
    # The same four benign, complete diffs are used for both worker degrees.
    for lane in lanes:
        config = lane / '.skodun.toml'
        config.write_text(config.read_text() + 'max_diff_bytes = 4000\n')
        for index in range(4):
            (lane / f'part-{index}.txt').write_text(''.join(
                f'part-{index} line {line:04d}\n' for line in range(90)))
    binary = _fake_provider(root)
    database = root / 'fixture-authority.db'
    with Store.open(database):
        pass
    report = {'schema_version': 'parallel-batch-pilot/v1', 'provider_kind': 'hermetic',
        'build': provenance.code_provenance(), 'base_head': _git(repo, 'rev-parse', 'HEAD'),
        'worktree_count': len(lanes), 'delay_seconds': delay_seconds, 'trials': [],
        'limitations': ['Two single-trial profiles with fake provider latency.',
            'No live-provider speedup or token/dollar claim.',
            'Host memory and SQLite contention totals are unknown.']}
    for degree in (1, 2):
        trial = root / f'workers-{degree}'
        trial.mkdir()
        events = trial / 'provider-events.jsonl'
        env = _environment(root, database, binary, events, delay_seconds, (1, provider_capacity, False))
        results, rows, queues, gates = [], [], [], []
        started_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        began = time.monotonic()
        # Run requests sequentially to isolate batch overlap from foreground
        # concurrency. Each request's provider admission remains shared.
        for index, lane in enumerate(lanes, 1):
            out, err = trial / f'lane-{index}.json', trial / f'lane-{index}.stderr.txt'
            cmd = [sys.executable, '-m', 'skodun', 'review', '--repo', str(lane),
                '--reviewer', 'fixture', '--fresh', '--json', '--batch-concurrency', str(degree),
                '--request-key', f'batch-pilot-{degree}-{index}', '--max-queue-seconds', '30',
                '--max-review-seconds', '90', '--max-provider-wait-seconds', '30',
                '--max-wall-seconds', '120']
            result = runner.run_with_watchdog(cmd, 130, lane, out, err, env=env)
            try:
                payload = json.loads(out.read_text())
            except (OSError, ValueError):
                payload = None
            rows.append({'returncode': result.rc, 'timed_out': result.timed_out,
                'elapsed_seconds': result.duration_sec, 'result': payload,
                'stdout': out.name, 'stderr': err.name})
            if isinstance(payload, dict):
                results.append(payload)
        elapsed = time.monotonic() - began
        ended_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        activity = provider_activity([json.loads(line) for line in events.read_text().splitlines()]
                                     if events.exists() else [])
        records = []
        with Store.open_readonly(database) as store:
            for payload in results:
                request_id = payload['ids']['request_id']
                status, rendered = services.svc_queue(store, request_id=request_id, output='json')
                if status:
                    raise RuntimeError('fixture queue inspection failed')
                queues.append(json.loads(rendered))
                record = store.get_review(payload['ids']['review_id']) if payload['ids']['review_id'] else None
                if record:
                    records.append(record)
            active = store._c.execute("SELECT COUNT(*) FROM capacity_admissions WHERE status IN ('queued','admitted','running')").fetchone()[0]
        for lane in lanes:
            gate = subprocess.run([sys.executable, '-m', 'skodun', 'gate', '--repo', str(lane)],
                env=env, text=True, capture_output=True, timeout=20)
            gates.append(gate.returncode)
        (trial / 'queue.json').write_text(json.dumps(queues, indent=2, sort_keys=True) + '\n')
        (trial / 'reviews.json').write_text(json.dumps(records, indent=2, sort_keys=True) + '\n')
        report['trials'].append({'batch_concurrency': degree,
            'profile': {'foreground': 1, 'provider': provider_capacity, 'legacy_dual_hold': False},
            'sample_count': len(lanes), 'sample_denominator': 'requested reviews',
            'elapsed_sample_count': 1, 'elapsed_denominator': 'one four-request trial',
            'elapsed_seconds': elapsed, 'timing_method': 'monotonic trial wall time',
            'window': {'started_at': started_at, 'completed_at': ended_at},
            'provider_launches': activity['launches'], 'provider_peak': activity['peak'],
            'provider_intervals_complete': activity['complete'],
            'trustworthy_completed': sum(p['coverage']['trustworthy'] is True
                and p['execution']['exit_code'] in (0, 1) for p in results),
            'unique_request_count': len({p['ids']['request_id'] for p in results}),
            'result_count': len(results),
            'outcome_counts': dict(Counter(p['execution']['reason_code'] for p in results)),
            'batch_counts': [r.get('batch_count') for r in records],
            'boundary_digests': [(r.get('batch_plan') or {}).get('boundary_digest') for r in records],
            'integration_observations': [(r.get('integration') or {}).get('telemetry') for r in records],
            'diff_hashes': [p['identity']['requested']['diff_hash'] for p in results],
            'identity_matches': [p['identity']['requested']['diff_hash'] == p['identity']['observed']['diff_hash'] for p in results],
            'gate_codes': gates, 'active_admissions_after': active,
            'token_cost': None, 'results': rows, 'artifact_directory': trial.name})
    (root / 'report.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--delay-seconds', type=float, default=1)
    parser.add_argument('--provider-capacity', type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    report = run_fixture(args.output, delay_seconds=args.delay_seconds, provider_capacity=args.provider_capacity)
    print(json.dumps({'report': str(args.output.resolve() / 'report.json'),
        'trials': [{k: row[k] for k in ('batch_concurrency', 'elapsed_seconds',
            'provider_launches', 'provider_peak', 'trustworthy_completed')} for row in report['trials']]}, indent=2))


if __name__ == '__main__':
    main()
