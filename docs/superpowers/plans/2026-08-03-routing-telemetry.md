# Routing Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make S5 Phase A's per-review routing audit readable in aggregate, so an operator can see which provider is carrying the load and whether routing put it there.

**Architecture:** One read-only `Store` method aggregates `adapter` × `route_reason` × `routed_reviewer` over a time window with a single grouped SQL query, using SQLite's `json_extract` over the existing `artifact_json`. `cli._cmd_providers` renders it: a header line for the effective routing config, one extra `served=` bit per provider line, and two footer lines. No schema change, no new persisted state.

**Tech Stack:** Python 3.12+, stdlib only, sqlite3 (JSON1), pytest.

Design: [`docs/superpowers/specs/2026-08-03-routing-telemetry-design.md`](../specs/2026-08-03-routing-telemetry-design.md).

## Global Constraints

- **Stdlib only.** No new dependencies.
- **No `SCHEMA_VERSION` bump.** The fields are already persisted in `artifact_json`; this plan adds no column, table, or migration.
- **ASCII-only output.** `cli._emit` guards against `UnicodeEncodeError` on an ASCII-only locale; separators in new output are `", "`, never `·` or an em dash.
- **`providers`' exit contract is unchanged:** `0` normally, `1` only when the loaded config names a provider with no registered adapter, `2` for a `--repo`, config, or store that could not be read at all. No new exit code, and no new way to reach an existing one except argparse's own rejection of a bad `--since-days`.
- **Degrade, never refuse.** Every new read is guarded and omits its output on failure, exactly as the existing `holders=` bit does.
- **Do not edit** `src/skodun/gate.py` or `src/skodun/trust.py`.
- Commit messages: complete sentences saying *why*; end with `refs #69, #77`.

---

### Task 1: `Store.routing_counts`

**Files:**
- Modify: `src/skodun/store.py` (new method beside `api_spend_sum_usd`, ~line 1980)
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Store.routing_counts(*, since_iso: str) -> list[dict]`. Each dict has keys `adapter` (str), `route_reason` (str | None), `routed_reviewer` (str | None), `n` (int). Task 2 consumes exactly this.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_store.py`. The record below is verified to pass `save_review`'s validation as written — do not trim fields from it.

```python
# --- routing telemetry (S5 Phase A read-back) -------------------------------


def _routing_review(st, rid, *, at, adapter, reason=None, routed=None):
    """One persisted review with (or without) a routing audit."""
    rec = {
        "id": rid, "reviewed_at": at, "source": "skodun", "branch": "feat",
        "head": "a" * 40, "base_ref": "main", "base_sha": "b" * 40,
        "diff_hash": rid, "mode": "now", "model": "m", "adapter": adapter,
        "status": "clean", "parse_ok": True, "degraded": False,
        "diff_truncated": False, "trustworthy": True, "stop_reason": None,
        "findings": [], "findings_total": 0, "summary": "",
    }
    if reason is not None:
        rec["route_reason"] = reason
        rec["routed_reviewer"] = routed
    st.save_review(rec)


def test_routing_counts_groups_by_adapter_reason_and_head(tmp_path):
    with Store.open(tmp_path / "s.db") as st:
        _routing_review(st, "r1", at="2026-08-02T00:00:00Z", adapter="grok",
                        reason="auto:free", routed="finder-grok")
        _routing_review(st, "r2", at="2026-08-02T01:00:00Z", adapter="grok",
                        reason="auto:free", routed="finder-grok")
        _routing_review(st, "r3", at="2026-08-02T02:00:00Z", adapter="codex",
                        reason="pinned", routed="finder-codex")
        rows = st.routing_counts(since_iso="2026-08-01T00:00:00Z")
    assert {(r["adapter"], r["route_reason"], r["routed_reviewer"], r["n"])
            for r in rows} == {
        ("grok", "auto:free", "finder-grok", 2),
        ("codex", "pinned", "finder-codex", 1),
    }


def test_routing_counts_excludes_reviews_before_the_window(tmp_path):
    """The boundary is inclusive: a review AT the cutoff is in the window."""
    with Store.open(tmp_path / "s.db") as st:
        _routing_review(st, "old", at="2026-07-30T23:59:59Z", adapter="grok",
                        reason="auto:free", routed="finder-grok")
        _routing_review(st, "edge", at="2026-08-01T00:00:00Z", adapter="grok",
                        reason="auto:free", routed="finder-grok")
        rows = st.routing_counts(since_iso="2026-08-01T00:00:00Z")
    assert [(r["adapter"], r["n"]) for r in rows] == [("grok", 1)]


def test_routing_counts_reports_unrouted_records_as_their_own_group(tmp_path):
    """Pre-S5 records and background pre-push reviews have no route audit.

    Both consumed a provider slot, so they belong in the total; neither was a
    routing decision, so neither may be counted as one.
    """
    with Store.open(tmp_path / "s.db") as st:
        _routing_review(st, "legacy", at="2026-08-02T00:00:00Z", adapter="grok")
        _routing_review(st, "routed", at="2026-08-02T01:00:00Z", adapter="grok",
                        reason="auto:free", routed="finder-grok")
        rows = st.routing_counts(since_iso="2026-08-01T00:00:00Z")
    by_reason = {r["route_reason"]: r["n"] for r in rows}
    assert by_reason == {None: 1, "auto:free": 1}


def test_routing_counts_on_an_empty_store_is_an_empty_list(tmp_path):
    with Store.open(tmp_path / "s.db") as st:
        assert st.routing_counts(since_iso="2026-08-01T00:00:00Z") == []


def test_routing_counts_refuses_a_timestamp_it_cannot_order_by(tmp_path):
    """String comparison is only correct for the canonical fixed-width shape."""
    with Store.open(tmp_path / "s.db") as st:
        with pytest.raises(ValueError, match="since_iso"):
            st.routing_counts(since_iso="last tuesday")
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python3 -m pytest tests/test_store.py -k routing_counts -q
```

