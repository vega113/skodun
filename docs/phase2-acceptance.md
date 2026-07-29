# Phase 2 live acceptance — runbook and evidence log

This document is two things at once: a **runbook** anyone can re-run against their
own machine, and the **evidence log** of the run that was actually executed. Every
block under an "Evidence" heading is real output from a real command, not a
description of one.

The epic's acceptance criteria say the criteria must be *demonstrated, not
asserted*. Where something could not be demonstrated in this environment, it is
marked as such with the reason. A documented gap is honest; a glossed one is not.
**Nothing below is marked ✅ on the strength of a partial demonstration** — see the
scorecard at the end.

## About the pasted transcripts

**All pasted output is sanitized.** Absolute paths, the porting oracle's location,
and the operator's home directory are replaced by the shell variables the runbook
itself defines (`$WORK`, `$SKODUN_ORACLE_DIR`, `$ARCHIVE`, `$HOME`). Nothing else
is edited: counts, exit codes, timings, review ids, diff hashes, model text and
error strings are verbatim. Where a line was dropped from a transcript for length,
the omission is marked inline.

---

## 0. Prerequisites, and the provider substitution this run had to make

### Safety rules — read before running anything

1. **Never point this runbook at your real store.** `SKODUN_DB` is pinned to a
   scratch path in *every* command below. A development store can hold thousands
   of imported reviews and the destructive steps (adoption, seeding) would be
   irreversible against it.
2. **Never point it at a repository you care about.** The runbook creates its own
   throwaway git repository and reviews *that*. Reviews are cheap to re-run;
   someone else's working tree is not.
3. **Pin the config too.** `SKODUN_CONFIG` is set to a scratch file so the run
   cannot pick up (or be perturbed by) a personal `~/.config/skodun/config.toml`.
4. **Do not run any `auth login` flow.** This runbook only *reads* provider
   credentials via the CLIs; it never rewrites them.
5. **Destructive steps run on a copy.** The one step that mutates a store
   (`triage --adopt-refuter`) runs against a *copy* of the store, never the
   original — see §1.4.

### Provider availability at the time of this run

Checked live, immediately before the run:

| provider | CLI | version | status |
|---|---|---|---|
| `openai` | `codex` | codex-cli 0.144.5 | **works** — `gpt-5.4-mini` confirmed live |
| `google` | `agy` | 1.1.8 | **works** — `gemini-3.6-flash-low` confirmed live |
| `xai` | `grok` | grok 0.2.112 | **dead** — HTTP 402, usage balance exhausted |
| `anthropic` | `claude` | 2.1.118 | **dead**, and *not registered at all* |

Evidence — the two dead ones:

```
$ grok --model grok-4.5 -p "reply with the single word OK"
Internal error: {
  "message": "API error (status 402 Payment Required): Grok Build usage balance exhausted",
  "http_status": 402
}
Error: Internal error: {
  "message": "API error (status 402 Payment Required): Grok Build usage balance exhausted",
  "http_status": 402
}
# rc 1, and the whole message is on stderr; stdout is empty.

$ claude -p "reply with the single word OK"
Not logged in · Please run /login
```

Evidence — `agy`'s model listing, which is why the `google` model id below carries
an effort suffix and **no** `effort` key (a suffixed id plus any non-matching
`effort` is a guaranteed rc-1 refusal; see `examples/multi-provider.toml`):

```
$ agy models
gemini-3.6-flash-high
gemini-3.6-flash-medium
gemini-3.6-flash-low
gemini-3.5-flash-high
gemini-3.5-flash-medium
gemini-3.5-flash-low
gemini-3.1-pro-high
gemini-3.1-pro-low
claude-sonnet-4-6
claude-opus-4-6-thinking
gpt-oss-120b-medium
```

### SUBSTITUTION — recorded, not silent

The plan's runbook specifies **finder = grok, refuter = codex (or claude)**. Both
of those named providers were unusable at run time: `grok` is out of quota and
`claude` is not logged in. This run therefore substituted:

> **finder = `codex` (provider `openai`), refuter = `agy` (provider `google`)** — and,
> for a second run, the reverse.

That still satisfies the criterion as written: the point of the cross-provider
requirement is that the finder and the refuter answer from *different vendors*, and
they do. The substitution is recorded here rather than swapped in quietly, because
"which model refuted this finding" is exactly the fact an audit trail exists to
preserve.

### `anthropic` is NOT a shipped adapter — this supersedes the plan and spec

