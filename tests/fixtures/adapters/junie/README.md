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

degraded_stderr.txt / degraded_missing_envelope.txt
    SYNTHESIZED from the outer runner's own refusal strings
    (`envelope refused`, `did not produce a JSON output envelope`), which are
    present verbatim in `junie_runner.py`.

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