Expected: FAIL, `AttributeError: 'Store' object has no attribute 'routing_counts'`.

- [ ] **Step 3: Implement the method**

Add to `src/skodun/store.py`, immediately after `api_spend_sum_usd`:

```python
    def routing_counts(self, *, since_iso: str) -> list[dict]:
        """Routing decisions since `since_iso`, grouped. Read-only, no schema.

        `(adapter, route_reason, routed_reviewer)` with a count each, where
        `adapter` is WHO SERVED (rewritten by the pipeline to whoever actually
        answered) and `routed_reviewer` is who the router CHOSE. After a
        fallback those name different providers, and the gap between them is
        the fallback rate -- see the S5 telemetry design.

        The routing fields live inside `artifact_json` rather than in columns,
        so they are read with `json_extract`, and the grouping happens in SQL:
        `list_reviews` decodes every artifact it returns, which for a whole
        window would be megabytes of findings and attempts to answer a question
        about four scalars. `json_valid` guards the extract so one malformed
        row -- an artifact written by something other than `json.dumps` -- costs
        its own row's attribution rather than blinding the whole query.

        A record with no routing audit yields `route_reason IS NULL`: it is
        either pre-S5 or a background pre-push review, and both consumed a
        provider slot without being a routing decision. The caller decides how
        to present that; this method does not hide it.

        The window is a string comparison, correct only because store
        timestamps are fixed-width canonical UTC -- hence `_require_ts`.
        `reviewed_at` carries no index of its own (only `(branch,
        reviewed_at)`), so this is a table scan. That is the right trade for a
        read-only diagnostic at these row counts, and it is cheaper than the
        index would be to maintain on every write.
        """
        since_iso = _require_ts("since_iso", since_iso)
        rows = self._c.execute(
            """SELECT adapter,
                      CASE WHEN json_valid(artifact_json)
                           THEN json_extract(artifact_json, '$.route_reason')
                      END AS route_reason,
                      CASE WHEN json_valid(artifact_json)
                           THEN json_extract(artifact_json, '$.routed_reviewer')
                      END AS routed_reviewer,
                      COUNT(*) AS n
                 FROM reviews
                WHERE reviewed_at >= ?
             GROUP BY adapter, route_reason, routed_reviewer
             ORDER BY adapter, route_reason, routed_reviewer""",
            (since_iso,)).fetchall()
        return [{"adapter": r["adapter"], "route_reason": r["route_reason"],
                 "routed_reviewer": r["routed_reviewer"], "n": int(r["n"])}
                for r in rows]
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python3 -m pytest tests/test_store.py -k routing_counts -q
```