The Phase 2 plan and spec describe four provider CLIs (grok, codex, claude, agy)
and a `claude.py` adapter. **That adapter was never shipped** — its task was
blocked — and `anthropic` is not in the registry. Any statement in the plan or the
design spec that treats `anthropic` as a shipped adapter is superseded by this
document. The shipped registry is `{xai, openai, google}`, the product says so, and
a config naming `anthropic` fails closed:

```
$ skodun providers --repo .
google | adapter=agy | binary=agy (executable) | state=none
openai | adapter=codex | binary=codex (executable) | state=none
xai | adapter=grok | binary=$HOME/.grok/bin/grok (executable) | state=none
skodun providers: FAILED reviewer 'security' uses provider 'anthropic', which has no registered adapter (known: ['google', 'openai', 'xai'])
# rc 1
```

### Environment used by every command below

```sh
WORK="$(mktemp -d)"                     # every artefact of this runbook lives here
export SKODUN_DB="$WORK/store.db"       # NEVER the real store
export SKODUN_CONFIG="$WORK/config.toml"
```

---

## 1. Cross-provider run

**What it must demonstrate:** a review on a change-set with at least one real
finding; `triage --list` showing the refuter annotation *with provider
attribution*; `--adopt-refuter` on a refuted finding; `skodun gate` flipping 1 → 0;
and the artifact JSON carrying per-pass `{provider, model, effort}`.

### 1.1 Set up a throwaway repository with a real defect

```sh
mkdir -p "$WORK/demo" && cd "$WORK/demo"
git init -q -b main .
mkdir src
cat > src/cart.py <<'EOF'
"""A tiny shopping-cart helper."""


def subtotal(items):
    total = 0
    for item in items:
        total += item["price"] * item["qty"]
    return total
EOF
git add -A && git commit -q -m "initial commit"

# `skodun review` refuses to run in a primary checkout, so branch into a
# linked worktree -- which is also how the tool is meant to be used.
git worktree add -q -b feature/refunds "$WORK/wt"
cd "$WORK/wt"
cat > src/refund.py <<'EOF'
"""Refund helpers."""

from .cart import subtotal


def average_item_price(items):
    return subtotal(items) / len(items)


def refund_amount(items, percent):
    total = subtotal(items)
    return total * percent / 100
EOF
git add -A && git commit -q -m "add refund helpers"
```

`average_item_price` raises `ZeroDivisionError` on an empty cart. That is the
"at least one real finding" the criterion asks for, and it is a genuine defect
rather than a marker planted for the reviewer to find.

### 1.2 Configure a cross-provider pair

```sh
cat > "$SKODUN_CONFIG" <<'EOF'
[[reviewers]]
name     = "finder"
provider = "openai"
model    = "gpt-5.4-mini"
effort   = "medium"
role     = "finder"

[[reviewers]]
name     = "refuter"
provider = "google"
model    = "gemini-3.6-flash-low"
role     = "refuter"
EOF
```

### 1.3 Review, then list the findings

**Evidence — the live review (finder `openai`, refuter `google`):**

```
$ skodun review --repo .
skodun: checklist: path-scoped rules dropped -- no checklist directory at $WORK/wt/docs/review/checklists -- continuing with generic review rules
skodun: reviewing 1 file(s) vs main as sk_20260729T034659Z_9048_86615b89 ...
skodun: refuter pass (annotation only) ...
SKODUN VERDICT: trustworthy=true findings=1 degraded=false stop_reason=turn.completed head=c345f58db id=sk_20260729T034659Z_9048_86615b89 severity=0/1/0
# wall clock: 30.7 s
```

**Evidence — `triage --list`, with the refuter annotation and its attribution:**

```
$ skodun triage sk_20260729T034659Z_9048_86615b89 --list
[0] medium src/refund.py:7 average_item_price divides by zero on empty carts (OPEN)
refuter(google/gemini-3.6-flash-low): confirmed — Line 7 executes subtotal(items) / len(items) without checking if items is empty, which raises ZeroDivisionError when len
# rc 0.  The annotation line is bounded and truncated by design.

$ skodun gate --repo .
SKODUN GATE: FAIL(1) 1 finding(s) open on review sk_20260729T034659Z_9048_86615b89
# rc 1
```

The annotation names **`google/gemini-3.6-flash-low`** while the finding came from
**`openai/gpt-5.4-mini`**. That is the provider attribution the criterion asks for,
and the two are different vendors.

**Evidence — per-pass `{provider, model, effort}` in the stored artifact:**

