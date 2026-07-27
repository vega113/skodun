from skodun.cli import main


def test_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip().startswith("skodun ")


def test_oracle_dir_none_when_unset(monkeypatch):
    from tests.conftest import oracle_dir

    monkeypatch.delenv("SKODUN_ORACLE_DIR", raising=False)
    assert oracle_dir() is None