Expected: `5 passed`.

- [ ] **Step 5: Run the whole store module for regressions**

```bash
python3 -m pytest tests/test_store.py -q --tb=line \
  --deselect tests/test_store.py::test_store_touching_modules_run_clean_under_resourcewarning_error
```

Expected: only the pre-existing `test_the_sweep_lists_every_store_touching_module` failure (`tests/test_openai_api.py` is unlisted on `main`). Any other failure is yours — stop and fix.

- [ ] **Step 6: Commit**

```bash
git add src/skodun/store.py tests/test_store.py
git commit -m "feat(store): read the S5 routing audit back in aggregate

Phase A records how every review was routed and nothing could read it in
aggregate. One grouped query over artifact_json answers it, with json_extract
doing the work in SQL rather than decoding every artifact in Python to reach
four scalars.

refs #69, #77"
```

---

### Task 2: `skodun providers` renders it

**Files:**
- Modify: `src/skodun/cli.py` — `_cmd_providers` (~line 1128) and the `providers` subparser (~line 96 region, beside `review`'s arguments)
- Test: `tests/test_cli.py`
- Modify: `examples/fragments/concurrency.md`, `README.md`

**Interfaces:**
- Consumes: `Store.routing_counts(*, since_iso: str) -> list[dict]` from Task 1.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`. `_seed_routing` writes records straight to a store the CLI will then read via `SKODUN_DB`.

```python
# --- providers: routing telemetry (S5) --------------------------------------


def _providers_out(tmp_path, monkeypatch, capsys, *argv) -> str:
    """`skodun providers` stdout, against a repo with two finders configured."""
    repo = tmp_path / "r"; repo.mkdir(exist_ok=True)
    (repo / ".skodun.toml").write_text("""
[routing]
mode = "auto"
[[reviewers]]
name = "finder-grok"
provider = "xai"
model = "m"
role = "finder"
[[reviewers]]
name = "finder-codex"
provider = "openai"
model = "m"
role = "finder"
""", encoding="utf-8")
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "no-global.toml"))
    main(["providers", "--repo", str(repo), *argv])
    return capsys.readouterr().out


def _seed_routing(db, rows):
    """rows: (id, reviewed_at, adapter, route_reason|None, routed_reviewer|None)."""
    from skodun.store import Store

    with Store.open(db) as st:
        for rid, at, adapter, reason, routed in rows:
            rec = {
                "id": rid, "reviewed_at": at, "source": "skodun",
                "branch": "feat", "head": "a" * 40, "base_ref": "main",
                "base_sha": "b" * 40, "diff_hash": rid, "mode": "now",
                "model": "m", "adapter": adapter, "status": "clean",
                "parse_ok": True, "degraded": False, "diff_truncated": False,
                "trustworthy": True, "stop_reason": None, "findings": [],
                "findings_total": 0, "summary": "",
            }
            if reason is not None:
                rec["route_reason"] = reason
                rec["routed_reviewer"] = routed
            st.save_review(rec)


def _recent(hours_ago: int) -> str:
    import time
    from skodun.store import _TS_FORMAT

    return time.strftime(_TS_FORMAT, time.gmtime(time.time() - hours_ago * 3600))


def test_providers_reports_the_effective_routing_config(tmp_path, monkeypatch,
                                                        capsys):
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "s.db"))
    out = _providers_out(tmp_path, monkeypatch, capsys)
    assert "routing: mode=auto" in out
    assert "pool=all-enabled-finders" in out
    assert "cross_model=on" in out
    assert "window=7d" in out


def test_providers_splits_served_counts_by_how_the_head_was_chosen(
        tmp_path, monkeypatch, capsys):
    """A provider at 80% because agents keep pinning it is a docs problem, not
    a weights problem, and an undifferentiated count cannot tell them apart."""
    db = tmp_path / "s.db"
    monkeypatch.setenv("SKODUN_DB", str(db))
    _seed_routing(db, [
        ("a", _recent(1), "grok", "auto:free", "finder-grok"),
        ("b", _recent(2), "grok", "pinned", "finder-grok"),
        ("c", _recent(3), "grok", None, None),
        ("d", _recent(4), "codex", "auto:wait", "finder-codex"),
    ])
    out = _providers_out(tmp_path, monkeypatch, capsys)
    assert "served=3/4 (auto 1, pinned 1, unrouted 1)" in out
    assert "served=1/4 (auto 1)" in out


