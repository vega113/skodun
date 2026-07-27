# skodun Phase 1 Implementation Plan — Core + Grok Adapter, Shadow Mode

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python CLI (`skodun`) that runs a full foreground code review through the grok CLI with the tubescribes pipeline semantics ported (diff identity, dedup, checklists, context packing, security/skeptic passes, trust model, gate 0/1/2, triage ledger), persisting to SQLite, runnable in shadow mode beside the existing tubescribes scripts to compare verdicts.

**Architecture:** A single Python package with a pipeline orchestrator, one provider adapter (grok, subprocess-based), and a SQLite (WAL) store. All fail-closed invariants from tubescribes are preserved verbatim; the porting oracle is the tubescribes source tree, referenced per-task by exact path. Shadow mode reuses the legacy foreground lock so both systems never hit the inference backend concurrently.

**Tech Stack:** Python ≥ 3.12, stdlib only at runtime (`sqlite3`, `tomllib`, `subprocess`, `hashlib`, `json`, `argparse`), `pytest` as the sole dev dependency.

## Global Constraints

- Python ≥ 3.12. Runtime deps: stdlib only. Dev deps: pytest only. (MCP SDK, scheduling, other adapters are later phases.)
- **Trust invariant, verbatim:** `trustworthy = parse_ok and not degraded and not diff_truncated`. Nothing may suppress a re-review or pass the gate unless `trustworthy` is true.
- **Gate exit contract, verbatim:** `0` = clean or every finding triaged; `1` = findings remain open; `2` = no trustworthy review covers this exact content. Every unexpected exception in the gate maps to exit `2`, never `1` (corruption must not read as findings).
- **Diff identity (legacy-compatible):** diff bytes captured with `git --no-pager diff --no-ext-diff --no-textconv`; `diff_hash` = git blob SHA-1 (`sha1("blob <len>\0" + bytes)`) over the captured bytes **with all trailing newlines stripped** — the oracle stores the diff in a shell variable, and command substitution strips trailing newlines before hashing, so hashing raw bytes produces a different hash than the legacy archive. Verified in tests against both `git hash-object --stdin` and the oracle seam `sh scripts/grok-prepush-review.sh --diff-hash` (skipped when the tubescribes checkout is absent).
- **Grok binary resolution (oracle parity):** `SKODUN_GROK_BIN` env → `~/.grok/bin/grok` if executable → `grok` on PATH. The legacy `-p` ARG_MAX fallback for stale CLIs is an **intentional deviation** — skodun targets current grok CLIs and always uses `--prompt-file`; an empty-stdout run is a failed attempt, never re-shelled.
- **Triage keys, verbatim:** `finding_key = sha256(norm(file) + "\0" + norm(title)).hexdigest()[:16]` (line number deliberately excluded); ledger key = `norm(branch) + "\0" + norm(base_sha) + "\0" + finding_key`. `norm` is ported from `grok_review_triage.py` — one definition, imported everywhere, never re-derived.
- **Retries are always fresh sessions** — never resume a grok session for a retry.
- Every text file read/written passes `encoding="utf-8"` explicitly (C-locale hooks default to ASCII).
- All prompts/diffs travel via **files**, never shell-interpolated strings.
- Model selection is **explicit** (`-m` from config) — never rely on `.grok/settings.json`.
- Grok reviewer runs tool-less: `--disallowed-tools bash,read,write,edit,web_search,web_fetch`, `--max-turns 40`, no `--always-approve` needed.
- Verdict banner is always the **last line of stdout**, values read back from the persisted record, never recomputed.
- Store path: `~/.local/share/skodun/skodun.db` (override: `SKODUN_DB`). Config: `~/.config/skodun/config.toml` overridden by `<repo>/.skodun.toml` (deep merge, project wins).
- Porting oracle: the tubescribes checkout — each task cites its source file under its `scripts/` directory. Where this plan's code and the oracle's observable behavior disagree, the oracle wins; add a parity test. **The repo is public/open-source — committed code is generic:** no machine-specific paths, no owner-specific defaults, no tubescribes-specific behavior outside clearly-labeled parity tests. Test code locates the oracle solely via the `SKODUN_ORACLE_DIR` env var (no fallback default) and skips parity tests when unset. Shared test helper: `tests/conftest.py` defines `oracle_dir() -> Path | None` returning `Path(os.environ["SKODUN_ORACLE_DIR"])` when set and existing, else `None`. (Docs may reference the owner's local context; code may not.)
- Out of scope for Phase 1 (fail closed where relevant): batched review of oversized diffs (diff over budget ⇒ `diff_truncated=true` ⇒ not trustworthy ⇒ gate exit 2 — never a silent pass); pre-push dispatcher **and with it the dedup probe** (foreground `--now` never dedups in the oracle — every `skodun review` runs a fresh review; the 3-way diff/context dedup probe is dispatcher machinery, Phase 3 — the store nonetheless preserves the `context_hash` NULL-vs-`""` distinction for forward compatibility); same-branch supersede (legacy retires only *prepush*-mode workers, which don't exist in Phase 1); rules-registry authoring/generation/sync (`generate-review-rules.mjs` + `check-review-rules-sync.sh` stay in tubescribes; skodun only *consumes* the generated `docs/review/checklists/*.md` + `code-rules.json`); MCP server; scheduling; non-grok adapters; cloud-bot embed generation; SessionStart delivery hook; macOS notifications; retention/pruning; the legacy `-p` prompt fallback (see Grok binary resolution above).

## File Structure

```
skodun/
├── pyproject.toml
├── src/skodun/
│   ├── __init__.py          # __version__
│   ├── cli.py               # argparse dispatch: review, gate, triage, log, import-legacy, shadow-compare
│   ├── config.py            # layered TOML config → Config dataclasses
│   ├── store.py             # SQLite WAL schema + DAO functions
│   ├── gitio.py             # base resolution, diff capture, diff_hash, untracked handling
│   ├── textnorm.py          # norm(), finding_key(), ledger_key()  (single definition)
│   ├── triage.py            # reason validation, ledger ops, artifact validation, gate decision
│   ├── checklist.py         # per-change checklist selection (port)
│   ├── contextpack.py       # changed-file context packing (port)
│   ├── promptbuild.py       # review prompt assembly
│   ├── runner.py            # watchdog subprocess execution (process-group kill)
│   ├── trust.py             # trust invariant + verdict banner
│   ├── passes.py            # security/skeptic pass decisions + merge/demotion
│   ├── pipeline.py          # orchestrates one review run + fg lock
│   ├── shadow.py            # verdict comparison vs legacy .grok-reviews
│   ├── legacy_import.py     # import legacy triage.jsonl + index.jsonl
│   └── adapters/
│       ├── __init__.py      # Adapter protocol + registry
│       └── grok.py          # grok CLI adapter: cmd build, envelope parse, degraded detection
└── tests/                   # mirrors src/ (test_config.py, test_store.py, ...)
```

Porting source map (tubescribes → skodun):

| Source (scripts/) | Target | Notes |
|---|---|---|
| `grok_review_triage.py` | `textnorm.py`, `triage.py` | keys, reason validation, artifact validation, gate |
| `grok-checklist-select.py` | `checklist.py` | prefix mapping, 18 KiB budget, drop order |
| `grok-context-pack.py` | `contextpack.py` | selection, budgets, symlink/traversal hardening |
| `grok-extra-passes.py` | `passes.py` | should-run logic, prompts, merge/demotion |
| `grok-prepush-review.sh` (`write_prompt`, `run_grok_with_timeout`, `detect_degraded`, envelope parse, banner) | `promptbuild.py`, `runner.py`, `adapters/grok.py`, `trust.py` | bash → Python rewrite |
| `grok-review-now.sh` (fg lock) | `pipeline.py` | same lock path + protocol for shadow coexistence |

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `src/skodun/__init__.py`, `src/skodun/cli.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: installable package `skodun` with console script `skodun`; `skodun.cli.main(argv) -> int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
from skodun.cli import main

def test_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip().startswith("skodun ")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli.py -v` — Expected: FAIL (ModuleNotFoundError).

- [ ] **Step 3: Write minimal implementation**

```toml
# pyproject.toml
[project]
name = "skodun"
version = "0.1.0"
requires-python = ">=3.12"
[project.scripts]
skodun = "skodun.cli:entry"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
[tool.pytest.ini_options]
pythonpath = ["src"]
```

```python
# src/skodun/__init__.py
__version__ = "0.1.0"
```

```python
# src/skodun/cli.py
import argparse
from . import __version__

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skodun")
    p.add_argument("--version", action="version", version=f"skodun {__version__}")
    p.add_subparsers(dest="command")
    return p

def main(argv: list[str] | None = None) -> int:
    try:
        build_parser().parse_args(argv)
    except SystemExit as e:   # argparse --version exits 0
        return int(e.code or 0)
    return 0

def entry() -> None:
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes** — `python -m pytest tests/test_cli.py -v` → PASS
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: scaffold skodun package and CLI entrypoint"`

---

### Task 2: Layered TOML config

**Files:**
- Create: `src/skodun/config.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `load_config(repo_root: Path | None, global_path: Path | None = None) -> Config`;
  `Config(defaults: Defaults, reviewers: tuple[Reviewer, ...])`;
  `Reviewer(name, provider, model, effort, role, dimensions, persona, max_cost_usd, enabled)`;
  `Defaults(severity_gate="high", confidence_threshold=7, max_diff_bytes=400_000, timeout_sec=420, timeout_retries=1, degraded_retries=1, max_turns=40, deny_tools="bash,read,write,edit,web_search,web_fetch", context_pack=True, checklist_dir="docs/review/checklists", rules_json="docs/review/code-rules.json")`.
- Effort is a canonical enum: `none|low|medium|high|max` or `None` (unset).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
from pathlib import Path
from skodun.config import load_config

def _write(p: Path, s: str) -> Path:
    p.write_text(s, encoding="utf-8"); return p

def test_project_overrides_global_and_merges_reviewers_by_name(tmp_path):
    g = _write(tmp_path / "global.toml", """
[defaults]
timeout_sec = 420
[[reviewers]]
name = "finder"
provider = "xai"
model = "grok-4.20-0309-reasoning"
role = "finder"
""")
    repo = tmp_path / "repo"; repo.mkdir()
    _write(repo / ".skodun.toml", """
[defaults]
timeout_sec = 240
[[reviewers]]
name = "finder"
effort = "high"
""")
    cfg = load_config(repo, global_path=g)
    assert cfg.defaults.timeout_sec == 240
    assert cfg.defaults.max_turns == 40          # untouched default survives
    f = cfg.reviewer("finder")
    assert f.model == "grok-4.20-0309-reasoning"  # inherited from global entry
    assert f.effort == "high"                     # overridden by project entry

def test_unknown_effort_rejected(tmp_path):
    g = _write(tmp_path / "g.toml", """
[[reviewers]]
name = "x"
provider = "xai"
model = "m"
role = "finder"
effort = "turbo"
""")
    import pytest
    with pytest.raises(ValueError, match="effort"):
        load_config(None, global_path=g)
```

- [ ] **Step 2: Run to verify FAIL** — `python -m pytest tests/test_config.py -v`
- [ ] **Step 3: Implementation**

```python
# src/skodun/config.py
from __future__ import annotations
import os, tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

EFFORTS = {"none", "low", "medium", "high", "max"}
ROLES = {"finder", "refuter", "security", "triager", "integrator"}

@dataclass(frozen=True)
class Defaults:
    severity_gate: str = "high"
    confidence_threshold: int = 7
    max_diff_bytes: int = 400_000
    timeout_sec: int = 420
    timeout_retries: int = 1
    degraded_retries: int = 1
    max_turns: int = 40
    deny_tools: str = "bash,read,write,edit,web_search,web_fetch"
    context_pack: bool = True
    checklist_dir: str = "docs/review/checklists"
    rules_json: str = "docs/review/code-rules.json"
    untracked_max: int = 100

@dataclass(frozen=True)
class Reviewer:
    name: str
    provider: str = ""
    model: str = ""
    role: str = "finder"
    effort: str | None = None
    dimensions: tuple[str, ...] = ()
    persona: str | None = None
    max_cost_usd: float | None = None
    enabled: bool = True

@dataclass(frozen=True)
class Config:
    defaults: Defaults
    reviewers: tuple[Reviewer, ...]
    def reviewer(self, name: str) -> Reviewer:
        for r in self.reviewers:
            if r.name == name:
                return r
        raise KeyError(name)

def _read(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)

def _validate(r: Reviewer) -> Reviewer:
    if r.effort is not None and r.effort not in EFFORTS:
        raise ValueError(f"reviewer {r.name!r}: unknown effort {r.effort!r}")
    if r.role not in ROLES:
        raise ValueError(f"reviewer {r.name!r}: unknown role {r.role!r}")
    if not r.provider or not r.model:
        raise ValueError(f"reviewer {r.name!r}: provider and model are required")
    return r

def load_config(repo_root: Path | None, global_path: Path | None = None) -> Config:
    if global_path is None:
        global_path = Path(os.environ.get(
            "SKODUN_CONFIG", Path.home() / ".config" / "skodun" / "config.toml"))
    layers = [_read(global_path)]
    if repo_root is not None:
        layers.append(_read(Path(repo_root) / ".skodun.toml"))

    dvals: dict = {}
    rmap: dict[str, dict] = {}
    order: list[str] = []
    for layer in layers:
        dvals.update(layer.get("defaults", {}))
        for entry in layer.get("reviewers", []):
            name = entry["name"]
            if name not in rmap:
                rmap[name] = {}; order.append(name)
            rmap[name].update(entry)   # later layer wins per-key, merged by name

    known = {f.name for f in fields(Defaults)}
    unknown = set(dvals) - known
    if unknown:
        raise ValueError(f"unknown [defaults] keys: {sorted(unknown)}")
    rknown = {f.name for f in fields(Reviewer)}
    reviewers = []
    for name in order:
        e = dict(rmap[name])
        bad = set(e) - rknown
        if bad:
            raise ValueError(f"reviewer {name!r}: unknown keys {sorted(bad)}")
        if "dimensions" in e:
            e["dimensions"] = tuple(e["dimensions"])
        reviewers.append(_validate(Reviewer(**e)))
    return Config(defaults=Defaults(**dvals), reviewers=tuple(reviewers))
```

- [ ] **Step 4: Run to verify PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat: layered TOML config with reviewer merge by name"`

---

### Task 3: SQLite store + trust invariant

**Files:**
- Create: `src/skodun/store.py`, `src/skodun/trust.py` (invariant only — the banner is added in Task 13), `tests/test_store.py`

**Interfaces:**
- Produces: `trust.is_trustworthy(parse_ok: bool, degraded: bool, diff_truncated: bool) -> bool` — the single definition of the invariant, imported everywhere, never re-derived;
  `Store.open(path: Path) -> Store` (WAL, foreign keys on, schema migrated);
  `save_review(record: dict) -> None` — full artifact under `artifact_json`, indexed columns extracted. **`trustworthy` is COMPUTED by the store** via `trust.is_trustworthy(...)` from the record's `parse_ok`/`degraded`/`diff_truncated`; a caller-supplied `trustworthy` value is overwritten in both the column and the stored artifact JSON (an inconsistent index row must be impossible by construction). **The three trust axes must be actual `bool` instances** — `save_review` raises `ValueError` on any other type (`bool("false")` is `True`; a string-typed axis from a hand-edited or mis-mapped record must never coerce its way to trustworthy). The upsert's `ON CONFLICT` clause updates **every** indexed column, identity included (`diff_hash, branch, head, base_ref, base_sha, context_hash, reviewed_at, mode, model, adapter`) — index and artifact move in lockstep or not at all;
  `get_review(review_id) -> dict | None`; `latest_trustworthy_for(diff_hash: str) -> dict | None`;
  `add_triage(rec: dict) -> None`; `triage_for(branch, base_sha) -> dict[str, dict]` (keyed by `finding_key`);
  `list_reviews(branch: str | None, limit: int) -> list[dict]`;
  `set_status(review_id, status)` — updates the indexed column **and** the `status` field inside `artifact_json` in one UPDATE (`json_set`), so viewers reading artifacts never see a stale `running`;
  `log_gate_event(rec: dict) -> None` + table `gate_events(at, repo, branch, diff_hash, outcome, code, note)` — every gate decision (pass/fail/skipped) is a durable record.
- Review record statuses: `running | clean | degraded | failed | superseded` (legacy vocabulary).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py
from skodun.store import Store

REC = dict(id="r1", reviewed_at="2026-07-27T10:00:00Z", branch="b", head="h"*20,
           base_ref="origin/main", base_sha="s"*40, diff_hash="d"*40, context_hash="",
           mode="now", model="grok-4.20-0309-reasoning", adapter="grok", status="clean",
           parse_ok=True, degraded=False, diff_truncated=False, trustworthy=True,
           stop_reason="EndTurn", summary="ok", findings_total=0,
           severity={"high": 0, "medium": 0, "low": 0}, findings=[])

def test_roundtrip_and_dedup_query(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)
    assert st.get_review("r1")["summary"] == "ok"
    assert st.latest_trustworthy_for("d" * 40)["id"] == "r1"

def test_untrustworthy_never_matches_dedup(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review({**REC, "id": "r2", "diff_hash": "e"*40,
                    "degraded": True, "trustworthy": False, "status": "degraded"})
    assert st.latest_trustworthy_for("e" * 40) is None

def test_save_is_upsert(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)
    st.save_review({**REC, "parse_ok": False, "status": "failed"})
    assert st.latest_trustworthy_for("d" * 40) is None   # demotion visible

def test_trust_is_computed_never_caller_supplied(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review({**REC, "degraded": True, "trustworthy": True})  # liar caller
    assert st.latest_trustworthy_for("d" * 40) is None
    assert st.get_review("r1")["trustworthy"] is False   # artifact rewritten too

def test_non_bool_trust_axis_rejected(tmp_path):
    import pytest
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(ValueError):                       # bool("false") is True
        st.save_review({**REC, "parse_ok": "false"})

def test_set_status_updates_artifact_json_too(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review({**REC, "status": "running"})
    st.set_status("r1", "failed")
    assert st.get_review("r1")["status"] == "failed"
```

- [ ] **Step 2: Run to verify FAIL**
- [ ] **Step 3: Implementation**

```python
# src/skodun/store.py
from __future__ import annotations
import json, sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
  id TEXT PRIMARY KEY, reviewed_at TEXT, branch TEXT, head TEXT,
  base_ref TEXT, base_sha TEXT, diff_hash TEXT, context_hash TEXT,
  mode TEXT, model TEXT, adapter TEXT, status TEXT,
  parse_ok INTEGER, degraded INTEGER, diff_truncated INTEGER, trustworthy INTEGER,
  stop_reason TEXT, findings_total INTEGER, sev_high INTEGER, sev_medium INTEGER,
  sev_low INTEGER, summary TEXT, source TEXT DEFAULT 'skodun', artifact_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_reviews_diff ON reviews(diff_hash, trustworthy);
CREATE INDEX IF NOT EXISTS ix_reviews_branch ON reviews(branch, reviewed_at);
CREATE TABLE IF NOT EXISTS triage (
  ledger_key TEXT PRIMARY KEY, finding_key TEXT, review_id TEXT, branch TEXT,
  base_sha TEXT, file TEXT, line INTEGER, severity TEXT, title TEXT,
  dismissed_reason TEXT, dismissed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_triage_scope ON triage(branch, base_sha);
CREATE TABLE IF NOT EXISTS gate_events (
  at TEXT, repo TEXT, branch TEXT, diff_hash TEXT, outcome TEXT,
  code INTEGER, note TEXT
);
"""

class Store:
    def __init__(self, conn: sqlite3.Connection):
        self._c = conn

    @classmethod
    def open(cls, path: Path) -> "Store":
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        return cls(conn)

    def save_review(self, rec: dict) -> None:
        from .trust import is_trustworthy
        rec = dict(rec)   # never mutate the caller's dict
        axes = {k: rec.get(k, False)
                for k in ("parse_ok", "degraded", "diff_truncated")}
        for k, v in axes.items():
            if not isinstance(v, bool):   # bool("false") is True — refuse coercion
                raise ValueError(f"save_review: {k} must be bool, got {type(v).__name__}")
        rec.update(axes)
        rec["trustworthy"] = is_trustworthy(**axes)
        sev = rec.get("severity") or {}
        self._c.execute(
            """INSERT INTO reviews (id, reviewed_at, branch, head, base_ref, base_sha,
                 diff_hash, context_hash, mode, model, adapter, status, parse_ok,
                 degraded, diff_truncated, trustworthy, stop_reason, findings_total,
                 sev_high, sev_medium, sev_low, summary, source, artifact_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 reviewed_at=excluded.reviewed_at, branch=excluded.branch,
                 head=excluded.head, base_ref=excluded.base_ref,
                 base_sha=excluded.base_sha, diff_hash=excluded.diff_hash,
                 context_hash=excluded.context_hash, mode=excluded.mode,
                 model=excluded.model, adapter=excluded.adapter,
                 status=excluded.status, parse_ok=excluded.parse_ok,
                 degraded=excluded.degraded, diff_truncated=excluded.diff_truncated,
                 trustworthy=excluded.trustworthy, stop_reason=excluded.stop_reason,
                 findings_total=excluded.findings_total, sev_high=excluded.sev_high,
                 sev_medium=excluded.sev_medium, sev_low=excluded.sev_low,
                 summary=excluded.summary, source=excluded.source,
                 artifact_json=excluded.artifact_json""",
            (rec["id"], rec.get("reviewed_at"), rec.get("branch"), rec.get("head"),
             rec.get("base_ref"), rec.get("base_sha"), rec.get("diff_hash"),
             rec.get("context_hash", ""), rec.get("mode"), rec.get("model"),
             rec.get("adapter"), rec.get("status"), int(bool(rec.get("parse_ok"))),
             int(bool(rec.get("degraded"))), int(bool(rec.get("diff_truncated"))),
             int(bool(rec.get("trustworthy"))), rec.get("stop_reason"),
             int(rec.get("findings_total") or 0), int(sev.get("high") or 0),
             int(sev.get("medium") or 0), int(sev.get("low") or 0),
             rec.get("summary"), rec.get("source", "skodun"),
             json.dumps(rec, ensure_ascii=False)))

    def get_review(self, review_id: str) -> dict | None:
        row = self._c.execute("SELECT artifact_json FROM reviews WHERE id=?",
                              (review_id,)).fetchone()
        return json.loads(row["artifact_json"]) if row else None

    def latest_trustworthy_for(self, diff_hash: str) -> dict | None:
        row = self._c.execute(
            """SELECT artifact_json FROM reviews
               WHERE diff_hash=? AND trustworthy=1
               ORDER BY reviewed_at DESC LIMIT 1""", (diff_hash,)).fetchone()
        return json.loads(row["artifact_json"]) if row else None

    def set_status(self, review_id: str, status: str) -> None:
        self._c.execute(
            """UPDATE reviews SET status=?,
                 artifact_json=json_set(artifact_json, '$.status', ?)
               WHERE id=?""", (status, status, review_id))

    def log_gate_event(self, rec: dict) -> None:
        self._c.execute(
            "INSERT INTO gate_events (at, repo, branch, diff_hash, outcome, code, note)"
            " VALUES (?,?,?,?,?,?,?)",
            (rec.get("at"), rec.get("repo"), rec.get("branch"), rec.get("diff_hash"),
             rec.get("outcome"), rec.get("code"), rec.get("note")))

    def add_triage(self, rec: dict) -> None:
        self._c.execute(
            """INSERT OR REPLACE INTO triage (ledger_key, finding_key, review_id, branch,
                 base_sha, file, line, severity, title, dismissed_reason, dismissed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (rec["ledger_key"], rec["finding_key"], rec.get("id"), rec["branch"],
             rec["base_sha"], rec.get("file"), rec.get("line"), rec.get("severity"),
             rec.get("title"), rec["dismissed_reason"], rec.get("dismissed_at")))

    def triage_for(self, branch: str, base_sha: str) -> dict[str, dict]:
        rows = self._c.execute("SELECT * FROM triage WHERE branch=? AND base_sha=?",
                               (branch, base_sha)).fetchall()
        return {r["finding_key"]: dict(r) for r in rows}

    def list_reviews(self, branch: str | None, limit: int = 30) -> list[dict]:
        q = "SELECT artifact_json FROM reviews"
        args: tuple = ()
        if branch:
            q += " WHERE branch=?"; args = (branch,)
        q += " ORDER BY reviewed_at DESC LIMIT ?"
        rows = self._c.execute(q, args + (limit,)).fetchall()
        return [json.loads(r["artifact_json"]) for r in rows]
```

Plus the invariant module this task also creates:

```python
# src/skodun/trust.py
def is_trustworthy(parse_ok: bool, degraded: bool, diff_truncated: bool) -> bool:
    return bool(parse_ok) and not degraded and not diff_truncated
```

- [ ] **Step 4: Run to verify PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat: SQLite WAL store with computed trust and gate-event log"`

---

### Task 4: Git IO — base resolution, diff capture, diff_hash

**Files:**
- Create: `src/skodun/gitio.py`, `tests/test_gitio.py`

**Interfaces:**
- Produces: `resolve_base(repo: Path) -> Base(ref: str, sha: str, warning: str | None)` — candidate order `github/main` → `origin/main` → `main`; a candidate whose `merge-base <ref> HEAD` fails (unrelated histories) is skipped with the next tried; if none resolves, fall back to `HEAD^`, and if `HEAD^` doesn't exist (single-commit repo) to `HEAD` — both fallbacks carry `warning`.
  `capture_diff(repo: Path, base_sha: str, untracked_max: int) -> Diff(data: bytes, files: list[str], statuses: dict[str, str], truncated_untracked: bool)` — working tree vs base incl. untracked via `git diff --no-index -- /dev/null <f>`, capped; `statuses` maps path → one-letter status (`A`/`M`/`D`/`R`…), with untracked files entered as `A` — Task 9's context packer consumes it to classify `added`/`deleted`/`already-in-diff`. **Path parsing is NUL-delimited**: file lists come from `git diff --name-status -z <base_sha>` and `git ls-files --others --exclude-standard -z`, split on `\0` with no whitespace stripping — text-mode parsing plus `.strip()` corrupts filenames with non-ASCII characters (git quotes them as `"\303\244.txt"` under default `core.quotepath`) or meaningful leading/trailing spaces, and the context packer would then open the wrong path. **Concatenation parity:** the oracle joins the tracked diff and untracked `--no-index` sections with a `\n` separator (an untracked-only change yields `"\n" + udiff` after the empty tracked capture) — reproduce its exact concatenation; the `--diff-hash` parity tests below must include an untracked-only case.
  `blob_sha1(data: bytes) -> str` (git hash-object equivalent over raw bytes);
  `diff_identity(data: bytes) -> str` = `blob_sha1(data.rstrip(b"\n"))` — **the** diff-hash function. The oracle round-trips the diff through `$(...)` command substitution, which strips all trailing newlines before hashing; hashing raw captured bytes yields a hash the legacy archive has never seen, breaking legacy import joins and shadow-compare. All skodun code hashes via `diff_identity`, never `blob_sha1` directly.
  `git_common_dir(repo: Path) -> Path`; `current_branch(repo) -> str`; `head_sha(repo) -> str`;
  `is_primary_checkout(repo: Path) -> bool` — true iff resolved `--git-dir` == resolved `--git-common-dir` (they differ exactly for linked worktrees; substring tests on the path misclassify repos that merely contain a `worktrees` directory in their path).
- Oracle: `grok-prepush-review.sh` `resolve_outgoing_change` (lines 1694–1746) and the `--diff-hash` seam. `--no-ext-diff --no-textconv` mandatory.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gitio.py
import subprocess
from pathlib import Path
from skodun.gitio import blob_sha1, resolve_base, capture_diff, is_primary_checkout

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()

def _mkrepo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"; repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "c0")
    return repo

def test_blob_sha1_matches_git(tmp_path):
    data = b"hello \xff diff bytes\n"
    p = tmp_path / "x"; p.write_bytes(data)
    expected = subprocess.run(["git", "hash-object", "--stdin"], input=data,
                              capture_output=True, check=True).stdout.decode().strip()
    assert blob_sha1(data) == expected

def test_diff_includes_untracked_and_is_stable(tmp_path):
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")   # modified
    (repo / "new.txt").write_text("brand new\n", encoding="utf-8")  # untracked
    base = resolve_base(repo)
    d1 = capture_diff(repo, base.sha, untracked_max=100)
    d2 = capture_diff(repo, base.sha, untracked_max=100)
    assert d1.data == d2.data                     # deterministic bytes
    assert "new.txt" in d1.files and "a.txt" in d1.files
    assert b"brand new" in d1.data

def test_base_falls_back_with_warning(tmp_path):
    repo = _mkrepo(tmp_path)
    (repo / "b.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-m", "c1")
    # no origin/github remotes; 'main' is current branch => merge-base(main, HEAD)=HEAD
    # simulate detached work on a branch without main: delete main pointer
    _git(repo, "checkout", "-b", "feat"); _git(repo, "branch", "-D", "main")
    base = resolve_base(repo)
    assert base.ref == "HEAD^" and base.warning is not None

def test_base_single_commit_repo_falls_back_to_head(tmp_path):
    repo = _mkrepo(tmp_path)          # exactly one commit; HEAD^ does not exist
    _git(repo, "checkout", "-b", "feat"); _git(repo, "branch", "-D", "main")
    base = resolve_base(repo)
    assert base.ref == "HEAD" and base.warning is not None

def test_diff_identity_strips_trailing_newlines_like_shell():
    from skodun.gitio import diff_identity
    assert diff_identity(b"diff --git a b\n+x\n\n\n") == diff_identity(b"diff --git a b\n+x")

from tests.conftest import oracle_dir
ORACLE = (oracle_dir() / "scripts" / "grok-prepush-review.sh") if oracle_dir() else None

def _oracle_hash(repo: Path) -> str:
    import subprocess as sp
    return sp.run(["sh", str(ORACLE), "--diff-hash"], cwd=repo,
                  capture_output=True, text=True).stdout.strip().splitlines()[-1]

def test_diff_identity_parity_with_oracle(tmp_path):
    import pytest
    if ORACLE is None or not ORACLE.exists():
        pytest.skip("oracle checkout not present (set SKODUN_ORACLE_DIR)")
    from skodun.gitio import resolve_base, capture_diff, diff_identity
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")           # tracked edit
    base = resolve_base(repo)
    assert diff_identity(capture_diff(repo, base.sha, 100).data) == _oracle_hash(repo)
    (repo / "brand-new.txt").write_text("nu\n", encoding="utf-8")    # + untracked
    assert diff_identity(capture_diff(repo, base.sha, 100).data) == _oracle_hash(repo)

def test_diff_identity_parity_untracked_only(tmp_path):
    import pytest
    if ORACLE is None or not ORACLE.exists():
        pytest.skip("oracle checkout not present (set SKODUN_ORACLE_DIR)")
    from skodun.gitio import resolve_base, capture_diff, diff_identity
    repo = _mkrepo(tmp_path)
    _git(repo, "checkout", "-b", "feat")
    (repo / "only-new.txt").write_text("nu\n", encoding="utf-8")     # untracked ONLY —
    base = resolve_base(repo)                    # oracle output starts "\n" + udiff
    assert diff_identity(capture_diff(repo, base.sha, 100).data) == _oracle_hash(repo)

def test_primary_checkout_detection(tmp_path):
    repo = _mkrepo(tmp_path)
    assert is_primary_checkout(repo) is True
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", str(wt), "-b", "w1")
    assert is_primary_checkout(wt) is False
```

- [ ] **Step 2: Run to verify FAIL**
- [ ] **Step 3: Implementation**

```python
# src/skodun/gitio.py
from __future__ import annotations
import hashlib, subprocess
from dataclasses import dataclass
from pathlib import Path

class GitError(RuntimeError): ...

def _run(repo: Path, *args: str, ok_codes=(0,)) -> subprocess.CompletedProcess:
    cp = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if cp.returncode not in ok_codes:
        raise GitError(f"git {' '.join(args)}: rc={cp.returncode} "
                       f"{cp.stderr.decode('utf-8', 'replace').strip()}")
    return cp

def _out(repo: Path, *args: str) -> str:
    return _run(repo, *args).stdout.decode("utf-8", "replace").strip()

def blob_sha1(data: bytes) -> str:
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data)); h.update(data)
    return h.hexdigest()

def diff_identity(data: bytes) -> str:
    # PARITY-CRITICAL: the oracle hashes $(git diff ...) after command
    # substitution, which strips ALL trailing newlines. Hash the same bytes.
    return blob_sha1(data.rstrip(b"\n"))

@dataclass(frozen=True)
class Base:
    ref: str
    sha: str
    warning: str | None = None

def resolve_base(repo: Path) -> Base:
    for ref in ("github/main", "origin/main", "main"):
        cp = _run(repo, "rev-parse", "--verify", "--quiet", ref, ok_codes=(0, 1))
        if cp.returncode != 0:
            continue
        mb = _run(repo, "merge-base", ref, "HEAD", ok_codes=(0, 1, 128))
        if mb.returncode == 0:
            return Base(ref=ref, sha=mb.stdout.decode("utf-8").strip())
        # ref exists but shares no history — try the next candidate
    cp = _run(repo, "rev-parse", "--verify", "--quiet", "HEAD^", ok_codes=(0, 1, 128))
    if cp.returncode == 0:
        return Base(ref="HEAD^", sha=cp.stdout.decode("utf-8").strip(),
                    warning="no main ref found; falling back to HEAD^ — a "
                            "multi-commit branch may be under-reviewed")
    return Base(ref="HEAD", sha=_out(repo, "rev-parse", "HEAD"),
                warning="no main ref and no parent commit; falling back to HEAD")

@dataclass(frozen=True)
class Diff:
    data: bytes
    files: list[str]
    statuses: dict[str, str]
    truncated_untracked: bool = False

def capture_diff(repo: Path, base_sha: str, untracked_max: int) -> Diff:
    tracked = _run(repo, "--no-pager", "diff", "--no-ext-diff", "--no-textconv",
                   base_sha).stdout
    statuses: dict[str, str] = {}
    # -z: NUL-delimited records — "M\0path\0" / "R100\0old\0new\0". Never
    # text-parse + strip(): quotepath mangles non-ASCII, strip() eats real spaces.
    toks = _run(repo, "diff", "--name-status", "-z",
                base_sha).stdout.decode("utf-8", "replace").split("\0")
    i = 0
    while i < len(toks) and toks[i]:
        code = toks[i][:1]
        two_path = code in ("R", "C")            # rename AND copy carry old\0new
        path = toks[i + 2] if two_path else toks[i + 1]      # new name wins
        statuses[path] = code
        i += 3 if two_path else 2
    files = list(statuses)
    untracked = [f for f in _run(repo, "ls-files", "--others", "--exclude-standard",
                                 "-z").stdout.decode("utf-8", "replace").split("\0") if f]
    truncated = len(untracked) > untracked_max
    # ORACLE PARITY: every capture round-trips a shell $(...) in the oracle, so
    # each SECTION loses its trailing newlines, and sections are joined with
    # exactly one "\n" (DIFF="$DIFF"$'\n'"$UDIFF"). Untracked-only therefore
    # starts "\n" + udiff (empty first section). Joining unstripped sections
    # would insert a doubled blank line and break the --diff-hash parity tests,
    # which are the authority here.
    sections = [tracked.rstrip(b"\n")]
    for f in sorted(untracked)[:untracked_max]:
        cp = _run(repo, "--no-pager", "diff", "--no-ext-diff", "--no-textconv",
                  "--no-index", "--", "/dev/null", f, ok_codes=(0, 1))
        sections.append(cp.stdout.rstrip(b"\n"))
        files.append(f); statuses[f] = "A"
    return Diff(data=b"\n".join(sections), files=files, statuses=statuses,
                truncated_untracked=truncated)

def git_common_dir(repo: Path) -> Path:
    return (Path(repo) / _out(repo, "rev-parse", "--git-common-dir")).resolve()

def current_branch(repo: Path) -> str:
    return _out(repo, "rev-parse", "--abbrev-ref", "HEAD")

def head_sha(repo: Path) -> str:
    return _out(repo, "rev-parse", "HEAD")

def is_primary_checkout(repo: Path) -> bool:
    git_dir = Path(_out(repo, "rev-parse", "--absolute-git-dir")).resolve()
    common = Path(_out(repo, "rev-parse", "--path-format=absolute",
                       "--git-common-dir")).resolve()
    return git_dir == common
```

- [ ] **Step 4: Run to verify PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat: git diff capture, base resolution, blob-sha1 diff identity"`

---

### Task 5: textnorm — the single key definition

**Files:**
- Create: `src/skodun/textnorm.py`, `tests/test_textnorm.py`

**Interfaces:**
- Produces: `norm(s: str) -> str`; `finding_key(file: str, title: str) -> str` (16 hex chars); `ledger_key(branch: str, base_sha: str, fkey: str) -> str`.
- **Porting oracle:** `grok_review_triage.py` lines 75–90 (`finding_key`) and 114–135 (ledger key). Port `norm` exactly (the oracle lowercases, strips, and collapses internal whitespace — copy its exact transformation; verify by running the oracle's functions against the same inputs during porting).
- **Parity requirement:** keys must match the legacy implementation byte-for-byte so existing `triage.jsonl` dismissals stay valid after import. The parity test below runs the *actual legacy module* against ours.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_textnorm.py
import importlib.util, sys
from pathlib import Path
from skodun.textnorm import finding_key, ledger_key, norm

from tests.conftest import oracle_dir
LEGACY = (oracle_dir() / "scripts" / "grok_review_triage.py") if oracle_dir() else None

def _load_legacy():
    spec = importlib.util.spec_from_file_location("legacy_triage", LEGACY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["legacy_triage"] = mod
    spec.loader.exec_module(mod)
    return mod

def test_key_shape():
    k = finding_key("src/Foo.scala", "  Missing NULL check ")
    assert len(k) == 16 and int(k, 16) >= 0
    assert finding_key("src/foo.scala", "missing null check") == k  # normalized

def test_parity_with_legacy_module():
    if LEGACY is None or not LEGACY.exists():
        import pytest; pytest.skip("oracle checkout not present (set SKODUN_ORACLE_DIR)")
    legacy = _load_legacy()
    cases = [("src/A.scala", "NPE in handler"),
             ("ui/x.ts", "  race   condition  IN effect "),
             ("db/чейндж.xml", "unicode Title ✓")]
    for f, t in cases:
        assert finding_key(f, t) == legacy.finding_key(f, t)
```

Note: the legacy module's public function names may differ (`finding_key` vs `_finding_key`); inspect `grok_review_triage.py` and adapt the parity test to call whatever it actually exposes, keeping the assertion identical.

- [ ] **Step 2: Run to verify FAIL**
- [ ] **Step 3: Implementation** — port from the oracle. Expected shape (verify against source before committing; the oracle wins):

```python
# src/skodun/textnorm.py
from __future__ import annotations
import hashlib, re

def norm(s) -> str:
    # PARITY-CRITICAL: byte-for-byte the oracle's _norm (grok_review_triage.py:70-72).
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()

def finding_key(file: str, title: str) -> str:
    h = hashlib.sha256()
    h.update(norm(file).encode("utf-8")); h.update(b"\0")
    h.update(norm(title).encode("utf-8"))
    return h.hexdigest()[:16]

def ledger_key(branch: str, base_sha: str, fkey: str) -> str:
    return "\0".join((norm(branch), norm(base_sha), fkey))
```

- [ ] **Step 4: Run to verify PASS** (parity test must pass against the real legacy module)
- [ ] **Step 5: Commit** — `git commit -am "feat: finding/ledger keys with byte parity against legacy triage"`

---

### Task 6: Triage — reason validation, ledger ops, artifact validation

**Files:**
- Create: `src/skodun/triage.py`, `tests/test_triage.py`

**Interfaces:**
- Consumes: `Store` (Task 3), `textnorm` (Task 5).
- Produces: `validate_reason(reason: str) -> None` (raises `TriageError`); `dismiss(store, review: dict, index: int, reason: str, now: str) -> dict`; `load_valid_artifact(rec: dict) -> dict` (raises `ArtifactError` on every self-inconsistent shape); `open_findings(review: dict, triaged: dict[str, dict]) -> list[dict]`.
- **Oracle:** `grok_review_triage.py` lines 58–63 and 93–107 (reason rules: min 20 chars post-normalization; reject the 27-item `PLACEHOLDER_REASONS` set, inlined verbatim below), 176–230 (artifact validation: reject non-object artifact, non-list findings, non-dict list members, boolean/float/string `findings_total`, and `findings_total != len(findings)`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_triage.py
import pytest
from skodun.triage import (TriageError, ArtifactError, validate_reason,
                           load_valid_artifact, dismiss, open_findings)
from skodun.store import Store
from skodun.textnorm import finding_key

GOOD = dict(id="r1", branch="feat", base_sha="s"*40, findings_total=1,
            findings=[dict(file="a.py", line=3, severity="high",
                           category="bug", title="NPE", detail="boom")])

def test_reason_rules():
    with pytest.raises(TriageError): validate_reason("false positive")
    with pytest.raises(TriageError): validate_reason("short")
    validate_reason("download-artifact@v4 already extracts to the target dir, see README")

def test_negative_index_rejected(tmp_path):
    from skodun.store import Store
    st = Store.open(tmp_path / "s.db")
    with pytest.raises(TriageError):
        dismiss(st, GOOD, -1, "a perfectly valid twenty-plus character reason here",
                now="2026-07-27T10:00:00Z")

def test_artifact_validation_fails_closed():
    for bad in [dict(GOOD, findings_total=True),
                dict(GOOD, findings_total=2),
                dict(GOOD, findings="oops"),
                dict(GOOD, findings=[1]),
                "not a dict"]:
        with pytest.raises(ArtifactError):
            load_valid_artifact(bad)

def test_dismiss_and_open(tmp_path):
    st = Store.open(tmp_path / "s.db")
    rec = dismiss(st, GOOD, 0, "line numbers drift; verified handler checks None on entry",
                  now="2026-07-27T10:00:00Z")
    assert rec["finding_key"] == finding_key("a.py", "NPE")
    triaged = st.triage_for("feat", "s"*40)
    assert open_findings(GOOD, triaged) == []
```

- [ ] **Step 2: Run to verify FAIL**
- [ ] **Step 3: Implementation**

```python
# src/skodun/triage.py
from __future__ import annotations
from .store import Store
from .textnorm import finding_key, ledger_key, norm

class TriageError(ValueError): ...
class ArtifactError(ValueError): ...

# PARITY: verbatim from tubescribes/scripts/grok_review_triage.py:58-63 —
# the parity test below asserts exact set equality against the oracle module.
PLACEHOLDER_REASONS = {
    "false positive", "fp", "not a bug", "wontfix", "won't fix", "no", "nope",
    "n/a", "na", "none", "ignore", "ignored", "skip", "skipped", "ok", "fine",
    "invalid", "wrong", "incorrect", "disagree", "not an issue", "no issue",
    "already fixed", "by design", "intentional", "known", "irrelevant",
}
MIN_REASON_CHARS = 20

def validate_reason(reason: str) -> None:
    n = norm(reason)
    if len(n) < MIN_REASON_CHARS:
        raise TriageError(f"reason too short (<{MIN_REASON_CHARS} chars normalized)")
    if n in PLACEHOLDER_REASONS:
        raise TriageError(f"placeholder reason rejected: {n!r}")

def load_valid_artifact(rec) -> dict:
    if not isinstance(rec, dict):
        raise ArtifactError("artifact is not an object")
    findings = rec.get("findings")
    if not isinstance(findings, list) or any(not isinstance(f, dict) for f in findings):
        raise ArtifactError("findings is not a list of objects")
    total = rec.get("findings_total")
    if isinstance(total, bool) or not isinstance(total, int):
        raise ArtifactError("findings_total is not an integer")
    if total != len(findings):
        raise ArtifactError(f"findings_total={total} != len(findings)={len(findings)} "
                            "(truncated or hand-edited artifact)")
    return rec

def dismiss(store: Store, review: dict, index: int, reason: str, now: str) -> dict:
    review = load_valid_artifact(review)
    validate_reason(reason)
    if not (0 <= index < len(review["findings"])):   # negative indexes must not
        raise TriageError(f"finding index {index} out of range")  # silently alias
    f = review["findings"][index]
    fkey = finding_key(f.get("file", ""), f.get("title", ""))
    rec = dict(ledger_key=ledger_key(review["branch"], review["base_sha"], fkey),
               finding_key=fkey, id=review["id"], branch=review["branch"],
               base_sha=review["base_sha"], file=f.get("file"), line=f.get("line"),
               severity=f.get("severity"), title=f.get("title"),
               dismissed_reason=reason, dismissed_at=now)
    store.add_triage(rec)
    return rec

def open_findings(review: dict, triaged: dict[str, dict]) -> list[dict]:
    out = []
    for f in load_valid_artifact(review)["findings"]:
        if finding_key(f.get("file", ""), f.get("title", "")) not in triaged:
            out.append(f)
    return out
```

- [ ] **Step 4: Run to verify PASS.** Add `test_placeholder_set_matches_legacy` (Task 5's legacy-module-loading technique): `assert PLACEHOLDER_REASONS == legacy.PLACEHOLDER_REASONS and MIN_REASON_CHARS == legacy.MIN_REASON_CHARS` — skipped when tubescribes is absent.
- [ ] **Step 5: Commit** — `git commit -am "feat: triage ledger with audited dismissal reasons and fail-closed artifact validation"`

---

### Task 7: Gate — exit contract 0/1/2

**Files:**
- Create: `src/skodun/gate.py`, `tests/test_gate.py`; Modify: `src/skodun/cli.py` (add `gate` subcommand)

**Interfaces:**
- Consumes: `Store`, `triage.open_findings`, `gitio` (diff identity of the *current* tree), `config`.
- Produces: `run_gate(store, repo: Path, cfg: Config, env=os.environ) -> GateResult(code: int, message: str)`; CLI `skodun gate` prints `SKODUN GATE: ...` lines and exits with the code.
- **Oracle:** `grok_review_triage.py` `gate` + `grok-review-now.sh --gate` (lines 53–136). Contract: recompute the current diff identity; **empty outgoing diff ⇒ PASS(0) "no outgoing change"** (oracle behavior — nothing to review); otherwise find a trustworthy review with that exact `diff_hash`; **re-assert the loaded artifact against the index** — the artifact's own `parse_ok/degraded/diff_truncated` must recompute to trustworthy via `trust.is_trustworthy` and the artifact's `diff_hash` must equal the current hash (index and artifact can diverge via crashed writer or hand edit; a derived summary is never trusted alone); verify `base_sha` matches (rebase detection); then 0 if no open findings, 1 if open findings remain, 2 otherwise. **Every unexpected exception → 2.** Identity-helper stderr (base warnings, untracked cap) is echoed as `SKODUN GATE: identity note: ...`.
- **Recorded bypass:** `SKODUN_GATE_SKIP=1` ⇒ exit 0 with `SKODUN GATE: SKIPPED — recorded as a decision`, and a `gate_events` row `outcome="skipped"`. Every gate decision (pass/fail/skipped) writes a `gate_events` row — a bypass is a decision on the record, never a rule that quietly stopped applying. The durability is itself fail-closed: `GateResult` carries the computed `diff_hash` so the event identifies the gated content, and **a failure to persist the event converts the result to exit 2** (a gate that cannot write its own record is running on a broken store and must not certify anything). Identity conventions for the two hash-less decisions, by design and tested: a **skipped** event records `diff_hash=None` — the bypass must work even when identity computation itself is broken (that is what a bypass is for), so it never depends on it; an **empty-diff pass** records `diff_hash=""` (the defined empty-change identity).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gate.py
from pathlib import Path
from skodun.gate import run_gate
from skodun.store import Store
from skodun.config import load_config
# reuse _mkrepo/_git helpers from tests/test_gitio.py via a tests/conftest.py fixture
from tests.test_gitio import _mkrepo, _git
from skodun import gitio

def _reviewed(store, repo, *, findings=(), trustworthy=True):
    base = gitio.resolve_base(repo)
    diff = gitio.capture_diff(repo, base.sha, 100)
    store.save_review(dict(
        id="r1", reviewed_at="2026-07-27T10:00:00Z", branch=gitio.current_branch(repo),
        head=gitio.head_sha(repo), base_ref=base.ref, base_sha=base.sha,
        diff_hash=gitio.diff_identity(diff.data), context_hash="", mode="now",
        model="m", adapter="grok", status="clean", parse_ok=trustworthy,
        degraded=False, diff_truncated=False, trustworthy=trustworthy,
        stop_reason="EndTurn", summary="s", findings_total=len(findings),
        severity={"high": 0, "medium": 0, "low": 0}, findings=list(findings)))
    return base

def test_gate_empty_diff_is_0_no_outgoing_change(tmp_path):
    repo = _mkrepo(tmp_path); st = Store.open(tmp_path / "s.db")
    r = run_gate(st, repo, load_config(repo))          # clean tree on main
    assert r.code == 0 and "no outgoing change" in r.message
    row = st._c.execute("SELECT diff_hash FROM gate_events").fetchone()
    assert row["diff_hash"] == ""                      # empty-change identity

def test_gate_no_review_is_2(tmp_path):
    repo = _mkrepo(tmp_path); st = Store.open(tmp_path / "s.db")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")   # real outgoing change
    assert run_gate(st, repo, load_config(repo)).code == 2

def test_gate_skip_is_recorded(tmp_path):
    repo = _mkrepo(tmp_path); st = Store.open(tmp_path / "s.db")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    r = run_gate(st, repo, load_config(repo), env={"SKODUN_GATE_SKIP": "1"})
    assert r.code == 0 and "SKIPPED" in r.message
    row = st._c.execute("SELECT outcome, diff_hash FROM gate_events").fetchone()
    assert row["outcome"] == "skipped"
    assert row["diff_hash"] is None    # skip never depends on identity computation

def test_gate_rejects_artifact_index_disagreement(tmp_path):
    repo = _mkrepo(tmp_path); st = Store.open(tmp_path / "s.db")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _reviewed(st, repo)
    # corrupt the artifact behind the index's back: hand-edit degraded=true
    st._c.execute("UPDATE reviews SET artifact_json=json_set(artifact_json,"
                  "'$.degraded', json('true'))")
    assert run_gate(st, repo, load_config(repo)).code == 2

def test_gate_clean_is_0_and_edit_invalidates(tmp_path):
    repo = _mkrepo(tmp_path); st = Store.open(tmp_path / "s.db")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _reviewed(st, repo)
    assert run_gate(st, repo, load_config(repo)).code == 0
    (repo / "a.txt").write_text("three\n", encoding="utf-8")   # content changed
    assert run_gate(st, repo, load_config(repo)).code == 2      # exact-content match only

def test_gate_open_finding_is_1_until_triaged(tmp_path):
    repo = _mkrepo(tmp_path); st = Store.open(tmp_path / "s.db")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _reviewed(st, repo, findings=[dict(file="a.txt", line=1, severity="high",
                                       category="bug", title="T", detail="d")])
    assert run_gate(st, repo, load_config(repo)).code == 1

def test_gate_store_corruption_is_2_not_1(tmp_path, monkeypatch):
    repo = _mkrepo(tmp_path); st = Store.open(tmp_path / "s.db")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")   # a real outgoing change —
    monkeypatch.setattr(st, "latest_trustworthy_for",        # else empty-diff PASS(0)
                        lambda *a: (_ for _ in ()).throw(RuntimeError("corrupt")))
    assert run_gate(st, repo, load_config(repo)).code == 2
```

- [ ] **Step 2: Run to verify FAIL**
- [ ] **Step 3: Implementation**

```python
# src/skodun/gate.py
from __future__ import annotations
import os, time
from dataclasses import dataclass
from pathlib import Path
from . import gitio
from .config import Config
from .store import Store
from .triage import ArtifactError, load_valid_artifact, open_findings
from .trust import is_trustworthy

@dataclass(frozen=True)
class GateResult:
    code: int
    message: str
    diff_hash: str | None = None

def _gate(store: Store, repo: Path, cfg: Config) -> GateResult:
    notes: list[str] = []
    base = gitio.resolve_base(repo)
    if base.warning:
        notes.append(f"identity note: {base.warning}")
    diff = gitio.capture_diff(repo, base.sha, cfg.defaults.untracked_max)
    if diff.truncated_untracked:
        notes.append(f"identity note: untracked scan capped at {cfg.defaults.untracked_max}")
    prefix = "".join(f"SKODUN GATE: {n}\n" for n in notes)
    if diff.data.rstrip(b"\n") == b"":
        return GateResult(0, prefix + "SKODUN GATE: PASS no outgoing change", "")
    dh = gitio.diff_identity(diff.data)
    review = store.latest_trustworthy_for(dh)
    if review is None:
        return GateResult(2, prefix + f"SKODUN GATE: FAIL(2) no trustworthy review for "
                                      f"diff_hash={dh[:12]}", dh)
    review = load_valid_artifact(review)
    # Re-assert artifact↔index agreement: the index is a derived summary and
    # the two can diverge (crashed writer, hand edit). Never trust it alone.
    axes = [review.get("parse_ok"), review.get("degraded"),
            review.get("diff_truncated"), review.get("trustworthy")]
    if any(not isinstance(v, bool) for v in axes):
        return GateResult(2, prefix + "SKODUN GATE: FAIL(2) artifact trust fields "
                                      "are not booleans", dh)
    recomputed = is_trustworthy(axes[0], axes[1], axes[2])
    if not recomputed or review["trustworthy"] is not recomputed:
        return GateResult(2, prefix + "SKODUN GATE: FAIL(2) index/artifact disagree "
                                      f"on trust for review {review.get('id')}", dh)
    if review.get("diff_hash") != dh:
        return GateResult(2, prefix + "SKODUN GATE: FAIL(2) index/artifact disagree "
                                      "on diff_hash", dh)
    if review.get("base_sha") != base.sha:
        return GateResult(2, prefix + "SKODUN GATE: FAIL(2) base_sha mismatch "
                                      "(rebase detected) — re-review required", dh)
    remaining = open_findings(review,
                              store.triage_for(review["branch"], review["base_sha"]))
    if remaining:
        return GateResult(1, prefix + f"SKODUN GATE: FAIL(1) {len(remaining)} finding(s) "
                                      f"open on review {review['id']}", dh)
    return GateResult(0, prefix + f"SKODUN GATE: PASS review {review['id']} "
                                  f"covers diff_hash={dh[:12]}", dh)

def run_gate(store: Store, repo: Path, cfg: Config, env=os.environ) -> GateResult:
    def _record(result: GateResult, outcome: str) -> GateResult:
        try:
            branch = None
            try:
                branch = gitio.current_branch(repo)
            except Exception:
                pass   # best-effort: a skip must survive a broken repo identity
            store.log_gate_event(dict(
                at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                repo=str(repo), branch=branch,
                diff_hash=result.diff_hash, outcome=outcome, code=result.code,
                note=result.message.splitlines()[-1]))
        except Exception as e:
            # A gate that cannot write its own record must not certify anything.
            return GateResult(2, result.message + f"\nSKODUN GATE: FAIL(2) "
                              f"could not record gate event: {e!r}", result.diff_hash)
        return result

    if env.get("SKODUN_GATE_SKIP") == "1":
        return _record(GateResult(0, "SKODUN GATE: SKIPPED — recorded as a decision"),
                       "skipped")
    try:
        r = _gate(store, repo, cfg)
        return _record(r, {0: "pass", 1: "open-findings", 2: "no-review"}[r.code])
    except ArtifactError as e:
        return _record(GateResult(2, f"SKODUN GATE: FAIL(2) invalid artifact: {e}"),
                       "error")
    except BaseException as e:   # EVERY unexpected error is 2, never 1
        return _record(GateResult(2, f"SKODUN GATE: FAIL(2) internal error: {e!r}"),
                       "error")
```

Wire into `cli.py`: subcommand `gate` with `--repo` (default cwd) loads config+store, prints `result.message`, returns `result.code`.

- [ ] **Step 4: Run to verify PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat: fail-closed review gate with 0/1/2 exit contract"`

---

### Task 8: Checklist selection (port)

**Files:**
- Create: `src/skodun/checklist.py`, `tests/test_checklist.py`

**Interfaces:**
- Consumes: repo-relative `docs/review/checklists/*.md` and `docs/review/code-rules.json` (paths from `Defaults`).
- Produces: `select(files: list[str], mode: str, checklist_dir: Path, rules_json: Path) -> Selection(sections: list[str], bytes_total: int, over_budget: bool, dropped: list[str], body: str, note: str = "")` — `note` carries fail-soft diagnostics (e.g. `"checklist selection failed: <err>; continuing without path-scoped rules"`); the pipeline surfaces it on stderr and in the artifact.
- **Oracle:** `grok-checklist-select.py` (136 lines). Semantics to port exactly:
  - Longest-exclusive-prefix mapping: `src/main/resources/db/changelog/`→`migrations`, `src/main/`→`backend`, `ui/`→`frontend`, `scripts/`|`.github/`→`tooling`; test-path detection (`*.spec.ts`, `*.test.sh`, `*.test.mjs`, `test-utils`+`.ts`, `src/test/`, `integration-tests/src/test/`, `ui/src/app/testing/`) → `tests`.
  - `core` always included. `cross-file` only when a changed path matches a `crossFile` rule's globs read live from `code-rules.json`.
  - Modes: `full` (everything eligible), `batch` (never cross-file), `integration` (core + cross-file only).
  - Budget 18 KiB (18432 bytes); drop order lowest-first: `tooling, frontend, tests, backend, migrations, cross-file` — `core` never dropped.
  - **Fail-soft:** any exception ⇒ return empty `Selection` with a note; the caller proceeds without rules.

- [ ] **Step 1: Write the failing test** (fixtures: write small checklist files + a minimal `code-rules.json` with one `crossFile` rule globbing `src/main/**`)

```python
# tests/test_checklist.py
import json
from pathlib import Path
from skodun.checklist import select

def _fixtures(tmp_path: Path):
    cdir = tmp_path / "checklists"; cdir.mkdir()
    for name in ("core", "backend", "frontend", "tests", "migrations",
                 "tooling", "cross-file"):
        (cdir / f"{name}.md").write_text(f"## {name}\n- rule for {name}\n",
                                         encoding="utf-8")
    rules = tmp_path / "code-rules.json"
    rules.write_text(json.dumps({"version": 1, "rules": [
        {"id": "x-callers", "crossFile": True, "paths": ["src/main/**"],
         "doForm": "d", "flagForm": "f", "rationale": "docs/x.md",
         "layer": "guideline+checklist"}]}), encoding="utf-8")
    return cdir, rules

def test_selection_by_prefix_and_crossfile(tmp_path):
    cdir, rules = _fixtures(tmp_path)
    sel = select(["src/main/scala/App.scala", "ui/src/thing.ts"], "full", cdir, rules)
    assert set(sel.sections) == {"core", "backend", "frontend", "cross-file"}
    assert "rule for backend" in sel.body

def test_batch_mode_never_includes_crossfile(tmp_path):
    cdir, rules = _fixtures(tmp_path)
    sel = select(["src/main/scala/App.scala"], "batch", cdir, rules)
    assert "cross-file" not in sel.sections

def test_budget_drop_order_never_drops_core(tmp_path):
    cdir, rules = _fixtures(tmp_path)
    (cdir / "tooling.md").write_text("x" * 20000, encoding="utf-8")  # blows budget
    sel = select(["scripts/a.sh", "src/main/A.scala"], "full", cdir, rules)
    assert "tooling" in sel.dropped and "core" in sel.sections

def test_fail_soft_on_missing_dir(tmp_path):
    sel = select(["a"], "full", tmp_path / "nope", tmp_path / "nope.json")
    assert sel.sections == [] and sel.body == "" and "failed" in sel.note
```

- [ ] **Step 2: Run to verify FAIL**
- [ ] **Step 3: Implementation** — port `grok-checklist-select.py` into `select()`; keep constants `BUDGET = 18 * 1024`, `DROP_ORDER = ["tooling", "frontend", "tests", "backend", "migrations", "cross-file"]`; use `fnmatch`-on-`/`-segments for `crossFile` globs matching the oracle's glob semantics (check whether the oracle uses `fnmatch` or `pathlib.match` — mirror it). The whole body is wrapped in `try/except Exception as e: return Selection([], 0, False, [], "", note=f"checklist selection failed: {e}; continuing without path-scoped rules")`.
- [ ] **Step 4: Run to verify PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat: path-scoped checklist selection with budget and fail-soft (port)"`

---

### Task 9: Context packing (port)

**Files:**
- Create: `src/skodun/contextpack.py`, `tests/test_contextpack.py`

**Interfaces:**
- Produces: `pack(repo: Path, files: list[str], statuses: dict[str, str], headroom: int, source: str = "wt", per_file_cap: int | None = None) -> Pack(body: bytes, bytes_total: int, included: list[str], omitted: list[tuple[str, str]], sha256: str)` — `statuses` is `Diff.statuses` from Task 4 (path → `A`/`M`/`D`/…); the packer needs it to classify `deleted` (status `D`) and `already-in-diff` (status `A` and file < 16 KiB — its full content is already in the diff); a path absent from `statuses` is treated as `M`.
- Omission reasons (verbatim vocabulary): `deleted | binary | already-in-diff | missing | over-file-cap | over-headroom`.
- **Oracle:** `grok-context-pack.py` (467 lines). Port exactly:
  - Selection: candidates sorted descending by size, path ascending as tie-break; inclusion all-or-nothing per file.
  - `added` files < 16 KiB are `already-in-diff`.
  - **Security:** reject absolute paths, `..`, Windows drive prefixes, any symlink component; re-verify resolved path is under the worktree; open with `O_NOFOLLOW`.
  - Binary detection: first 8 KiB — NUL byte or >30% non-text bytes.
  - Emit `Context omitted for: a (reason), b (reason)` header; drop trailing sections until the header itself fits.
- Phase 1 uses only `source="wt"` (working tree); `oid` mode raises `NotImplementedError` (dispatcher is Phase 3).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contextpack.py
import os
from pathlib import Path
from skodun.contextpack import pack

M = {"big.py": "M", "small.py": "M", "link.txt": "M", "b.bin": "M",
     "../etc/passwd": "M", "/etc/passwd": "M", "new.py": "A", "gone.py": "D"}

def test_all_or_nothing_and_deterministic(tmp_path):
    (tmp_path / "big.py").write_text("x" * 5000, encoding="utf-8")
    (tmp_path / "small.py").write_text("y" * 100, encoding="utf-8")
    p1 = pack(tmp_path, ["big.py", "small.py"], M, headroom=600)
    assert p1.included == ["small.py"]           # big can't fit whole → skipped whole
    assert ("big.py", "over-headroom") in p1.omitted
    assert p1.body == pack(tmp_path, ["big.py", "small.py"], M, headroom=600).body

def test_added_and_deleted_classified(tmp_path):
    (tmp_path / "new.py").write_text("tiny added file", encoding="utf-8")
    p = pack(tmp_path, ["new.py", "gone.py"], M, headroom=10_000)
    assert ("new.py", "already-in-diff") in p.omitted   # A + <16KiB
    assert ("gone.py", "deleted") in p.omitted

def test_symlink_rejected(tmp_path):
    (tmp_path / "real.txt").write_text("secret", encoding="utf-8")
    os.symlink(tmp_path / "real.txt", tmp_path / "link.txt")
    p = pack(tmp_path, ["link.txt"], M, headroom=10_000)
    assert p.included == [] and p.omitted[0][0] == "link.txt"

def test_traversal_rejected(tmp_path):
    p = pack(tmp_path, ["../etc/passwd", "/etc/passwd"], M, headroom=10_000)
    assert p.included == []

def test_binary_omitted(tmp_path):
    (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02" * 100)
    p = pack(tmp_path, ["b.bin"], M, headroom=10_000)
    assert ("b.bin", "binary") in p.omitted
```

- [ ] **Step 2: Run to verify FAIL**
- [ ] **Step 3: Implementation** — port from the oracle. The path-validation function ports as:

```python
def _safe_open(repo: Path, rel: str):
    if rel.startswith(("/", "\\")) or ".." in Path(rel).parts or ":" in rel.split("/")[0]:
        return None
    cur = Path(repo)
    for part in Path(rel).parts:
        cur = cur / part
        if cur.is_symlink():
            return None
    resolved = cur.resolve()
    if not str(resolved).startswith(str(Path(repo).resolve()) + os.sep):
        return None
    try:
        if not stat.S_ISREG(os.lstat(resolved).st_mode):   # preflight: FIFO/device
            return None                                    # would block on open
        # O_NONBLOCK makes a racily-swapped-in FIFO open fail/return instead of
        # hanging; harmless for regular files. fstat re-checks post-open (TOCTOU).
        fd = os.open(resolved, os.O_RDONLY | os.O_NONBLOCK
                     | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    os.set_blocking(fd, True)
    return os.fdopen(fd, "rb")
```

Body assembly mirrors the oracle **byte-for-byte**: header line listing omissions, then per-file sections framed with the oracle's exact markers — `----- BEGIN FILE CONTEXT: <path> -----\n<bytes>\n----- END FILE CONTEXT -----\n` (not any invented `FILE:` marker — prompt parity is the point); recompute and drop trailing sections until the omission header fits inside `headroom`; `sha256` over the final body bytes. Add a byte-level fixture test asserting the exact marker lines. `_safe_open` additionally verifies the opened fd is a **regular file** via `stat.S_ISREG(os.fstat(fd).st_mode)` (checked post-open, so a FIFO or device swapped in cannot block the review process; close and return `None` otherwise).

- [ ] **Step 4: Run to verify PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat: hardened working-tree context packing (port)"`

---

### Task 10: Prompt construction

**Files:**
- Create: `src/skodun/promptbuild.py`, `tests/test_promptbuild.py`

**Interfaces:**
- Consumes: `checklist.Selection`, `contextpack.Pack`, `Diff` bytes.
- Produces: `build(branch, base_ref, base_sha, head, diff: bytes, max_diff_bytes: int, selection, pack_body: bytes | None) -> Prompt(text: bytes, diff_truncated: bool, prompt_bytes: int)` — the exact legacy prompt layout (oracle: `write_prompt`, `grok-prepush-review.sh` lines 1821–2066):
  1. reviewer instructions, 2. JSON response contract, 3. branch/base/head block, 4. `----- BEGIN REPO RULES (path-scoped) -----` section, 5. `----- BEGIN DIFF -----` + diff (truncated at `max_diff_bytes` with `----- DIFF TRUNCATED at N bytes -----` marker), 6. FILE CONTEXT sections.
- **Instruction text is ported BYTE-EXACTLY from the oracle's `write_prompt` (lines 2004–2064)** — including (a) the conditional 4-line FILE CONTEXT instruction block that appears **only when context packing is on** (`pack_body is not None`), and (b) the oracle's actual JSON response example, copied character-for-character (it is valid JSON; do NOT retype it from memory or from any summary — an invalid example in a schema-constrained prompt degrades output). The excerpt in the research report is orientation only, not the source.
- **Parity oracle:** the `--write-prompt` seam (`sh scripts/grok-prepush-review.sh --write-prompt ...`) generates the legacy prompt for a fixture diff. Add `test_prompt_parity_with_oracle` (skipped when tubescribes is absent): generate both prompts for the same fixture inputs and assert byte equality of the instruction header (everything above the branch/base/head block), for both the packing-on and packing-off variants.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_promptbuild.py
from skodun.promptbuild import build
from skodun.checklist import Selection

SEL = Selection(sections=["core"], bytes_total=10, over_budget=False,
                dropped=[], body="## core\n- r\n")

def test_layout_and_truncation():
    diff = b"diff --git a/a b/a\n" + b"x" * 100
    p = build("feat", "origin/main", "s"*40, "h"*40, diff,
              max_diff_bytes=50, selection=SEL, pack_body=b"CTX")
    t = p.text
    assert p.diff_truncated is True
    assert b"----- DIFF TRUNCATED at 50 bytes -----" in t
    assert t.index(b"BEGIN REPO RULES") < t.index(b"BEGIN DIFF")
    assert t.index(b"END DIFF") < t.index(b"CTX")

def test_no_truncation_when_within_budget():
    p = build("b", "origin/main", "s"*40, "h"*40, b"small", 400_000, SEL, None)
    assert p.diff_truncated is False and b"TRUNCATED" not in p.text
```

- [ ] **Step 2: Run to verify FAIL**
- [ ] **Step 3: Implementation** — assemble bytes exactly as above; `Prompt` is a dataclass `(text: bytes, diff_truncated: bool, prompt_bytes: int)`. Diff is truncated with `diff[:max_diff_bytes]` and the marker appended. Head is labeled `(working tree)` in `--now` mode, matching the oracle.
- [ ] **Step 4: Run to verify PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat: legacy-layout review prompt builder"`

---

### Task 11: Watchdog runner

**Files:**
- Create: `src/skodun/runner.py`, `tests/test_runner.py`

**Interfaces:**
- Produces: `run_with_watchdog(cmd: list[str], timeout_sec: int, cwd: Path, stdout_path: Path, stderr_path: Path) -> RunResult(rc: int, timed_out: bool, duration_sec: float, first_output_sec: float | None)`.
- Semantics (oracle: `run_grok_with_timeout`, lines 1118–1174): child starts as its own session/process-group leader (`start_new_session=True`; the PGID equals the child pid — capture it at spawn); stdout/stderr stream **directly to files** (no pipes to the parent); on timeout, `SIGTERM` the whole group, 3s grace, then **always `SIGKILL` the group** (wrapped in `try/except ProcessLookupError`) — even if the leader already exited, a grandchild that ignored TERM must not survive; record time-to-first-output.
- **A timed-out run's stdout is evidence of nothing:** on `timed_out`, the runner **truncates `stdout_path` to zero bytes** before returning. The oracle does the same — a process can print a complete clean envelope and then hang, and parsing that output after retries are exhausted would mint a trustworthy clean review from a run that never finished.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_runner.py
import sys, time
from skodun.runner import run_with_watchdog

def test_completes_within_budget(tmp_path):
    r = run_with_watchdog([sys.executable, "-c", "print('hi')"], 10, tmp_path,
                          tmp_path / "out", tmp_path / "err")
    assert r.rc == 0 and not r.timed_out
    assert (tmp_path / "out").read_text(encoding="utf-8").strip() == "hi"

def test_kills_whole_group_even_if_leader_dies_and_grandchild_ignores_term(tmp_path):
    # grandchild ignores SIGTERM and records its pid; leader exits right after TERM
    gc = ("import os,signal,time;"
          f"open({str(tmp_path / 'gc.pid')!r},'w').write(str(os.getpid()));"
          "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)")
    code = (f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{gc!r}]);"
            "time.sleep(60)")
    t0 = time.monotonic()
    r = run_with_watchdog([sys.executable, "-c", code], 2, tmp_path,
                          tmp_path / "out", tmp_path / "err")
    assert r.timed_out and time.monotonic() - t0 < 15
    time.sleep(0.5)
    import os, pytest
    gc_pid = int((tmp_path / "gc.pid").read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(gc_pid, 0)          # grandchild must be dead, not just the leader

def test_timed_out_stdout_is_discarded(tmp_path):
    # prints a plausible clean envelope, then hangs — output must not survive
    code = "print('{\"structuredOutput\":{\"summary\":\"ok\",\"findings\":[]}}',flush=True);import time;time.sleep(60)"
    r = run_with_watchdog([sys.executable, "-c", code], 2, tmp_path,
                          tmp_path / "out", tmp_path / "err")
    assert r.timed_out and (tmp_path / "out").read_bytes() == b""
```

- [ ] **Step 2: Run to verify FAIL**
- [ ] **Step 3: Implementation**

```python
# src/skodun/runner.py
from __future__ import annotations
import os, signal, subprocess, time
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class RunResult:
    rc: int
    timed_out: bool
    duration_sec: float
    first_output_sec: float | None

def run_with_watchdog(cmd: list[str], timeout_sec: int, cwd: Path,
                      stdout_path: Path, stderr_path: Path) -> RunResult:
    t0 = time.monotonic()
    first_out: float | None = None
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=out, stderr=err,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        pg = proc.pid            # start_new_session=True → child is its own PGID;
        deadline = t0 + timeout_sec   # capture now, getpgid() races with exit
        timed_out = False
        while True:
            rc = proc.poll()
            if first_out is None and stdout_path.stat().st_size > 0:
                first_out = time.monotonic() - t0
            if rc is not None:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _killpg(pg, signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
                # ALWAYS nuke the group: the leader may be gone while a
                # TERM-ignoring grandchild lives on in the same PGID.
                _killpg(pg, signal.SIGKILL)
                rc = proc.returncode
                break
            time.sleep(0.25)
    if timed_out:
        # A run that never finished proved nothing — its output must not be
        # parseable into a trustworthy review (oracle truncates the same way).
        stdout_path.write_bytes(b"")
    return RunResult(rc=rc, timed_out=timed_out,
                     duration_sec=time.monotonic() - t0, first_output_sec=first_out)

def _killpg(pg: int, sig: int) -> None:
    try:
        os.killpg(pg, sig)
    except ProcessLookupError:
        pass
```

- [ ] **Step 4: Run to verify PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat: process-group watchdog runner"`

---

### Task 12: Grok adapter — invocation, envelope parsing, degraded detection

**Files:**
- Create: `src/skodun/adapters/__init__.py`, `src/skodun/adapters/grok.py`, `tests/test_adapter_grok.py`

**Interfaces:**
- Produces: `Adapter` protocol — `build_cmd(prompt_file: Path, reviewer: Reviewer, d: Defaults, cwd: Path) -> list[str]`; `parse(stdout: bytes, stderr: bytes) -> ParseResult(parse_ok, findings, summary, stop_reason, degraded, degraded_reason)`; registry `get_adapter(provider: str) -> Adapter` (`"xai" -> GrokAdapter`).
- **Command (oracle lines 1200–1206, plus explicit model):**
  `grok --prompt-file <f> --json-schema <SCHEMA> -m <model> --disable-web-search --no-subagents --no-memory --no-plan --max-turns <n> --verbatim --disallowed-tools <deny> --cwd <dir>` — plus `--effort <e>` only when the model supports it (capability table: models matching `grok-build*` do NOT support effort — raise `ValueError` if configured; others pass it through when set).
- `SCHEMA` (verbatim, single line): `{"type":"object","properties":{"summary":{"type":"string"},"findings":{"type":"array","items":{"type":"object","properties":{"file":{"type":"string"},"line":{"type":"integer"},"severity":{"type":"string","enum":["high","medium","low"]},"category":{"type":"string"},"title":{"type":"string"},"detail":{"type":"string"}},"required":["file","severity","title","detail"]}}},"required":["summary","findings"]}`
- **Binary resolution:** `resolve_grok_bin() -> str`: `SKODUN_GROK_BIN` env → `~/.grok/bin/grok` if executable → `"grok"`. (The legacy `-p` re-shell fallback is an intentional deviation — see Global Constraints.)
- **Envelope parse, 3-level fallback (oracle: easy-to-miss #6):** the **same eligibility predicate at every level** — a candidate object is accepted only if it has `summary` or `findings`: (1) root `structuredOutput` if eligible (an empty `{}` is NOT eligible and must fall through, or a hollow envelope masks a valid `text` payload); (2) `text` field — scanned with the raw decoder, not a bare `json.loads`, so prose around the object doesn't defeat it; (3) raw scan of full stdout for the first eligible object (never lock onto an individual finding-shaped object).
- **`parse_ok` requires schema-valid findings:** `summary` a str, `findings` a list where **every item** is a dict with `file`/`title`/`detail` strings and `severity` in `{high, medium, low}` (`line` optional int). A malformed item ⇒ `parse_ok=False` — otherwise garbage like `{"findings":[1]}` becomes a trustworthy record that Task 6 later rejects, stranding the gate.
- **Degraded detection (oracle `detect_degraded`, lines 362–398 — positive evidence only):**
  1. stderr contains `tool_error`, `execution_failure`, `dropped the response channel`, or `harness-side bug` / `harness side bug` — matched **case-insensitively** (the oracle uses `grep -i`; `Tool_Error` must not slip through);
  2. stdout contains the leaked control token `tool▁call` (U+2581), matched on **bytes**;
  3. root `stopReason` present and != `EndTurn` (parsed from JSON, never grepped);
  4. `max turns reached` in **stderr only**, case-insensitive.
  Explicit NON-signals (do not flag): `Transport channel closed, when Auth(AuthorizationRequired)`; `structuredOutputError` alongside `stopReason:EndTurn`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_adapter_grok.py
import json, pytest
from pathlib import Path
from skodun.adapters.grok import GrokAdapter, SCHEMA
from skodun.config import Reviewer, Defaults

R = Reviewer(name="f", provider="xai", model="grok-4.20-0309-reasoning", role="finder")
D = Defaults()

def test_cmd_has_explicit_model_and_denies_tools(tmp_path):
    cmd = GrokAdapter().build_cmd(tmp_path / "p.txt", R, D, tmp_path)
    s = " ".join(cmd)
    assert "-m grok-4.20-0309-reasoning" in s
    assert "--disallowed-tools bash,read,write,edit,web_search,web_fetch" in s
    assert "--json-schema" in s and "--max-turns 40" in s

def test_effort_rejected_for_grok_build(tmp_path):
    r = Reviewer(name="f", provider="xai", model="grok-build", role="finder",
                 effort="high")
    with pytest.raises(ValueError, match="effort"):
        GrokAdapter().build_cmd(tmp_path / "p.txt", r, D, tmp_path)

GOOD = {"summary": "ok", "findings": []}

def test_parse_structured_output():
    env = json.dumps({"structuredOutput": GOOD, "stopReason": "EndTurn"}).encode()
    p = GrokAdapter().parse(env, b"")
    assert p.parse_ok and not p.degraded and p.stop_reason == "EndTurn"

def test_parse_text_fallback_and_raw_scan():
    env = json.dumps({"structuredOutput": None,
                      "structuredOutputError": "x",
                      "text": json.dumps(GOOD), "stopReason": "EndTurn"}).encode()
    assert GrokAdapter().parse(env, b"").parse_ok
    raw = b'noise {"file":"a","severity":"low","title":"t","detail":"d"} ' + \
          json.dumps(GOOD).encode() + b" trailing"
    p = GrokAdapter().parse(raw, b"")
    assert p.parse_ok and p.summary == "ok"      # skipped the finding-shaped object

def test_degraded_cancelled_stop_reason():
    env = json.dumps({"structuredOutput": GOOD, "stopReason": "Cancelled"}).encode()
    p = GrokAdapter().parse(env, b"")
    assert p.degraded and "stopReason" in p.degraded_reason

def test_auth_noise_is_not_degraded():
    env = json.dumps({"structuredOutput": GOOD, "stopReason": "EndTurn"}).encode()
    err = b"worker quit with fatal: Transport channel closed, when Auth(AuthorizationRequired)"
    assert not GrokAdapter().parse(env, err).degraded

def test_stderr_signals_case_insensitive():
    env = json.dumps({"structuredOutput": GOOD, "stopReason": "EndTurn"}).encode()
    assert GrokAdapter().parse(env, b"Tool_Error: boom").degraded
    assert GrokAdapter().parse(env, b"Max Turns Reached").degraded

def test_hollow_structured_output_falls_through_to_text():
    env = json.dumps({"structuredOutput": {},          # eligible-looking but hollow
                      "text": json.dumps(GOOD), "stopReason": "EndTurn"}).encode()
    p = GrokAdapter().parse(env, b"")
    assert p.parse_ok and p.summary == "ok"

def test_malformed_finding_items_fail_parse():
    bad = {"summary": "ok", "findings": [1]}
    env = json.dumps({"structuredOutput": bad, "stopReason": "EndTurn"}).encode()
    assert not GrokAdapter().parse(env, b"").parse_ok
    bad2 = {"summary": "ok", "findings": [{"file": "a", "severity": "urgent",
                                           "title": "t", "detail": "d"}]}
    env2 = json.dumps({"structuredOutput": bad2, "stopReason": "EndTurn"}).encode()
    assert not GrokAdapter().parse(env2, b"").parse_ok
    bad3 = {"summary": "ok", "findings": [{"file": "a", "severity": "low",
                                           "title": "t", "detail": "d",
                                           "line": True}]}   # bool is an int subclass
    env3 = json.dumps({"structuredOutput": bad3, "stopReason": "EndTurn"}).encode()
    assert not GrokAdapter().parse(env3, b"").parse_ok

def test_max_turns_in_stdout_is_not_degraded_but_stderr_is():
    env = json.dumps({"structuredOutput":
                      {"summary": "discusses max turns reached", "findings": []},
                      "stopReason": "EndTurn"}).encode()
    assert not GrokAdapter().parse(env, b"").degraded
    assert GrokAdapter().parse(env, b"max turns reached").degraded
```

- [ ] **Step 2: Run to verify FAIL**
- [ ] **Step 3: Implementation**

```python
# src/skodun/adapters/__init__.py
from __future__ import annotations
from typing import Protocol
from pathlib import Path
from ..config import Defaults, Reviewer

class ParseResult:  # defined in grok.py, re-exported here
    ...

def get_adapter(provider: str):
    from .grok import GrokAdapter
    registry = {"xai": GrokAdapter}
    try:
        return registry[provider]()
    except KeyError:
        raise ValueError(f"no adapter for provider {provider!r} (phase 1: xai only)")
```

```python
# src/skodun/adapters/grok.py
from __future__ import annotations
import json, re
from dataclasses import dataclass
from pathlib import Path
from ..config import Defaults, Reviewer

SCHEMA = ('{"type":"object","properties":{"summary":{"type":"string"},"findings":'
          '{"type":"array","items":{"type":"object","properties":{"file":{"type":'
          '"string"},"line":{"type":"integer"},"severity":{"type":"string","enum":'
          '["high","medium","low"]},"category":{"type":"string"},"title":{"type":'
          '"string"},"detail":{"type":"string"}},"required":["file","severity",'
          '"title","detail"]}}},"required":["summary","findings"]}')

_STDERR_SIGNALS = (b"tool_error", b"execution_failure",
                   b"dropped the response channel",
                   b"harness-side bug", b"harness side bug")
_LEAKED_TOKEN = "tool▁call".encode("utf-8")
_SEVERITIES = {"high", "medium", "low"}

def resolve_grok_bin() -> str:
    import os
    from pathlib import Path as _P
    if os.environ.get("SKODUN_GROK_BIN"):
        return os.environ["SKODUN_GROK_BIN"]
    default = _P.home() / ".grok" / "bin" / "grok"
    return str(default) if os.access(default, os.X_OK) else "grok"

def _eligible(obj) -> bool:
    return isinstance(obj, dict) and ("summary" in obj or "findings" in obj)

def _valid_payload(obj) -> bool:
    if not (_eligible(obj) and isinstance(obj.get("summary"), str)
            and isinstance(obj.get("findings"), list)):
        return False
    for f in obj["findings"]:
        if not isinstance(f, dict):
            return False
        if not all(isinstance(f.get(k), str) for k in ("file", "title", "detail")):
            return False
        if f.get("severity") not in _SEVERITIES:
            return False
        if "line" in f and type(f["line"]) is not int:   # bool is an int subclass;
            return False                                 # {"line": true} must fail
    return True

@dataclass(frozen=True)
class ParseResult:
    parse_ok: bool
    findings: list
    summary: str
    stop_reason: str | None
    degraded: bool
    degraded_reason: str

class GrokAdapter:
    name = "grok"

    def build_cmd(self, prompt_file: Path, r: Reviewer, d: Defaults,
                  cwd: Path) -> list[str]:
        if r.model.startswith("grok-build") and r.effort:
            raise ValueError(f"model {r.model} does not support effort "
                             f"(configured {r.effort!r}) — remove it or change model")
        cmd = [resolve_grok_bin(), "--prompt-file", str(prompt_file),
               "--json-schema", SCHEMA,
               "-m", r.model, "--disable-web-search", "--no-subagents",
               "--no-memory", "--no-plan", "--max-turns", str(d.max_turns),
               "--verbatim", "--disallowed-tools", d.deny_tools, "--cwd", str(cwd)]
        if r.effort and r.effort != "none":
            cmd += ["--effort", r.effort]
        return cmd

    def parse(self, stdout: bytes, stderr: bytes) -> ParseResult:
        stop_reason = None
        payload = None
        try:
            root = json.loads(stdout.decode("utf-8", "replace"))
            if isinstance(root, dict):
                stop_reason = root.get("stopReason")
                so = root.get("structuredOutput")
                if _eligible(so):                       # {} falls through
                    payload = so
                elif isinstance(root.get("text"), str):
                    payload = _first_review_object(root["text"].encode("utf-8"))
        except ValueError:
            pass
        if payload is None:
            payload = _first_review_object(stdout)
        parse_ok = _valid_payload(payload)
        degraded, reason = _detect_degraded(stdout, stderr, stop_reason)
        return ParseResult(
            parse_ok=parse_ok,
            findings=list(payload["findings"]) if parse_ok else [],
            summary=payload["summary"] if parse_ok else "",
            stop_reason=stop_reason, degraded=degraded, degraded_reason=reason)

def _first_review_object(data: bytes):
    text = data.decode("utf-8", "replace")
    for m in re.finditer(r"\{", text):
        dec = json.JSONDecoder()
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        if _eligible(obj):
            return obj
    return None

def _detect_degraded(stdout: bytes, stderr: bytes,
                     stop_reason) -> tuple[bool, str]:
    err_l = stderr.lower()                    # oracle greps with -i
    for sig in _STDERR_SIGNALS:
        if sig in err_l:
            return True, f"stderr signal: {sig.decode()}"
    if _LEAKED_TOKEN in stdout:
        return True, "leaked tool-call control token in stdout"
    if stop_reason is not None and stop_reason != "EndTurn":
        return True, f"stopReason={stop_reason!r} (not EndTurn)"
    if b"max turns reached" in err_l:
        return True, "max turns reached (stderr)"
    return False, ""
```

- [ ] **Step 4: Run to verify PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat: grok adapter with envelope fallback parsing and degraded detection (port)"`

---

### Task 13: Verdict banner

**Files:**
- Modify: `src/skodun/trust.py` (created in Task 3 with the invariant; this task adds the banner), Create: `tests/test_trust.py`

**Interfaces:**
- Consumes: `is_trustworthy` (Task 3).
- Produces: `banner(review: dict) -> str` — single line, values read from the persisted record:
  `SKODUN VERDICT: trustworthy=<t> findings=<n> degraded=<d> stop_reason=<s> head=<head[:9]> id=<id> severity=<h>/<m>/<l>`
  plus `banner_failure(reason: str) -> str` → `SKODUN VERDICT: trustworthy=false reason=<reason>`.
- Oracle: lines 441–510 (banner is emitted from the recorded artifact, never recomputed; triple fallback exists in the pipeline task).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trust.py
from skodun.trust import is_trustworthy, banner

def test_invariant():
    assert is_trustworthy(True, False, False)
    assert not is_trustworthy(False, False, False)
    assert not is_trustworthy(True, True, False)
    assert not is_trustworthy(True, False, True)

def test_banner_reads_recorded_values():
    rec = dict(id="loop_1", head="a"*40, trustworthy=True, findings_total=2,
               degraded=False, stop_reason="EndTurn",
               severity={"high": 1, "medium": 0, "low": 1})
    b = banner(rec)
    assert b.startswith("SKODUN VERDICT: trustworthy=true findings=2 ")
    assert "severity=1/0/1" in b and "head=aaaaaaaaa" in b and "\n" not in b
```

- [ ] **Step 2: Run to verify FAIL**
- [ ] **Step 3: Implementation** — direct formatting; booleans rendered lowercase.
- [ ] **Step 4: Run to verify PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat: trust invariant and verdict banner"`

---

### Task 14: Extra passes — security & skeptic

**Files:**
- Create: `src/skodun/passes.py`, `tests/test_passes.py`

**Interfaces:**
- Consumes: `promptbuild` prompt shell, `Reviewer` roles from config.
- Produces:
  `should_run_security(mode: str, files: list[str]) -> bool` — `mode == "now"` AND any path risky: a path segment in `{auth, billing, credits, dao, db, webhook}`, or compacted basename containing `telegramwebhook`/`webhookrouteservice`, or matching `api/services/*RouteService*`;
  `should_run_skeptic(mode: str, trustworthy: bool, findings_total: int) -> bool` — `mode == "now" and trustworthy and findings_total == 0`;
  `security_prompt(...) -> bytes`, `skeptic_prompt(...) -> bytes` (oracle: `grok-extra-passes.py` `write-security-prompt` / `write-skeptic-prompt` — port the prompt texts verbatim, including the skeptic framing *"A previous reviewer cleared this pull-request diff (0 findings). Your job is the ADVERSARIAL CLEAN-CHECK: prove them wrong if you can."*);
  `merge_extra_pass(primary: dict, extra: dict | None, pass_name: str) -> dict` — findings merged with title prefix `(security) ` / `(skeptic) ` (if a title starts with `[rule-id]`, prepend `(extra-pass: <name>) ` to `detail` instead — never pollute `rule_ids` extraction). **Demotion keeps the two axes independent (oracle `merge_extra_pass`, lines 162–273):** a *failed* pass (`extra is None` or `extra["parse_ok"]` false) sets the primary's `parse_ok=False` and appends to `failure_reason`; a *degraded* pass sets the primary's `degraded=True` and appends to `degraded_reason` — it does **not** touch `parse_ok`. Either way `trustworthy` recomputes to False on save. A size-capped pass sets `partial_coverage=True` without demotion.
- Kill switches: env `SKODUN_SECURITY_PASS=0`, `SKODUN_SKEPTIC_PASS=0`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_passes.py
from skodun.passes import (should_run_security, should_run_skeptic,
                           merge_extra_pass)

def test_security_trigger_paths():
    assert should_run_security("now", ["src/main/scala/auth/Login.scala"])
    assert should_run_security("now", ["api/services/FooRouteService.scala"])
    assert not should_run_security("now", ["ui/src/button.ts"])
    assert not should_run_security("prepush", ["src/auth/x.scala"])  # now-mode only

def test_skeptic_only_on_clean_trustworthy_now():
    assert should_run_skeptic("now", True, 0)
    assert not should_run_skeptic("now", True, 1)
    assert not should_run_skeptic("now", False, 0)
    assert not should_run_skeptic("prepush", True, 0)

def _primary() -> dict:
    # fresh nested structures per test — merge_extra_pass is ported from an
    # in-place-mutating oracle, and dict() is only a shallow copy: a shared
    # findings list or severity dict would contaminate the next test.
    return dict(id="r", parse_ok=True, degraded=False, diff_truncated=False,
                trustworthy=True, findings_total=0, findings=[], summary="ok",
                severity={"high": 0, "medium": 0, "low": 0}, extra_passes={})

def test_failed_extra_pass_clears_parse_ok():
    out = merge_extra_pass(_primary(), None, "security")
    assert out["parse_ok"] is False
    assert "security" in out["failure_reason"]

def test_degraded_extra_pass_sets_degraded_not_parse_ok():
    extra = dict(parse_ok=True, degraded=True, degraded_reason="stopReason=Cancelled",
                 summary="s", findings=[])
    out = merge_extra_pass(_primary(), extra, "security")
    assert out["parse_ok"] is True          # axes stay independent (oracle semantics)
    assert out["degraded"] is True and "security" in out["degraded_reason"]

def test_merge_prefixes_titles_and_recounts():
    extra = dict(parse_ok=True, degraded=False, summary="found",
                 findings=[dict(file="a", line=1, severity="high",
                                category="", title="SQLi", detail="d")])
    out = merge_extra_pass(_primary(), extra, "security")
    f = out["findings"][0]
    assert f["title"] == "(security) SQLi" and f["category"] == "security"
    assert out["findings_total"] == 1 and out["severity"]["high"] == 1
    assert out["trustworthy"] is True

def test_rule_id_title_not_polluted():
    extra = dict(parse_ok=True, degraded=False, summary="s",
                 findings=[dict(file="a", line=1, severity="low",
                                category="bug", title="[no-blocking-handler] x",
                                detail="d")])
    out = merge_extra_pass(_primary(), extra, "security")
    f = out["findings"][0]
    assert f["title"].startswith("[no-blocking-handler]")
    assert f["detail"].startswith("(extra-pass: security) ")
```

- [ ] **Step 2: Run to verify FAIL**
- [ ] **Step 3: Implementation** — port decision + merge logic from `grok-extra-passes.py` (`should_run_security`, `should_run_skeptic`, `merge_extra_pass` at lines 162–273); prompts as module-level templates ported verbatim from `write-security-prompt`/`write-skeptic-prompt`. Severity recount happens after merge; empty/`other` category on a security finding is rewritten to `security`.
- [ ] **Step 4: Run to verify PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat: security and skeptic extra passes with demotion semantics (port)"`

---

### Task 15: Pipeline orchestrator + foreground lock

**Files:**
- Create: `src/skodun/pipeline.py`, `tests/test_pipeline.py`; Modify: `src/skodun/cli.py` (add `review` subcommand)

**Interfaces:**
- Consumes: everything above.
- Produces: `run_review(repo: Path, cfg: Config, store: Store, mode: str = "now", lock_wait: float | None = None, lock_poll: float | None = None, id_prefix="sk_") -> dict` (the persisted review record; `lock_wait`/`lock_poll` override the 2580s/10s defaults — tests inject short values); `recover_stale(store, cfg) -> int`; CLI `skodun review [--repo DIR]` prints progress to stderr and the verdict banner as the **last stdout line**, exits: 0 trustworthy-clean, 1 trustworthy-with-findings, **2 primary-checkout refusal or other preflight refusal**, 3 lock give-up, 4 not-trustworthy. **Every** failure path — including preflight, before any record exists — still emits a `banner_failure(...)` line as the last stdout line (the global banner invariant has no exceptions).
- Orchestration order (oracle: `--now` path, lines 3222–3419 + A9/A18 of the audit):
  1. Refuse primary checkout unless `SKODUN_ALLOW_MAIN=1` (exit 2, `banner_failure` emitted).
  2. `recover_stale(store, cfg)`: mark any `running` record older than `2 * timeout_sec * (1 + timeout_retries + degraded_retries) + 60` seconds as `failed` (a SIGKILLed run never reaches its own `finally`; startup sweep is the only reliable janitor). Same-branch supersede is out of scope (legacy retires only prepush workers).
  3. Acquire foreground lock at `<git-common-dir>/grok-reviews-foreground.lock` — **the legacy path AND the legacy owner-file format** (see below) so skodun and the legacy scripts serialize against each other during shadow runs and each can judge the other's liveness.
  4. Resolve base + capture diff; compute `diff_hash = gitio.diff_identity(diff.data)`. **No dedup: `--now` never dedups in the oracle** — every foreground invocation runs a fresh review (the dedup probe is dispatcher machinery, Phase 3).
  5. Decide `hold_for_security = should_run_security(...)` **before** any record is persisted.
  6. Checklist selection → context pack (headroom = `max_diff_bytes - len(diff)` minus 1, only when positive; statuses from `Diff.statuses`) → prompt build.
  7. Persist a `running` record (id `sk_<utcstamp>_<pid>_<uuid4-hex8>` — the uuid component is mandatory, see the lock section), then run the finder via adapter + watchdog. Retries, always fresh runs: timeout ⇒ up to `timeout_retries`; degraded ⇒ up to `degraded_retries`. Each attempt appends `{n, rc, timed_out, duration_sec, first_output_sec}` to `attempts[]`. A timed-out attempt has empty stdout (the runner truncated it) and is **never parsed**; retries exhausted on timeout ⇒ `parse_ok=False`, `failure_reason="timed out after N attempts"`, status `failed`.
  8. Parse; run security pass if held (merge per Task 14 semantics); run skeptic pass if eligible (merge).
  9. Persist the final record — **the full artifact schema**: identity (`branch, head, base_ref, base_sha, diff_hash, context_hash`), config echo (`model, adapter, mode, timeout_seconds, max_turns`), trust axes (`parse_ok, degraded, degraded_reason, stop_reason, diff_truncated`), telemetry (`files_changed[], diff_bytes, prompt_bytes, checklist_sections[], checklist_bytes, checklist_note, context_bytes, context_files[], context_omitted_files[], attempts[]` — `checklist_note` persists `Selection.note` so a fail-soft selection failure is visible in the artifact, not just on stderr), results (`summary, findings[], findings_total, severity{}, rule_ids[], extra_passes{}`, `failure_reason`). `rule_ids` extracted from finding titles with `\[([a-z0-9]+(?:-[a-z0-9]+)*)\]` (closes the rules-telemetry loop; Task 17's log viewer reads `files_changed`). Status `clean` if trustworthy, else `degraded`/`failed`. **Then** emit the banner from the persisted record. If persistence itself fails: print `banner_failure("no review was recorded")` and exit 4.
  10. On any crash, the `finally` block releases the lock (only if still owner) and downgrades a still-`running` record to `failed`.
- **Lock owner-file format (interop-critical):** the legacy scripts write `owner` as three lines — `pid=<pid>`, `started=<unix-epoch>`, `worktree=<abs-path>` — and parse peers the same way. skodun writes and parses **exactly this format**. A plain-integer owner file (or any unparsable owner) is treated as owner-unknown: reclaim only past the 30s write-grace + stale ceiling, mirroring the oracle (`grok-review-now.sh` lines 138–325). Interop is tested in both directions (skodun respects a legacy-format live lock; a legacy-side parse of skodun's owner file yields the right pid — asserted by writing/parsing the exact byte format).
- Reviewer selection: role `finder` from config (first enabled), roles `security` / `refuter` reuse the finder's adapter in Phase 1 (same model — parity with legacy same-model passes; cross-provider comes in Phase 2).

- [ ] **Step 1: Write the failing test** (uses a **fake grok binary** on PATH — a shell script emitting a canned envelope — so no real CLI/subscription is needed)

```python
# tests/test_pipeline.py
import json, os, stat
from pathlib import Path
from skodun.pipeline import run_review
from skodun.store import Store
from skodun.config import load_config
from tests.test_gitio import _mkrepo, _git

ENVELOPE = json.dumps({"structuredOutput": {"summary": "ok", "findings": []},
                       "stopReason": "EndTurn"})

def _fake_grok(tmp_path: Path, envelope: str) -> None:
    b = tmp_path / "bin"; b.mkdir(exist_ok=True)
    g = b / "grok"
    g.write_text("#!/bin/sh\n"
                 'echo invoked >> "$(dirname "$0")/calls.log"\n'
                 f"cat <<'EOF'\n{envelope}\nEOF\n", encoding="utf-8")
    g.chmod(g.stat().st_mode | stat.S_IEXEC)
    os.environ["PATH"] = f"{b}:{os.environ['PATH']}"
    os.environ.pop("SKODUN_GROK_BIN", None)   # ensure PATH resolution wins in tests

CFG = """
[[reviewers]]
name = "finder"
provider = "xai"
model = "grok-4.20-0309-reasoning"
role = "finder"
"""

def test_clean_run_records_and_banners(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SKODUN_ALLOW_MAIN", "1")
    _fake_grok(tmp_path, ENVELOPE)
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(CFG, encoding="utf-8")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    st = Store.open(tmp_path / "s.db")
    rec = run_review(repo, load_config(repo), st)
    assert rec["trustworthy"] is True and rec["status"] == "clean"
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1].startswith("SKODUN VERDICT: trustworthy=true findings=0")

def test_now_mode_never_dedups(tmp_path, monkeypatch):
    # oracle: --now always reviews; dedup is dispatcher machinery (Phase 3)
    monkeypatch.setenv("SKODUN_ALLOW_MAIN", "1")
    _fake_grok(tmp_path, ENVELOPE)      # fake grok appends to calls.log per run
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(CFG, encoding="utf-8")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    st = Store.open(tmp_path / "s.db")
    r1 = run_review(repo, load_config(repo), st)
    r2 = run_review(repo, load_config(repo), st)
    assert r2["id"] != r1["id"]                        # two distinct reviews
    calls = (tmp_path / "bin" / "calls.log").read_text(encoding="utf-8")
    assert calls.count("invoked") == 2                 # grok really ran twice

def test_legacy_format_live_lock_is_respected(tmp_path, monkeypatch):
    monkeypatch.setenv("SKODUN_ALLOW_MAIN", "1")
    _fake_grok(tmp_path, ENVELOPE)
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(CFG, encoding="utf-8")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    from skodun.gitio import git_common_dir
    lock = git_common_dir(repo) / "grok-reviews-foreground.lock"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(              # exact legacy byte format, live pid
        f"pid={os.getpid()}\nstarted={int(__import__('time').time())}\n"
        f"worktree={repo}\n", encoding="utf-8")
    st = Store.open(tmp_path / "s.db")
    import pytest
    from skodun.pipeline import LockTimeout
    with pytest.raises(LockTimeout):          # short wait injected for the test
        run_review(repo, load_config(repo), st, lock_wait=1, lock_poll=0.2)

def test_degraded_envelope_is_not_trustworthy(tmp_path, monkeypatch):
    monkeypatch.setenv("SKODUN_ALLOW_MAIN", "1")
    env = json.dumps({"structuredOutput": {"summary": "s", "findings": []},
                      "stopReason": "Cancelled"})
    _fake_grok(tmp_path, env)
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(
        CFG + "\n[defaults]\ndegraded_retries = 0\n", encoding="utf-8")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    st = Store.open(tmp_path / "s.db")
    rec = run_review(repo, load_config(repo), st)
    assert rec["trustworthy"] is False and rec["status"] == "degraded"

def test_oversized_diff_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SKODUN_ALLOW_MAIN", "1")
    _fake_grok(tmp_path, ENVELOPE)
    repo = _mkrepo(tmp_path)
    (repo / ".skodun.toml").write_text(
        CFG + "\n[defaults]\nmax_diff_bytes = 64\n", encoding="utf-8")
    _git(repo, "checkout", "-b", "feat")
    (repo / "a.txt").write_text("x" * 4096, encoding="utf-8")
    st = Store.open(tmp_path / "s.db")
    rec = run_review(repo, load_config(repo), st)
    assert rec["diff_truncated"] is True and rec["trustworthy"] is False
```

- [ ] **Step 2: Run to verify FAIL**
- [ ] **Step 3: Implementation** — implement `run_review` per the orchestration order above. The lock helper:

```python
def _acquire_fg_lock(common_dir: Path, worktree: Path,
                     poll=10, stale=2580, wait=2580) -> Path:
    lock = common_dir / "grok-reviews-foreground.lock"
    deadline = time.monotonic() + wait
    while True:
        try:
            lock.mkdir()
            # EXACT legacy owner format — the tubescribes scripts parse this
            # file to judge our liveness during shadow runs, and vice versa.
            (lock / "owner").write_text(
                f"pid={os.getpid()}\nstarted={int(time.time())}\n"
                f"worktree={worktree}\n", encoding="utf-8")
            return lock
        except FileExistsError:
            if _lock_is_stale(lock, stale):
                shutil.rmtree(lock, ignore_errors=True); continue
            if time.monotonic() >= deadline:
                raise LockTimeout(f"foreground lock held: {lock}")
            time.sleep(poll)

def _owner_pid(lock: Path) -> int | None:
    try:
        for line in (lock / "owner").read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k == "pid" and v.strip().isdigit():
                return int(v)
    except FileNotFoundError:
        pass
    return None   # unparsable/missing owner => owner-unknown, be conservative

def _lock_is_stale(lock: Path, stale: int) -> bool:
    pid = _owner_pid(lock)
    if pid is not None:
        try:
            os.kill(pid, 0); alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
    else:
        alive = None   # unknown owner: only the age ceilings may reclaim
    age = time.time() - lock.stat().st_mtime
    if alive is True:
        return age > stale
    if alive is False:
        return age > 30            # dead owner + write grace
    return age > max(stale, 30)    # unknown owner: full ceiling only
```

Release (in `finally`): re-read `_owner_pid(lock)`; remove the lock dir **only if it equals our pid** (ABA guard — a reclaim by a peer must not be deleted by us).

Release in `finally` only when `(lock/"owner")` still holds our pid (ABA guard, oracle: grok-review-now.sh lock release). Extra passes run while the lock is held. The record id: `f"sk_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{os.getpid()}_{uuid.uuid4().hex[:8]}"` — the uuid component is mandatory: second-resolution time + pid collides for two runs in the same process-second, and the store's upsert would silently overwrite the first review.

- [ ] **Step 4: Run to verify PASS** (tests use short `wait` via monkeypatched constants where needed)
- [ ] **Step 5: Commit** — `git commit -am "feat: foreground review pipeline with legacy-compatible lock, dedup, extra passes"`

---

### Task 16: Legacy import

**Files:**
- Create: `src/skodun/legacy_import.py`, `tests/test_legacy_import.py`; Modify: `cli.py` (add `import-legacy` subcommand)

**Interfaces:**
- Produces: `import_legacy(store: Store, grok_reviews_dir: Path) -> ImportStats(reviews: int, triage: int, skipped_lines: int, demoted_no_artifact: int)`.
- Semantics: read `index.jsonl` (JSONL, tolerate partial/corrupt lines with `errors="replace"` + per-line try/except — skip and count, never abort); map rows to review records with `source="legacy"`; back-compat trust rule (oracle: `grok_review_triage.py` lines 255–272): rows missing `trustworthy` derive it from `parse_ok and not degraded and not diff_truncated`.
- **A trustworthy import requires the full artifact.** An index row is a derived summary without `findings[]`; storing it as trustworthy would let it satisfy the gate, whose artifact validation (Task 6) then rejects it — stranding the gate at exit 2 forever. So: for each index row that would be trustworthy, load and validate `<id>.json` from the archive; import the full artifact. If the artifact file is missing, corrupt, or disagrees with the index row (`diff_hash`, `findings_total`), import the row **demoted** (`parse_ok=False`, `failure_reason="legacy import: artifact missing/invalid"`, counted in `demoted_no_artifact`) — history is preserved, trust is not.
- Read `triage.jsonl` into the triage table using the **recorded** `finding_key` (never recomputed — the ledger is the authority).
- Purpose: gate/dedup continuity for already-reviewed content + dismissals survive the migration.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_legacy_import.py
import json
from skodun.legacy_import import import_legacy
from skodun.store import Store

ROW = dict(id="loop_1", reviewed_at="2026-07-01T00:00:00Z", branch="b",
           head="h"*40, base_sha="s"*40, diff_hash="d"*40, mode="prepush",
           parse_ok=True, degraded=False, diff_truncated=False,
           findings_total=0, severity={"high":0,"medium":0,"low":0})  # no 'trustworthy'

def _archive(tmp_path, with_artifact: bool):
    d = tmp_path / ".grok-reviews"; d.mkdir()
    (d / "index.jsonl").write_text(json.dumps(ROW) + "\n{corrupt", encoding="utf-8")
    if with_artifact:
        (d / "loop_1.json").write_text(json.dumps({**ROW, "summary": "ok",
                                                   "findings": []}), encoding="utf-8")
    (d / "triage.jsonl").write_text(json.dumps(dict(
        finding_key="ab"*8, id="loop_0", head="h"*40, branch="b", base_sha="s"*40,
        file="a.py", line=1, severity="high", title="T",
        dismissed_reason="verified: handler checks None on entry, see PR #1",
        dismissed_at="2026-07-01T00:00:00Z")) + "\n", encoding="utf-8")
    return d

def test_import_full_artifact_backcompat_trust_and_corrupt_line(tmp_path):
    st = Store.open(tmp_path / "s.db")
    stats = import_legacy(st, _archive(tmp_path, with_artifact=True))
    assert stats.reviews == 1 and stats.triage == 1 and stats.skipped_lines == 1
    imported = st.latest_trustworthy_for("d"*40)
    assert imported["source"] == "legacy" and imported["findings"] == []
    assert "ab"*8 in st.triage_for("b", "s"*40)

def test_index_row_without_artifact_is_imported_demoted(tmp_path):
    st = Store.open(tmp_path / "s.db")
    stats = import_legacy(st, _archive(tmp_path, with_artifact=False))
    assert stats.demoted_no_artifact == 1
    assert st.latest_trustworthy_for("d"*40) is None      # never gate-eligible
    assert st.get_review("loop_1")["source"] == "legacy"  # history kept
```

- [ ] **Step 2: Run to verify FAIL**
- [ ] **Step 3: Implementation** — straightforward line-by-line reader; ledger_key built from recorded branch/base_sha/finding_key via `textnorm.ledger_key`.
- [ ] **Step 4: Run to verify PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat: legacy .grok-reviews import for dedup and triage continuity"`

---

### Task 17: Shadow compare + log viewer + CLI wiring

**Files:**
- Create: `src/skodun/shadow.py`, `tests/test_shadow.py`; Modify: `src/skodun/cli.py` (subcommands `shadow-compare`, `log`, `triage`)

**Interfaces:**
- Produces:
  `compare(store: Store, grok_reviews_dir: Path, diff_hash: str | None) -> list[Comparison]` — iterate the **union** of diff_hashes from both sides (skodun store ∪ legacy `index.jsonl`), or the one given hash; where a hash has multiple rows on a side, take the **newest by `reviewed_at`** per side; `Comparison(diff_hash, skodun: dict | None, legacy: dict | None, match: bool, deltas: dict)` — one-sided hashes get `match=False` with the missing side `None` (iterating only skodun rows could never surface `legacy-only`, making the summary a lie). **`match` has exactly one definition:** both sides present, both agree on `trustworthy`, and both agree on cleanliness (`findings_total == 0` vs `> 0`). Exact finding counts and severity tallies across two independent LLM runs are *not* expected to be equal — they go into `deltas` (`findings_total`, `sev_high/medium/low` as `(skodun, legacy)` pairs) for human eyes only and never affect `match`;
  CLI `skodun shadow-compare [--dir PATH]` prints a table (`diff_hash[:12] | skodun t/f/H-M-L | legacy t/f/H-M-L | MATCH/MISMATCH/SKODUN-ONLY/LEGACY-ONLY`) and a summary line `shadow: N compared, M matched, K skodun-only, L legacy-only`; exit 0 always (shadow is observational);
  CLI `skodun log [--branch B] [-n N]` prints `reviewed_at | branch | files | H/M/L | status | summary` newest-first with `!` on non-trustworthy rows;
  CLI `skodun triage <review-id> <finding-index> "<reason>"` and `skodun triage --list <review-id>`.
- Per-finding text diffs are printed for human eyes, never asserted.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shadow.py
import json
from skodun.shadow import compare
from skodun.store import Store
from tests.test_store import REC

def test_compare_matches_on_trust_and_cleanliness(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)  # trustworthy, clean, diff_hash = "d"*40
    d = tmp_path / ".grok-reviews"; d.mkdir()
    legacy = dict(id="loop_9", diff_hash="d"*40, parse_ok=True, degraded=False,
                  diff_truncated=False, trustworthy=True, findings_total=0,
                  severity={"high": 0, "medium": 0, "low": 0}, branch="b",
                  reviewed_at="2026-07-01T00:00:00Z")
    (d / "index.jsonl").write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    out = compare(st, d, None)
    assert len(out) == 1 and out[0].match is True

def test_mismatch_when_legacy_found_findings(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)
    d = tmp_path / ".grok-reviews"; d.mkdir()
    legacy = dict(id="loop_9", diff_hash="d"*40, trustworthy=True,
                  findings_total=2, severity={"high": 1, "medium": 1, "low": 0},
                  branch="b", reviewed_at="2026-07-01T00:00:00Z")
    (d / "index.jsonl").write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    out = compare(st, d, None)
    assert out[0].match is False and out[0].deltas["findings_total"] == (0, 2)

def test_union_surfaces_one_sided_hashes_and_newest_row_wins(tmp_path):
    st = Store.open(tmp_path / "s.db")
    st.save_review(REC)                                   # skodun-only: "d"*40
    d = tmp_path / ".grok-reviews"; d.mkdir()
    older = dict(id="loop_1", diff_hash="e"*40, trustworthy=False, findings_total=0,
                 severity={"high":0,"medium":0,"low":0}, branch="b",
                 reviewed_at="2026-07-01T00:00:00Z")
    newer = dict(older, id="loop_2", trustworthy=True,
                 reviewed_at="2026-07-02T00:00:00Z")
    (d / "index.jsonl").write_text(
        json.dumps(older) + "\n" + json.dumps(newer) + "\n", encoding="utf-8")
    out = {c.diff_hash: c for c in compare(st, d, None)}
    assert out["d"*40].legacy is None and out["d"*40].match is False  # skodun-only
    assert out["e"*40].skodun is None                                  # legacy-only
    assert out["e"*40].legacy["id"] == "loop_2"                        # newest wins
```

- [ ] **Step 2: Run to verify FAIL**
- [ ] **Step 3: Implementation** — `compare` joins on `diff_hash`; CLI wiring dispatches all subcommands (`review`, `gate`, `triage`, `log`, `import-legacy`, `shadow-compare`) to their modules; each returns an int exit code from `main`.
- [ ] **Step 4: Run to verify PASS** — plus full suite: `python -m pytest -q` → all green.
- [ ] **Step 5: Commit** — `git commit -am "feat: shadow comparison against legacy archive, log viewer, CLI wiring"`

---

### Task 18: Shadow run on tubescribes (manual acceptance)

**Files:**
- Create: `docs/shadow-mode.md` (runbook)

**Interfaces:** none (operational task).

- [ ] **Step 1: Write the runbook** (`docs/shadow-mode.md`):

```markdown
# Shadow-mode runbook (tubescribes)

1. In a tubescribes *linked worktree* with real outgoing changes:
   `skodun import-legacy --dir "$(git rev-parse --git-common-dir)/../.grok-reviews"`
   (run once; idempotent).
2. Global config `~/.config/skodun/config.toml` gets the finder reviewer:
   provider "xai", model "grok-4.20-0309-reasoning" (from tubescribes
   .grok/settings.json), no effort.
3. Run the legacy loop first:  `sh scripts/grok-review-now.sh`
4. Run skodun second:          `skodun review`
   (both serialize on the shared foreground lock — expected, not a bug)
5. Compare: `skodun shadow-compare`
6. Acceptance: over >= 5 real change-sets — no crash; every skodun run produces
   a trustworthy verdict or an explicit degraded/failed record; gate agrees
   with `sh scripts/grok-review-now.sh --gate` (same 0/1/2) on each; triage
   of one finding via `skodun triage` flips gate 1 -> 0.
7. Record each comparison row in this file's log table (append).
```

- [ ] **Step 2: Execute the runbook end-to-end** on real tubescribes worktrees until the log table holds **at least 5 comparison rows with distinct `diff_hash` values** (the acceptance bar in step 6 — one run does not satisfy it). Paste each `shadow-compare` output into the runbook log table as you go.
- [ ] **Step 3: Fix any parity break found** (oracle wins; add a regression test in the matching test file).
- [ ] **Step 4: Full suite green** — `python -m pytest -q`.
- [ ] **Step 5: Commit** — `git commit -am "docs: shadow-mode runbook with first live comparison log"`

---

## Self-Review Notes

- Spec coverage: diff identity (T4, legacy-compatible trailing-newline semantics + oracle parity test), checklists (T8), context packing (T9, status-aware), prompt (T10), watchdog/retries/timeout-output-discard (T11, T15), grok envelope + degraded detection (T12, case-insensitive), trust computed-on-write (T3) + banner (T13), security/skeptic with independent demotion axes (T14), gate 0/1/2 with empty-diff PASS, artifact↔index re-assertion, and recorded `SKODUN_GATE_SKIP` bypass (T7), triage ledger + parity keys + negative-index guard (T5, T6), SQLite + gate_events (T3), stale-record recovery (T15), legacy import with artifact-backed trust (T16), union shadow compare (T17), ≥5-change-set acceptance (T18), fg-lock byte-format interop (T15).
- Explicitly out of scope (Global Constraints): batching (fail-closed truncation tested in T15), pre-push dispatcher + dedup probe (`--now` never dedups — tested in T15), same-branch supersede, rules-registry generation/sync (stays in tubescribes), the legacy `-p` re-shell fallback, MCP/scheduling/other adapters/retention.
- Known intentional deviations from legacy: SQLite instead of JSONL sprawl; explicit `-m` model flag; `SKODUN`-prefixed banner/gate lines (cutover-compat shims are a later phase); phase-1 extra passes reuse the finder model (cross-provider refuter is Phase 2); no `-p` ARG_MAX fallback.
- This plan was adversarially reviewed by codex (gpt-5.6-sol, high reasoning effort) over multiple rounds; all 26 round-1, 12 round-2, and 9 round-3 findings (incl. `bool("false")` coercion, NUL-delimited path parsing with `R`/`C` two-path records, section-stripped `\n` join for diff parity, byte-exact prompt port via the `--write-prompt` seam, FIFO-safe `_safe_open`, the complete inline `PLACEHOLDER_REASONS` set, and uuid-suffixed record IDs) are incorporated above.
