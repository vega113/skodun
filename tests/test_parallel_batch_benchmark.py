"""The bounded batch benchmark measures actual shipped CLI fixture execution."""
import pytest
from benchmarks.parallel_batch_concurrency import run_fixture


def test_parallel_benchmark_rejects_invalid_delay_without_creating_artifacts(tmp_path):
    for value in (True, 0, -1, 6, float('nan'), float('inf'), 10**1000):
        with pytest.raises(ValueError):
            run_fixture(tmp_path / 'unused', delay_seconds=value)
        assert not (tmp_path / 'unused').exists()


@pytest.mark.parametrize('provider_capacity', [1, 2])
def test_parallel_benchmark_preserves_workload_calls_and_coverage(tmp_path, provider_capacity):
    report = run_fixture(tmp_path / 'fixture', delay_seconds=.3, provider_capacity=provider_capacity)
    one, two = report['trials']
    for trial in (one, two):
        assert trial['sample_count'] == trial['unique_request_count'] == trial['trustworthy_completed'] == 4
        assert trial['elapsed_seconds'] > 0 and trial['elapsed_sample_count'] == 1
        assert trial['identity_matches'] == [True]*4
        assert trial['gate_codes'] == [0]*4
        assert trial['active_admissions_after'] == 0
        assert trial['provider_intervals_complete'] is True
        assert trial['token_cost'] is None
        assert trial['provider_launches'] > 4  # This must really exercise batching.
        assert len(trial['batch_counts']) == 4 and all(n > 1 for n in trial['batch_counts'])
        assert trial['result_count'] == 4
    assert one['diff_hashes'] == two['diff_hashes']
    assert one['batch_counts'] == two['batch_counts']
    assert one['boundary_digests'] == two['boundary_digests']
    assert one['provider_launches'] == two['provider_launches']
    assert one['provider_peak'] == 1 and two['provider_peak'] == provider_capacity
    assert one['profile']['provider'] == two['profile']['provider'] == provider_capacity


def test_provider_capacity_refuses_invalid_values_without_artifacts(tmp_path):
    for value in (True, False, 0, 3, None, '2'):
        with pytest.raises(ValueError):
            run_fixture(tmp_path / 'unused', provider_capacity=value)
        assert not (tmp_path / 'unused').exists()
