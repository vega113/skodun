junie conformance fixtures
==========================

Witnesses for `tests/adapter_conformance.py`. Format: `rc=<int>`, optional
`category=` (required on every `*unavailable*` file), then `--- stdout ---`
and `--- stderr ---`.

The outer runner (`skodun.adapters.junie_runner`) is what the chain actually
spawns. On acceptance it prints a single contract-shaped JSON object on
stdout (REVIEW: `summary`+`findings`; REFUTER: `verdicts`). On refusal it
prints a sanitized reason on stderr and exits non-zero with empty stdout.
These fixtures therefore describe the outer runner's wire format, not the
raw junie CLI envelope.

Provenance
----------

healthy.txt / healthy_noisy_stderr.txt / refuter_healthy.txt
    SYNTHESIZED envelopes in the outer-runner accepted shape. Finding titles
    are plain ASCII (rule 6). No live junie capture yet — replace with a
    sanitized live capture when one is available.

unavailable_harness_envelope.txt / unavailable_harness_missing_envelope.txt
    SYNTHESIZED from the outer runner's own refusal strings
    (`envelope refused`, `did not produce a JSON output envelope`), which are
    present verbatim in `junie_runner.py`. These two were `degraded_stderr.txt`
    and `degraded_missing_envelope.txt` until issue #92: a refusal from
    skodun's OWN wrapper is not a review that came back badly, and classifying
    it as one stopped the fallback chain on an entry that had not served at
    all. Renamed rather than edited so the rename is what the diff shows.

degraded_truncated_stderr.txt / degraded_truncated_answer.txt
    SYNTHESIZED, and weaker than the two above: they carry the one remaining
    entry of `_DEGRADED_STDERR_SIGNALS` (`truncated`), which matches JUNIE's
    own stderr as the runner passes it through, and no wording of junie's has
    been captured for it. The wording here is therefore the author's, not the
    CLI's -- what these fixtures prove is that the adapter reads the axis, not
    that junie spells it this way.

    The pair covers the two shapes that matter and they are not the same case:
    `_answer` is the one worth having, an envelope that VALIDATES beside stderr
    saying the answer was cut short, which is the only way junie can return a
    review that parses and must not be trusted. `_stderr` is the empty-handed
    one, and is also the fixture rule 6 splices signal words from.

    Standing obligation, same as the quota one: when a truncated junie run is
    captured, replace these with the sanitized real stderr.

unavailable_auth.txt / unavailable_model.txt
    SYNTHESIZED from the adapter's stderr signal tables and common CLI
    wording. Not live captures.

unavailable_quota.txt
    SYNTHESIZED. Wording:
    * `payment required` — IANA reason phrase for HTTP 402 (same standard as
      the other adapters' speculative 402 entries).
    * `quota exceeded` / `rate limit` — present in this adapter's
      `_QUOTA_SIGNALS` table; chosen as specific phrases rather than a bare
      `quota` so model prose cannot take the provider down.
    Standing obligation: when a live junie quota failure is captured, replace
    this fixture with the sanitized real envelope.