def test_providers_footer_reports_exact_reasons_and_routed_heads(
        tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    monkeypatch.setenv("SKODUN_DB", str(db))
    _seed_routing(db, [
        ("a", _recent(1), "grok", "auto:free", "finder-grok"),
        ("b", _recent(2), "grok", "auto:free", "finder-grok"),
        ("c", _recent(3), "codex", "auto:wait", "finder-codex"),
        ("d", _recent(4), "codex", None, None),
    ])
    out = _providers_out(tmp_path, monkeypatch, capsys)
    assert "routing decisions (7d): auto:free 2, auto:wait 1, unrouted 1" in out
    assert "routed head (7d): finder-grok 2, finder-codex 1" in out


def test_providers_honours_since_days(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    monkeypatch.setenv("SKODUN_DB", str(db))
    _seed_routing(db, [
        ("recent", _recent(1), "grok", "auto:free", "finder-grok"),
        ("old", _recent(72), "grok", "auto:free", "finder-grok"),
    ])
    assert "served=2/2" in _providers_out(tmp_path, monkeypatch, capsys)
    out = _providers_out(tmp_path, monkeypatch, capsys, "--since-days", "1")
    assert "window=1d" in out and "served=1/1" in out


def test_providers_says_nothing_per_line_when_the_window_is_empty(
        tmp_path, monkeypatch, capsys):
    """`served=0/0` on every line is noise; say it once instead."""
    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "s.db"))
    out = _providers_out(tmp_path, monkeypatch, capsys)
    assert "served=" not in out
    assert "no reviews in the last 7d" in out


def test_providers_output_is_ascii_only(tmp_path, monkeypatch, capsys):
    """`_emit` guards a UnicodeEncodeError from an ASCII-only locale for a
    reason; new output must not be the thing that trips it."""
    db = tmp_path / "s.db"
    monkeypatch.setenv("SKODUN_DB", str(db))
    _seed_routing(db, [("a", _recent(1), "grok", "auto:free", "finder-grok")])
    out = _providers_out(tmp_path, monkeypatch, capsys)
    out.encode("ascii")            # raises UnicodeEncodeError if it is not