```json
{
  "model": "gpt-5.4-mini",
  "attempts": [
    {
      "n": 1,
      "provider": "openai",
      "model": "gpt-5.4-mini",
      "effort": "medium",
      "rc": 0,
      "timed_out": false,
      "duration_sec": 23.94,
      "first_output_sec": 1.54,
      "classification": { "kind": "ok", "category": "", "detail": "" }
    }
  ],
  "extra_passes": {
    "refuter": {
      "pass": "refuter", "ran": true, "status": "ran", "degraded": false,
      "verdicts_total": 1, "annotated": 1, "dropped": 0,
      "provider": "google", "model": "gemini-3.6-flash-low", "effort": null,
      "note": ""
    }
  }
}
```

`effort: null` on the refuter is correct and deliberate: `gemini-3.6-flash-low` is
an effort-**suffixed** id, so the config sets no `effort` key, and the artifact
records the absence rather than inventing a level.

**The same demonstration with the providers reversed** (finder `google`, refuter
`openai`) on a different change-set, to show the attribution is not an artefact of
one ordering:

```
$ skodun review --repo .
skodun: reviewing 1 file(s) vs main as sk_20260729T034949Z_15098_3b16da1d ...
skodun: refuter pass (annotation only) ...
SKODUN VERDICT: trustworthy=true findings=2 degraded=false stop_reason=SUCCESS head=843bf5c9d id=sk_20260729T034949Z_15098_3b16da1d severity=0/1/1

$ skodun triage sk_20260729T034949Z_15098_3b16da1d --list
[0] medium src/cachekey.py:15 Unbounded cache growth (OPEN)
refuter(openai/gpt-5.4-mini): confirmed — `MAX_KEYS = 1024` is defined, but `remember()` only does `_CACHE[key] = blob` and returns; there is no size check, evict
[1] low src/cachekey.py:10 Use of weak hash algorithm (MD5) (OPEN)
refuter(openai/gpt-5.4-mini): confirmed — `content_key()` returns `hashlib.md5(blob).hexdigest()`, so the diff directly uses MD5 as the cache key algorithm; there
```

Its artifact carries the mirrored provenance:

```json
{
  "attempts": [
    { "n": 1, "provider": "google", "model": "gemini-3.6-flash-low", "effort": null,
      "rc": 0, "duration_sec": 20.65,
      "classification": { "kind": "ok", "category": "", "detail": "" } }
  ],
  "extra_passes": {
    "refuter": { "pass": "refuter", "ran": true, "status": "ran",
                 "verdicts_total": 2, "annotated": 2, "dropped": 0,
                 "provider": "openai", "model": "gpt-5.4-mini", "effort": "medium" }
  }
}
```

### 1.4 The adoption path — what happened live, and what had to be seeded

**Live result, stated plainly: no refuter refuted anything.**

Four refuter passes were run across two change-sets and both provider orderings.
They produced **six verdicts, all `confirmed`** — none `refuted`:

| review | finder | refuter | verdicts |
|---|---|---|---|
| `sk_20260729T034659Z_…` | `openai/gpt-5.4-mini` | `google/gemini-3.6-flash-low` | 1 × confirmed |
| `sk_20260729T034904Z_…` | `openai/gpt-5.4-mini` | `google/gemini-3.6-flash-low` | 2 × confirmed |
| `sk_20260729T034949Z_…` | `google/gemini-3.6-flash-low` | `openai/gpt-5.4-mini` | 2 × confirmed |
| `sk_20260729T035034Z_…` | `google/gemini-3.6-flash-low` | `openai/gpt-5.4-mini` | 1 × confirmed |

That is a legitimate outcome and it was **not** faked into a refutation. The
change-sets were deliberately seeded with arguable findings (an MD5 cache key with
its non-security rationale removed; an unreachable-looking tail `return`), and the
refuters correctly declined to refute all of them. It is also a mildly reassuring
result: the annotation channel is not a rubber stamp in the permissive direction.

The remainder of §1.4 works on the fourth review in that table
(`sk_20260729T035034Z_19687_24c47975`, finder `google` / refuter `openai`, on a
third throwaway branch carrying a retry helper). Each part of this runbook uses
its own scratch store; that one's is `$WORK/store3.db`.

**What that *does* demonstrate live** is the guard — a `confirmed` verdict cannot
be adopted:

```
$ skodun triage sk_20260729T035034Z_19687_24c47975 0 --adopt-refuter
skodun triage: refused: the refuter's verdict on finding 0 is 'confirmed', not 'refuted'; only a refutation can be adopted as a dismissal
# rc 1

$ skodun gate --repo .
SKODUN GATE: FAIL(1) 1 finding(s) open on review sk_20260729T035034Z_19687_24c47975
# rc 1
```

**The 1 → 0 flip therefore had to be demonstrated on a seeded store copy.** The
following is explicitly labelled: the *review, the finding, the refuter pass and
its attribution are all live*; only the **verdict string and reasoning text** were
rewritten in a copy of the store, to stand in for the refutation no real refuter
produced.

