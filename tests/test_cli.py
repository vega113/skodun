import pytest

from skodun.cli import main

#: The prefix `trust.banner_failure` renders for every path that refused
#: something before a record existed.
BANNER = "SKODUN VERDICT: trustworthy=false reason="


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


@pytest.mark.parametrize("argv", [[], ["definitely-not-a-command"],
                                  ["review", "--no-such-flag"]])
def test_a_usage_error_still_ends_with_a_verdict_banner(argv, capsys):
    """The invariant is that the LAST line of stdout is always a verdict.

    argparse writes its usage message to stderr and exits, so these paths used
    to leave stdout completely silent — a consumer reading stdout for the
    verdict saw nothing at all where a refusal belonged.
    """
    assert main(argv) == 2
    cap = capsys.readouterr()
    assert "usage:" in cap.err
    lines = cap.out.strip().splitlines()
    assert lines, "a usage error printed nothing to stdout"
    assert lines[-1] == BANNER + "usage error; no review ran"


@pytest.mark.parametrize("argv", [["--help"], ["gate", "--help"]])
def test_help_still_exits_0(argv, capsys):
    """--version and --help are the two invocations that legitimately exit 0
    without gating anything; the required-subcommand rule must not catch them."""
    assert main(argv) == 0
    assert "usage:" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--version"], ["--help"], ["gate", "--help"]])
def test_the_zero_exit_paths_carry_no_banner(argv, capsys):
    """The banner rule is about paths that refuse something. `--version` and
    `--help` gated nothing and their stdout is meant to be read verbatim."""
    assert main(argv) == 0
    assert "SKODUN VERDICT" not in capsys.readouterr().out


def test_an_import_failure_in_the_review_seam_still_banners(capsys, monkeypatch):
    """`_cmd_review`'s imports used to sit outside its try block, so a broken
    install reported on stderr and left stdout without a verdict."""
    from skodun import pipeline

    monkeypatch.delattr(pipeline, "run_review")
    assert main(["review", "--repo", "."]) == 2
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines and lines[-1].startswith(BANNER)
    assert "no review ran" in lines[-1]


def test_oracle_dir_none_when_unset(monkeypatch):
    from tests.conftest import oracle_dir

    monkeypatch.delenv("SKODUN_ORACLE_DIR", raising=False)
    assert oracle_dir() is None
