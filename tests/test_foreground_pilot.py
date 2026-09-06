"""Hermetic four-worktree benchmark drives the installed CLI source path."""
from benchmarks.foreground_concurrency import run_fixture, peak_overlap


def test_interval_peak_respects_half_open_process_lifetimes():
    assert peak_overlap([(0, 2), (1, 3), (3, 4)]) == 2
    assert peak_overlap([(0, 1), (1, 2)]) == 1
    assert peak_overlap([]) == 0


def test_four_worktrees_obey_foreground_provider_and_legacy_caps(tmp_path):
    report = run_fixture(tmp_path / 'trial', delay_seconds=.3,
                         profiles=((1, 2, False), (2, 2, False), (2, 2, True), (2, 1, False)))
    assert report['provider_kind'] == 'hermetic'
    assert report['worktree_count'] == 4
    assert len(report['trials']) == 4
    for trial in report['trials']:
        assert trial['sample_count'] == 4
        assert trial['trustworthy_completed'] == 4
        assert trial['provider_launches'] == 4
        assert trial['provider_intervals_complete'] is True
        assert trial['capacity_layers_observed']
        assert trial['unique_request_count'] == 4
        assert trial['review_record_count'] == 4
        assert trial['request_execution_count'] == 4
        assert trial['unique_diff_count'] == 4
        assert trial['active_admissions_after'] == 0
        profile = trial['profile']
        assert 0 < trial['effective_capacity'] <= min(profile['foreground'], profile['provider'], 1 if profile['legacy_dual_hold'] else 2)
        assert trial['provider_peak'] <= trial['effective_capacity']
        assert all(layer['configured_capacity'] == profile['foreground']
                   for layer in trial['capacity_layers_observed'] if layer['resource_class'] == 'review-fg')
        assert trial['gate_codes'] == [0] * 4
        assert trial['unknown_token_cost'] is True
    assert report['trials'][0]['provider_peak'] == 1
    assert report['trials'][1]['provider_peak'] == report['trials'][1]['effective_capacity']
    assert report['trials'][2]['provider_peak'] == 1
    assert report['trials'][3]['provider_peak'] == 1
    assert (tmp_path / 'trial' / 'report.json').is_file()


def test_invalid_fixture_profile_never_creates_an_authority(tmp_path):
    import pytest
    for profile in (((True, 2, False),), ((3, 2, False),), ((1, 2, 'false'),)):
        with pytest.raises(ValueError):
            run_fixture(tmp_path / 'refused', profiles=profile)
        assert not (tmp_path / 'refused').exists()
    with pytest.raises(ValueError):
        run_fixture(tmp_path / 'refused', delay_seconds=10 ** 1000)
    assert not (tmp_path / 'refused').exists()


def test_provider_observations_use_invocation_identity_and_keep_missing_ends_unknown():
    from benchmarks.foreground_concurrency import provider_activity
    events = [{'kind':kind,'pid':12,'invocation_id':identifier,'time_ns':at}
              for identifier,kind,at in [('one','start',0),('two','start',1),
                                         ('one','end',2),('two','end',3)]]
    assert provider_activity(events) == {'launches':2,'complete':True,'peak':2}
    missing = provider_activity(events[:-1])
    assert missing['launches'] == 2 and missing['complete'] is False and missing['peak'] is None