```sh
cp "$WORK/store3.db" "$WORK/store3-seeded.db"      # a COPY -- never the original
python3 - "$WORK/store3-seeded.db" <<'PY'
import sqlite3, sys, json
c = sqlite3.connect(sys.argv[1])
rid, a = c.execute("select id, artifact_json from reviews").fetchone()
d = json.loads(a)
ann = d["findings"][0]["refuter"]
ann["verdict"] = "refuted"                          # SEEDED: was "confirmed"
ann["reasoning"] = (                                # SEEDED: replaces the live text
    "The tail return is not reachable for any positive retries value and "
    "retries<=0 is rejected by the caller's own argument validation, so "
    "this finding does not describe a defect in the change under review.")
c.execute("update reviews set artifact_json=? where id=?", (json.dumps(d), rid))
c.commit()
PY
export SKODUN_DB="$WORK/store3-seeded.db"
```

**Evidence — adoption, and the gate flipping 1 → 0:**

```
$ skodun triage sk_20260729T035034Z_19687_24c47975 --list
[0] medium src/retry.py:15 Unreachable return statement and improper handling for retries <= 0 (OPEN)
refuter(openai/gpt-5.4-mini): refuted — The tail return is not reachable for any positive retries value and retries<=0 is rejected by the caller's own argument 

$ skodun gate --repo .
SKODUN GATE: FAIL(1) 1 finding(s) open on review sk_20260729T035034Z_19687_24c47975
# rc 1

$ skodun triage sk_20260729T035034Z_19687_24c47975 0 --adopt-refuter
skodun triage: adopted the refuter's dismissal of finding 0 on review sk_20260729T035034Z_19687_24c47975
# rc 0

$ skodun gate --repo .
SKODUN GATE: PASS 1 finding(s), all triaged on review sk_20260729T035034Z_19687_24c47975 for diff_hash=08fe5027d9e8
# rc 0
```

The gate went **1 → 0** on a single per-finding adoption, and the persisted
dismissal reason carries the refuter's attribution.

---

## 2. Fallback drill

Cacheability is **category-scoped**, so this is two separate drills. A dead binary
is `category=binary` and is deliberately **not** cached; only `category=quota` is
cached provider-wide.

### 2(a) Availability — dead binary, live fallback

```sh
cat > "$SKODUN_CONFIG" <<'EOF'
[[reviewers]]
name      = "finder"
provider  = "xai"
model     = "grok-4.5"
role      = "finder"
fallbacks = ["finder-openai"]

[[reviewers]]
name     = "finder-openai"
provider = "openai"
model    = "gpt-5.4-mini"
effort   = "medium"
role     = "finder"
EOF
export SKODUN_GROK_BIN=/nonexistent/grok        # head of the chain is dead
```

**Evidence — the chain advances and the review is trustworthy:**

```
$ skodun review --repo .
skodun: reviewing 1 file(s) vs main as sk_20260729T035243Z_30115_7805b79c ...
skodun: finder (xai): binary not found at /nonexistent/grok; trying the next entry
skodun: skeptic pass ...
skodun: finder (xai): binary not found at /nonexistent/grok; trying the next entry
SKODUN VERDICT: trustworthy=true findings=2 degraded=false stop_reason=turn.completed head=87d29e781 id=sk_20260729T035243Z_30115_7805b79c severity=1/1/0
```

(The message appears twice because the skeptic pass runs its own chain.)

**Evidence — `attempts[]` shows the `binary` classification, then the fallback
provider succeeding, and `provider_state` is empty — dead binaries are not
cached:**

```json
{
  "adapter": "codex", "model": "gpt-5.4-mini", "status": "clean", "trustworthy": true,
  "attempts": [
    { "n": 1, "provider": "xai", "model": "grok-4.5", "effort": null,
      "rc": null, "timed_out": null, "duration_sec": null,
      "classification": { "kind": "unavailable", "category": "binary",
                          "detail": "binary not found (rc 127)" },
      "skipped": "binary not found: /nonexistent/grok" },
    { "n": 2, "provider": "openai", "model": "gpt-5.4-mini", "effort": "medium",
      "rc": 0, "timed_out": false, "duration_sec": 14.159, "first_output_sec": 3.591,
      "classification": { "kind": "ok", "category": "", "detail": "" } }
  ]
}
```

```
provider_state rows: []
```

### 2(a′) Exhausted chain — both entries dead, on fresh content

```sh
# fresh content and a fresh store, so no prior trustworthy row can cover it
git commit -q -am "add backoff helper"
export SKODUN_DB="$WORK/fb2.db"
export SKODUN_GROK_BIN=/nonexistent/grok SKODUN_CODEX_BIN=/nonexistent/codex
```

