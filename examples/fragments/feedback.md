# Fragment: agent/human feedback (non-gate)

Paste when agents should record judgment or skodun product bugs **without**
clearing the gate.

**Not triage.** `triage_dismiss` / `triage_defer` move the gate and remain a
**human** decision. Feedback is an append-only inspection ledger.

---

## When to use feedback

| Kind | Use when |
|---|---|
| `finding_judgment` | Agent (or human) judgment on **one** finding: agree, disagree, nuance. Needs `review_id` + finding `index`. |
| `review_quality` | Whole-review quality note (noisy, excellent, missed class of bug). Needs `review_id`. |
| `product_bug` | Suspected **skodun** defect for maintainers to inspect and maybe file an issue. |
| `product_note` | Docs/UX/config product note without claiming a bug. |

Actor: `agent` (default) | `human` | `unknown`.  
Body: ≥20 characters of real substance (placeholders alone are useless later).

---

## CLI

```bash
# Agent judgment on finding [0]
skodun feedback add --kind finding_judgment --actor agent \
  --review-id sk_… --index 0 \
  "disagree: the guard at line 12 already rejects None before this path"

# Suspected skodun product bug
skodun feedback add --kind product_bug --actor agent \
  "MCP refuse-if-busy still allows two reviews when client retries tools/call; repro …"

# List for humans (filter optional)
skodun feedback list
skodun feedback list --kind product_bug -n 50
skodun feedback list --review-id sk_…
```

## MCP tools

- `feedback_add` — `kind`, `body` required; optional `actor`, `review_id`,
  `index`, `provider`, `repo`
- `feedback_list` — optional `kind`, `review_id`, `limit`

Agents: prefer `actor=agent`. Do **not** use feedback as a substitute for human
triage that clears the gate.

---

## Must not

- Do not `triage_dismiss` solely because an agent disagreed — record
  `finding_judgment`, then let a human decide.
- Do not put secrets or API keys in the body (stored in the skodun DB).
