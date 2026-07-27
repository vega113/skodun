import pytest

from skodun.cli import main


def test_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip().startswith("skodun ")


def test_no_subcommand_is_not_a_silent_success(capsys):
    """`skodun` with nothing after it ran nothing, so it must not report 0.

    This CLI's exit code is consumed by a pre-push hook, which cannot tell a
    "nothing to do" 0 from a "reviewed and clean" 0. 2 is the right value: the
    contract's "no trustworthy review covers this content" is exactly true of
    an invocation that never looked.
    """
    assert main([]) == 2
    assert "usage:" in capsys.readouterr().err


def test_unknown_subcommand_is_not_a_silent_success(capsys):
    assert main(["definitely-not-a-command"]) == 2
    assert "usage:" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--help"], ["gate", "--help"]])
def test_help_still_exits_0(argv, capsys):
    """--version and --help are the two invocations that legitimately exit 0
    without gating anything; the required-subcommand rule must not catch them."""
    assert main(argv) == 0
    assert "usage:" in capsys.readouterr().out


def test_oracle_dir_none_when_unset(monkeypatch):
    from tests.conftest import oracle_dir

    monkeypatch.delenv("SKODUN_ORACLE_DIR", raising=False)
    assert oracle_dir() is None