def test_a_routing_query_that_fails_omits_the_bit_and_keeps_exit_0(
        tmp_path, monkeypatch, capsys):
    """The `holders=` precedent: an operator running a diagnostic because
    something is wrong must not be refused the parts that still work."""
    from skodun.store import Store

    monkeypatch.setenv("SKODUN_DB", str(tmp_path / "s.db"))

    def boom(self, *, since_iso):
        raise RuntimeError("store is on fire")

    monkeypatch.setattr(Store, "routing_counts", boom)
    repo = tmp_path / "r"; repo.mkdir(exist_ok=True)
    (repo / ".skodun.toml").write_text(
        '[[reviewers]]\nname = "f"\nprovider = "xai"\nmodel = "m"\n',
        encoding="utf-8")
    monkeypatch.setenv("SKODUN_CONFIG", str(tmp_path / "no-global.toml"))
    assert main(["providers", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "served=" not in out
    assert "adapter=grok" in out          # the rest of the listing still ran


@pytest.mark.parametrize("bad", ["0", "-1", "lots"])
def test_since_days_must_be_a_positive_integer(tmp_path, bad, capsys):
    with pytest.raises(SystemExit) as e:
        main(["providers", "--repo", str(tmp_path), "--since-days", bad])
    assert e.value.code == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python3 -m pytest tests/test_cli.py -k "routing or since_days or served or ascii_only" -q
```

Expected: FAIL — `--since-days` is an unrecognized argument.

- [ ] **Step 3: Add the flag**

In `_build_parser`, on the `providers` subparser (the local variable is `providers`, assigned at `cli.py:173`), beside its existing `--repo`:

```python
    def _since_days(raw: str) -> int:
        """`--since-days`: a positive integer number of days, or argparse's 2."""
        try:
            value = int(raw, 10)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"expected a whole number of days, got {raw!r}") from None
        if value < 1:
            raise argparse.ArgumentTypeError(
                f"must be at least 1 day, got {value}")
        return value

    providers.add_argument(
        "--since-days", type=_since_days, default=7, dest="since_days",
        metavar="N",
        help="how many days of reviews the routing counts cover "
             "(default: 7)")
```

Define `_since_days` at module scope in `cli.py`, next to the other helpers, not nested in the parser builder.

- [ ] **Step 4: Add the rendering helpers**

At module scope in `src/skodun/cli.py`, above `_cmd_providers`:

```python
#: `route_reason` value -> the bucket the per-provider line counts it in.
#: `auto` is every `auto:*`, because the line answers "did the ROUTER put this
#: here", and which auto rule fired is the footer's question. `unrouted` is the
#: absence of an audit: a pre-S5 record, or a background pre-push review, which
#: the worker does not route. Both consumed a provider slot, so both are in the
#: denominator; neither was a routing decision, so neither is in `auto`.
_ROUTING_BUCKETS = ("auto", "pinned", "config", "unrouted", "other")


def _routing_bucket(reason: str | None) -> str:
    """Which `_ROUTING_BUCKETS` bucket a `route_reason` falls in.

    An unrecognised non-null reason lands in `other` rather than being dropped,
    so the buckets always sum to the total: a store written by a NEWER skodun
    must not make this listing quietly lie about how many reviews there were.
    """
    if reason is None:
        return "unrouted"
    if reason == "pinned":
        return "pinned"
    if reason == "config-finder":
        return "config"
    if reason.startswith("auto:"):
        return "auto"
    return "other"


def _routing_tally(rows: list[dict]) -> dict:
    """Fold `Store.routing_counts` rows into what the listing prints.

    `served` is keyed by ADAPTER NAME, which is what the review record carries
    and what `providers` already has in hand per line -- and it means "who
    actually answered", which after a fallback is not who the router chose.
    `heads` is keyed by the chosen entry. The two do not reconcile on purpose.
    """
    total = sum(r["n"] for r in rows)
    served: dict[str, dict[str, int]] = {}
    reasons: dict[str, int] = {}
    heads: dict[str, int] = {}
    for r in rows:
        n = r["n"]
        bucket = _routing_bucket(r["route_reason"])
        per = served.setdefault(r["adapter"], {})
        per[bucket] = per.get(bucket, 0) + n
        reasons[r["route_reason"] or "unrouted"] = (
            reasons.get(r["route_reason"] or "unrouted", 0) + n)
        if r["routed_reviewer"]:
            heads[r["routed_reviewer"]] = heads.get(r["routed_reviewer"], 0) + n
    return {"total": total, "served": served, "reasons": reasons,
            "heads": heads}


def _fmt_counts(counts: dict[str, int]) -> str:
    """`name n, name n`, commonest first, ties by name. ASCII separators only."""
    return ", ".join(
        f"{name} {n}" for name, n in
        sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _fmt_served(per_adapter: dict[str, int], total: int) -> str:
    """The `served=` bit for one provider line, or `""` when there is nothing
    to say (an empty window: `served=0/0` on every line is noise)."""
    if total <= 0:
        return ""
    served = sum(per_adapter.values())
    parts = ", ".join(f"{b} {per_adapter[b]}" for b in _ROUTING_BUCKETS
                      if per_adapter.get(b))
    detail = f" ({parts})" if parts else ""
    return f" | served={served}/{total}{detail}"


def _fmt_routing_header(routing, since_days: int) -> str:
    """The effective routing config, above the provider lines."""
    pool = ",".join(routing.pool) if routing.pool else "all-enabled-finders"
    return (f"routing: mode={routing.mode} pool={pool} "
            f"cross_model={'on' if routing.cross_model else 'off'} "
            f"window={since_days}d")
```

- [ ] **Step 5: Wire it into `_cmd_providers`**

Inside `_cmd_providers`, after `cfg` is loaded and the store is open (immediately after the `state_rows` block), read the counts once — guarded, like `holders`:

```python
        since_days = int(getattr(args, "since_days", 7) or 7)
        try:
            tally = _routing_tally(store.routing_counts(
                since_iso=time.strftime(
                    store_mod._TS_FORMAT,
                    time.gmtime(time.time() - since_days * 86400))))
        except Exception:
            # The `holders=` posture: an operator reaching for a diagnostic
            # because something is wrong must still get the parts that work.
            tally = None
        if tally is not None:
            _emit(_fmt_routing_header(cfg.routing, since_days), 0)
```

In the per-provider loop, append the bit to the emitted line:

```python
            served_bit = ("" if tally is None
                          else _fmt_served(tally["served"].get(adapter.name, {}),
                                           tally["total"]))
            _emit(f"{provider} | adapter={adapter.name} | "
                  f"binary={shown_binary} ({status}) | state={state}"
                  f"{holders_bit}{served_bit}", 0)
```

After the loop, and after the existing unregistered-`provider_state` notes, emit the footer:

```python
        if tally is not None:
            if tally["total"] == 0:
                _emit(f"routing: no reviews in the last {since_days}d", 0)
            else:
                _emit(f"routing decisions ({since_days}d): "
                      f"{_fmt_counts(tally['reasons'])}", 0)
                if tally["heads"]:
                    _emit(f"routed head ({since_days}d): "
                          f"{_fmt_counts(tally['heads'])}", 0)
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
python3 -m pytest tests/test_cli.py -k "routing or since_days or served or ascii_only" -q
```

Expected: all pass. If `test_providers_splits_served_counts_by_how_the_head_was_chosen` fails on ordering, check `_ROUTING_BUCKETS` order — the bucket detail is printed in that fixed order, not by count.

- [ ] **Step 7: Run the whole CLI module for regressions**

```bash
python3 -m pytest tests/test_cli.py -q --tb=line
```

Expected: all pass. The existing `providers` tests must be untouched — if one now fails on an unexpected extra line, the footer is being emitted where those tests assert on exact output; move it, do not weaken the test.

- [ ] **Step 8: Update the docs**

In `examples/fragments/concurrency.md`, in the "Auto-route the finder" section, after the **Audit** paragraph:

```markdown
**Seeing the distribution.** `skodun providers` reports, per provider,
how many reviews it *served* in a window and how those heads were chosen:

```bash
skodun providers --since-days 7
```

`served=` counts who actually answered; the `routed head` footer counts who the
router *chose*. After a fallback those differ, and the gap is the fallback rate.
```

In `README.md`, at the end of the "Auto-routing an un-pinned review" section:

```markdown
`skodun providers` reports the effective routing config and, per provider, how
many reviews it served in the last 7 days (`--since-days N`) split by how the
head was chosen — plus footer lines breaking down exact `route_reason` values
and routed entries. `served=` is who answered; `routed head` is who was chosen,
and after a fallback they differ.
```

- [ ] **Step 9: Verify the real command against a real store**

```bash
PYTHONPATH=src python3 -m skodun providers --since-days 7
```

Expected: the existing listing, plus a `routing:` header, `served=` bits, and footer lines. Confirm by eye that the per-provider `served` numbers sum to the denominator.

- [ ] **Step 10: Commit**

```bash
git add src/skodun/cli.py tests/test_cli.py examples/fragments/concurrency.md README.md
git commit -m "feat(cli): show routing distribution on skodun providers

Phase A's routing audit was write-only. This reads it back where an operator
already looks for provider state, so the question that gates Phase B -- is one
provider carrying the load, and did routing put it there -- has an answer.

The per-provider count keys on who SERVED, because that is who burned the
subscription; the footer keys on who the router CHOSE. After a fallback they
differ, and that gap is the fallback rate.

refs #69, #77"
```

---

## Verification (feature complete)

```bash
# the two modules this touches
python3 -m pytest tests/test_store.py tests/test_cli.py -q --tb=line \
  --deselect tests/test_store.py::test_store_touching_modules_run_clean_under_resourcewarning_error

# full suite before merge
python3 -m pytest -q --tb=line

# nothing sacred moved
git diff origin/main -- src/skodun/gate.py src/skodun/trust.py    # must be empty
git diff origin/main -- src/skodun/store.py | grep -c SCHEMA_VERSION   # must be 0
```

Two failures are expected and **pre-exist on `origin/main`** — verify by running them on the main checkout before blaming this branch:

- `test_store.py::test_the_sweep_lists_every_store_touching_module`
- `test_store.py::test_store_touching_modules_run_clean_under_resourcewarning_error`

## Out of scope

- Any `SCHEMA_VERSION` bump, index, or new column.
- An MCP tool for this.
- Weights, shares, or any Phase B mechanic.
- A machine-readable output format. This is a diagnostic a human reads; nothing parses it.
