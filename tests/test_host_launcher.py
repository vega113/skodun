"""Exercise the optional host launcher as a real subprocess, without providers."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / 'examples/bin/skodun-host'


def invoke(tmp_path, body=None, args=()):
    profile = tmp_path / 'host-profile.sh'
    if body is not None:
        profile.write_text(body)
    env = {**os.environ, 'SKODUN_HOST_PROFILE': str(profile)}
    return subprocess.run([str(LAUNCHER), *args], env=env,
                          text=True, capture_output=True, timeout=5)


def test_launcher_forwards_profile_arguments_and_exit(tmp_path):
    target = tmp_path / 'installed skodun'
    target.write_text(f'#!{sys.executable}\n' + '''import json, os, sys
print(json.dumps([sys.argv[1:], os.environ['SKODUN_REVIEW_FG_CAPACITY'],
                  os.environ['SKODUN_LEGACY_FG_LOCK']]))
sys.exit(3)
''')
    target.chmod(0o700)
    result = invoke(tmp_path, f"SKODUN_REAL_BIN='{target}'\n"
                    'export SKODUN_REVIEW_FG_CAPACITY=2\n'
                    'export SKODUN_LEGACY_FG_LOCK=0\n',
                    ('review', '--repo', '/a path/with spaces', '$(literal)'))
    assert result.returncode == 3
    assert json.loads(result.stdout) == [
        ['review', '--repo', '/a path/with spaces', '$(literal)'], '2', '0']
    assert result.stderr == ''


@pytest.mark.parametrize('body, message', [
    (None, 'missing host profile'),
    ('', 'set SKODUN_REAL_BIN'),
    ('SKODUN_REAL_BIN=missing\n', 'set SKODUN_REAL_BIN'),
    (f"SKODUN_REAL_BIN='{LAUNCHER}'\n", 'points back'),
    ('exit 7\n', None),
])
def test_launcher_refuses_invalid_setup(tmp_path, body, message):
    result = invoke(tmp_path, body)
    assert result.returncode == (7 if message is None else 2)
    assert result.stdout == ''
    if message:
        assert message in result.stderr


def test_launcher_refuses_symlink_recursion(tmp_path):
    alias = tmp_path / 'skodun'
    alias.symlink_to(LAUNCHER)
    result = invoke(tmp_path, f"SKODUN_REAL_BIN='{alias}'\n")
    assert result.returncode == 2
    assert 'points back' in result.stderr