**Evidence — `failed` record, banner `trustworthy=false`, gate exit 2:**

```
$ skodun review --repo .
skodun: reviewing 1 file(s) vs main as sk_20260729T035333Z_32285_f49daa20 ...
skodun: finder (xai): binary not found at /nonexistent/grok; trying the next entry
skodun: finder-openai (openai): binary not found at /nonexistent/codex; no entries remain
SKODUN VERDICT: trustworthy=false findings=0 degraded=false stop_reason= head=dbae11644 id=sk_20260729T035333Z_32285_f49daa20 severity=0/0/0
# rc 4

$ skodun gate --repo .
SKODUN GATE: FAIL(2) no trustworthy review covers diff_hash=f9fae18f3dc9 -- run a review before pushing
# rc 2
```

### 2(b) Cache — a quota-category failure, replayed from a fake CLI

The drill points a reviewer at a shell script standing in for the CLI, so the
failure is reproducible without burning real quota.

#### 2(b)-i — the REAL captured envelope, and the defect it exposes

The `grok` CLI on this machine is genuinely out of quota, so the envelope below is
a **real capture**, byte for byte:

```sh
cat > "$WORK/fake/grok-402.sh" <<'EOF'
#!/bin/sh
cat "$(dirname "$0")/grok-402-captured.txt" >&2
exit 1
EOF
# grok-402-captured.txt holds, verbatim:
#   Internal error: {
#     "message": "API error (status 402 Payment Required): Grok Build usage balance exhausted",
#     "http_status": 402
#   }
#   Error: Internal error: { ...the same object again... }
export SKODUN_GROK_BIN="$WORK/fake/grok-402.sh"
```

**Evidence — the real quota envelope classifies as `ok`, the chain does NOT
advance, and nothing is cached:**

```
$ skodun review --repo .
skodun: reviewing 1 file(s) vs main as sk_20260729T035357Z_32770_0ee3f824 ...
SKODUN VERDICT: trustworthy=false findings=0 degraded=false stop_reason= head=dbae11644 id=sk_20260729T035357Z_32770_0ee3f824 severity=0/0/0

$ skodun providers --repo .
google | adapter=agy | binary=agy (executable) | state=none
openai | adapter=codex | binary=codex (executable) | state=none
xai | adapter=grok | binary=$WORK/fake/... (executable) | state=none
```

```
status= failed   trustworthy= False
attempts = [
  { "n": 1, "provider": "xai", "model": "grok-4.5", "rc": 1, "duration_sec": 0.509,
    "classification": { "kind": "ok", "category": "", "detail": "" } }
]
provider_state: []
```

Reproduced against `classify` directly, with the captured stderr bytes:

```
>>> get_adapter("xai").classify(1, b"", open("grok-402-captured.txt","rb").read())
ClassifyResult(kind='ok', category='', detail='')
```

> ### DEFECT FOUND DURING ACCEPTANCE — real xAI quota exhaustion is not recognised
>
> The shipped `_QUOTA_SIGNALS` table in `src/skodun/adapters/grok.py` matches
> `quota`, `rate limit`, `rate_limit`, `ratelimit`, `too many requests`,
> `usage limit`, `insufficient credit`, `out of credits`. The message the installed
> `grok` CLI actually emits when the account is out of balance —
> `API error (status 402 Payment Required): Grok Build usage balance exhausted` —
> matches **none** of them, so:
>
> * the attempt classifies `ok` rather than `unavailable/quota`;
> * a configured fallback chain **does not advance** (only one attempt appears in
>   `attempts[]`, the codex fallback is never tried);
> * `provider_state` is **not** populated, so every later reviewer in the run keeps
>   paying the same failure.
>
> This is a **coverage gap in a signal table, not a break in the trust model**: the
> review still fails closed (`status=failed`, `trustworthy=false`), and the gate
> still returns 2. Nothing passes that should not. But the fallback feature is
> defeated by the exact real-world condition it exists for, and the closest real
> capture of that condition was never in the fixture set — no provider's fixture
> directory contains a `unavailable_quota` fixture at all. Fixing it is a table
> edit plus a fixture; it is out of scope for this task and is reported rather
> than patched here.

#### 2(b)-ii — an envelope whose text the shipped table does recognise

Because of the gap above, the caching half of the drill needs an envelope the
shipped signal table matches. This one is **synthesized** — shaped exactly like the
captured 402 above, but carrying a `429 / rate limit exceeded` message. It is
labelled as synthesized precisely because the real capture does not work:

