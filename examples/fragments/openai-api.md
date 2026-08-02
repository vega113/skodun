# Fragment: OpenAI HTTP API (metered) reviews

Optional **metered** path: Chat Completions over HTTPS with an API key.
Distinct from `provider = "openai"` (Codex **subscription CLI**).

---

## When to use

| Use | Prefer |
|---|---|
| ChatGPT/Codex subscription CLI | `provider = "openai"` (codex) |
| Pay-per-token API, any OpenAI model id, spend caps | `provider = "openai-api"` |

---

## Config

```toml
[[reviewers]]
name     = "finder-openai-api"
provider = "openai-api"
model    = "gpt-5.6-luna"   # any model id the OpenAI API accepts
effort   = "medium"         # optional; mapped when the API supports it
role     = "finder"
max_cost_usd = 0.50         # optional per-attempt note ceiling (recorded)

# fallbacks = ["finder"]    # hop to a CLI reviewer if API is down
```

```bash
export OPENAI_API_KEY=sk-...
# Optional daily USD ceiling for this provider (default 10):
export SKODUN_OPENAI_API_SPEND_LIMIT_USD=10
# Optional rate overrides (USD per 1M tokens):
# export SKODUN_OPENAI_API_INPUT_USD_PER_1M=1.0
# export SKODUN_OPENAI_API_OUTPUT_USD_PER_1M=4.0

skodun review --repo /abs/worktree --reviewer finder-openai-api
```

---

## Spend tracking

- Each API call records **tokens + estimated $** in the store (`api_spend_events`)
  and on the attempt’s `usage` field in the review artifact.
- Daily ceiling is **per API provider** (UTC day), default **$10**.
- When the ceiling is reached, that provider is skipped (`unavailable` / quota-style)
  so the chain can hop; it does not spend past the cap for new calls.

Inspect via SQLite or future CLI; notes also print on stderr during review:
`openai-api usage: tokens=… est_cost=$…`.

---

## Security

- Never put API keys in repo TOML or commits.
- Machine env / secret manager only.
- `skodun providers` will list `openai-api` as an adapter (no local CLI binary).