```
Internal error: {
  "message": "API error (status 429 Too Many Requests): rate limit exceeded for this API key",
  "http_status": 429
}
Error: Internal error: { ...the same object again... }
```

**Evidence — the chain advances, and the provider-wide state is cached:**

```
$ skodun review --repo .
skodun: reviewing 1 file(s) vs main as sk_20260729T035408Z_33256_b3053ad5 ...
skodun: marking provider xai unavailable until 2026-07-29T04:24:09Z (quota failure in stderr (rate limit) with no usable review payload)
skodun: finder (xai) is unavailable (quota): quota failure in stderr (rate limit) with no usable review payload
SKODUN VERDICT: trustworthy=true findings=1 degraded=false stop_reason=turn.completed head=dbae11644 id=sk_20260729T035408Z_33256_b3053ad5 severity=0/1/0

$ skodun providers --repo .
google | adapter=agy | binary=agy (executable) | state=none
openai | adapter=codex | binary=codex (executable) | state=none
xai | adapter=grok | binary=$WORK/fake/... (executable) | state=active=True until=2026-07-29T04:24:09Z reason=quota failure in stderr (rate limit) with no usable review payload category=quota
```

```json
"attempts": [
  { "n": 1, "provider": "xai", "model": "grok-4.5", "rc": 1, "duration_sec": 0.507,
    "classification": { "kind": "unavailable", "category": "quota",
                        "detail": "quota failure in stderr (rate limit) with no usable review payload" } },
  { "n": 2, "provider": "openai", "model": "gpt-5.4-mini", "effort": "medium",
    "rc": 0, "duration_sec": 11.573,
    "classification": { "kind": "ok", "category": "", "detail": "" } }
]
provider_state: [('xai', '2026-07-29T04:24:09Z',
                  'quota failure in stderr (rate limit) with no usable review payload',
                  'quota', '2026-07-29T03:54:09Z')]
```

**Evidence — the cached state is honoured on the next run (the binary is not even
spawned: `rc` is `null` and `skipped` says why):**

```
$ skodun review --repo .
skodun: reviewing 1 file(s) vs main as sk_20260729T035436Z_34453_06fc604b ...
skodun: skipping finder (xai): quota failure in stderr (rate limit) with no usable review payload
SKODUN VERDICT: trustworthy=true findings=2 degraded=false stop_reason=turn.completed head=e51f48ef2 id=sk_20260729T035436Z_34453_06fc604b severity=1/0/1
```

```json
{ "n": 1, "provider": "xai", "model": "grok-4.5", "rc": null, "timed_out": null,
  "classification": { "kind": "unavailable", "category": "quota",
                      "detail": "quota failure in stderr (rate limit) with no usable review payload" },
  "skipped": "provider marked unavailable: quota failure in stderr (rate limit) with no usable review payload" }
```

**Evidence — `SKODUN_IGNORE_PROVIDER_STATE=1` bypasses the cache** (the attempt is
made for real again: `rc` is `1`, `duration_sec` is non-null, and the state is
re-marked):

```
$ SKODUN_IGNORE_PROVIDER_STATE=1 skodun review --repo .
skodun: reviewing 1 file(s) vs main as sk_20260729T035459Z_35452_00546f20 ...
skodun: marking provider xai unavailable until 2026-07-29T04:25:00Z (quota failure in stderr (rate limit) with no usable review payload)
skodun: finder (xai) is unavailable (quota): quota failure in stderr (rate limit) with no usable review payload
SKODUN VERDICT: trustworthy=true findings=2 degraded=false stop_reason=turn.completed head=044f8acff id=sk_20260729T035459Z_35452_00546f20 severity=1/1/0
```

```json
{ "n": 1, "provider": "xai", "model": "grok-4.5", "rc": 1, "timed_out": false,
  "duration_sec": 0.256,
  "classification": { "kind": "unavailable", "category": "quota",
                      "detail": "quota failure in stderr (rate limit) with no usable review payload" } }
```

---

## 3. No-regression — shadow comparison against the legacy archive

This part needs **no live provider**: it reads the legacy archive and a store.

```sh
export SKODUN_ORACLE_DIR=<path to the legacy checkout>   # never committed
export ARCHIVE="$SKODUN_ORACLE_DIR/.grok-reviews"
export SKODUN_DB="$WORK/shadow.db"                       # a FRESH scratch store
skodun import-legacy --dir "$ARCHIVE"
skodun shadow-compare --dir "$ARCHIVE"
```

**Evidence — import:**

```
$ skodun import-legacy --dir "$ARCHIVE"
skodun import-legacy: ok $SKODUN_ORACLE_DIR/.grok-reviews -> reviews=6928 triage=317 skipped_lines=0 demoted_no_artifact=4 demoted_untrustworthy=456 findings_reconciled=175 triage_unauditable=0 store_failures=0
# rc 0, 4.2 s
```

**Evidence — whole-archive comparison:**

```
$ skodun shadow-compare --dir "$ARCHIVE" | tail -3
ffe830de9939 | t/0-1-1 | t/0-1-1 | MATCH
fff1099bd6df | f/0-2-1 | f/0-2-1 | MATCH
shadow: 5076 compared, 5071 matched, 0 skodun-only, 0 legacy-only, since=none, 0 unparseable-timestamp rows excluded
# rc 0
```

**Evidence — the five non-matching rows, in full:**

```
33ff92baf5e3 | t/0-0-0 | t/0-1-1 | MISMATCH
7dad5892a77c | f/0-2-0 | t/0-2-0 | MISMATCH
bf857d0899df | f/0-0-0 | t/0-0-0 | MISMATCH
ed94c85ecb8a | f/0-0-0 | t/0-0-0 | MISMATCH
f4dd20e881ac | f/0-2-0 | t/0-2-0 | MISMATCH
```

Four of the five are `f` on skodun's side against `t` on the legacy side — skodun
declining to trust a row the legacy tool trusted. That is exactly the
`docs/shadow-mode.md` divergence class **"trust is never short-circuited"** plus
**"artifact validation is stricter"**: skodun always recomputes
`parse_ok and not degraded and not diff_truncated` and requires the artifact to
carry its required keys, where the legacy validator tolerates less. Every one of
them is fail-safe — the stricter side costs a re-review, never a silent all-clear.
The fifth (`33ff92baf5e3`) is a cleanliness difference between two independent
model runs over the same bytes, the same class as row 7 of the Phase 1 log.

**Evidence — `--since` windowed runs:**

```
$ skodun shadow-compare --dir "$ARCHIVE" --since 2026-07-20T00:00:00Z | tail -1
shadow: 1636 compared, 1635 matched, 0 skodun-only, 0 legacy-only, since=2026-07-20T00:00:00Z, 0 unparseable-timestamp rows excluded
# rc 0

$ skodun shadow-compare --dir "$ARCHIVE" --since 2026-07-28T00:00:00Z | tail -1
shadow: 253 compared, 251 matched, 0 skodun-only, 1 legacy-only, since=2026-07-28T00:00:00Z, 0 unparseable-timestamp rows excluded
# rc 0

$ skodun shadow-compare --dir "$ARCHIVE" --since "yesterday"
skodun shadow-compare: --since must be an ISO-8601 UTC timestamp like 2026-07-28T12:00:00Z, got 'yesterday'
# rc 2 -- ordinary misuse, a defined exit code, no traceback
```

### Are these counts consistent with the Phase 1 log?

The Phase 1 log (`docs/shadow-mode.md`, Run 1) recorded, against the archive as it
stood on 2026-07-27:

```
import: 6116 reviews, 263 dismissals, 0 corrupt lines
shadow: 4792 compared, 4783 matched, 0 skodun-only, 3 legacy-only
```

This run, two days later:

```
import: 6928 reviews, 317 dismissals, 0 corrupt lines
shadow: 5076 compared, 5071 matched, 0 skodun-only, 0 legacy-only
```

Consistent, and every delta belongs to a documented class:

| delta | class |
|---|---|
| 6116 → 6928 reviews, 263 → 317 dismissals | **archive growth.** The legacy system kept running between the two comparisons; `docs/shadow-mode.md` names this drift explicitly and is why `--since` exists. |
| 4792 → 5076 compared | the same growth, projected onto distinct `diff_hash` values. |
| 9 → 5 non-matching rows | the Phase 1 comparison ran against a store that *also* held live skodun shadow-run rows, which is where its extra disagreements came from (Run 1 row 7 is one of them). This run's store is a clean whole-archive import, so the only disagreements left are the structural ones above. |
| 3 → 0 legacy-only | `legacy-only` means "the archive has content skodun's store has never seen". A fresh whole-archive import necessarily has none. The 1 legacy-only row inside the `--since 2026-07-28` window is the same measure, correctly bounded. |
| 0 skodun-only, both runs | unchanged. |

No regression: the join still lands on essentially the whole archive, and the
disagreements that remain are the deliberate fail-safe divergences Phase 1 already
documented.

---

## 4. Full suite, both modes, one final time

Run in the foreground, with the environment cleaned of every override this runbook
sets, so the suite pins its own paths and cannot reach a real store.

**Evidence — with `SKODUN_ORACLE_DIR` set (parity tests RUN):**

```
$ SKODUN_ORACLE_DIR=<legacy checkout> python3 -m pytest -q -rs
........................................................................ [  4%]
...  (24 lines of dots elided)  ...
....................................................                     [100%]
1708 passed in 164.44s (0:02:44)
```

**Evidence — with `SKODUN_ORACLE_DIR` unset (parity tests SKIP, cleanly):**

```
$ python3 -m pytest -q -rs        # SKODUN_ORACLE_DIR unset
...  (skip reasons elided; every one names SKODUN_ORACLE_DIR)  ...
SKIPPED [1] tests/test_legacy_import.py:1226: no legacy archive at $SKODUN_ORACLE_DIR/.grok-reviews
SKIPPED [14] tests/test_passes.py:761: SKODUN_ORACLE_DIR unset
SKIPPED [6] tests/test_passes.py:831: SKODUN_ORACLE_DIR unset
SKIPPED [3] tests/test_promptbuild.py:490: SKODUN_ORACLE_DIR unset or oracle script absent
1620 passed, 88 skipped in 139.17s (0:02:19)
```

| mode | ran | skipped | failed |
|---|---|---|---|
| `SKODUN_ORACLE_DIR` set | **1708** | 0 | 0 |
| `SKODUN_ORACLE_DIR` unset | **1620** | **88** | 0 |

1620 + 88 = 1708: the two modes account for exactly the same tests, and every skip
names `SKODUN_ORACLE_DIR` as its reason. **No parity test silently vanished.**

**Evidence — the conformance suite, which is the adapter registration gate:**

```
$ python3 -m pytest -q -k conformance
................................................                         [100%]
48 passed, 1660 deselected in 0.28s
```

Coverage is enforced structurally rather than by convention:
`tests/adapter_conformance.py::test_every_registered_adapter_has_conformance_coverage`
compares `skodun.adapters._REGISTRY` against the collected `AdapterConformance`
subclasses in both directions, and
`test_coverage_gate_fails_without_a_conformance_subclass` proves that gate bites.
All three registered providers (`xai`, `openai`, `google`) have a `Test*Conformance`
subclass.

---

## 5. Acceptance criteria scorecard

| # | criterion | verdict | evidence |
|---|---|---|---|
| 1 | Full suite green with and without `SKODUN_ORACLE_DIR`; Phase 1 parity tests untouched and passing | **✅** | §4 — 1708 passed / 1620 passed + 88 skipped, 1620 + 88 = 1708 |
| 2 | Every adapter in the registry passes the shared conformance suite | **✅** | §4 — 48 passed, plus the two-way registry-coverage gate |
| 3 | Live cross-provider run: finder and refuter on different providers, annotations visible with attribution, one `--adopt-refuter` flips the gate 1 → 0, artifact shows per-pass provenance | **PARTIAL** | §1.3 fully live (both orderings); §1.4 — **the 1 → 0 flip was demonstrated on a SEEDED store copy**, because all six live refuter verdicts were `confirmed`. The refusal to adopt a `confirmed` verdict *was* demonstrated live. |
| 4 | Fallback drill: `[dead → real]` yields a trustworthy review whose `attempts[]` shows `unavailable` then the fallback; `[dead → dead]` on fresh content yields `failed`, `trustworthy=false`, gate 2; provider-state cache exercised with a quota-category failure | **PARTIAL** | §2(a) and §2(a′) fully live and complete. §2(b) — caching, honouring and bypass all demonstrated, **but with a synthesized envelope**, because the *real* captured xAI quota message matches no shipped signal and classifies as `ok`. See the DEFECT box in §2(b)-i. |
| 5 | No regression: whole-archive shadow comparison reproduces Phase 1 counts modulo documented classes and the `--since` window | **✅** | §3 — 5076 compared / 5071 matched, five structurally-explained mismatches, two `--since` windows, every delta from Phase 1 mapped to a documented class |

### Not demonstrated, and why

* **A live `refuted` verdict.** Four refuter passes, six verdicts, all `confirmed`.
  Not a product defect — arguably the opposite — but it means the end-to-end
  adoption path has never been exercised on model output that a model actually
  produced. The next person to see a live refutation should re-run §1.4 without the
  seeding step and replace that section.
* **The `anthropic` adapter.** Never shipped; its task was blocked. The plan and
  spec still describe four providers; three are registered. `skodun providers`
  reports the missing one and exits 1.
* **A `grok`-as-finder run.** The account is out of quota. Every `xai` appearance in
  this document is a dead binary, a fake CLI, or a real 402.
* **A real captured quota fixture.** No provider's fixture directory contains one,
  and the one real quota envelope available on this machine does not classify as
  quota (§2(b)-i).
