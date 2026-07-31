# When should an automated code-review loop stop? A sourced brief

Research question: given that each round of AI-driven fixes surfaces new findings and the value of another
pass decays, what stopping rules does established practice (human and AI) actually support?

Bottom line up front: there is decent evidence for *diminishing returns with review length/size* and decent
practitioner consensus on *triage-by-severity as the release gate*, but almost no rigorous evidence for
specific round-count caps — those are folklore/heuristics, human and AI alike. The two most quoted numbers
in this space (200–400 LOC, 60–90 minutes) trace to a single vendor case study with no published raw data.

---

## 1. Diminishing returns in code review

**SmartBear "Best Kept Secrets of Peer Code Review" / Cisco study** — [smartbear.com](https://smartbear.com/learn/code-review/best-practices-for-peer-code-review/), original case study PDF: [static0.smartbear.co](https://static0.smartbear.co/support/media/resources/cc/book/code-review-cisco-case-study.pdf), retrospective: [Mike Conley's summary](https://mikeconley.ca/blog/2009/09/14/smart-bear-cisco-and-the-largest-study-on-code-review-ever/)
- What it says: ~10 months, 2,500 reviews, 3.2M LOC at Cisco. Reviewing faster than ~500 LOC/hour causes a
  sharp drop in defect density found. Reviews of 200–400 LOC over 60–90 minutes catch 70–90% of defects.
- Load-bearing: **weak-to-moderate**. This is the actual source of the ubiquitous "200-400 LOC / 60-90
  min" numbers — it is not a misattribution, it really is Cisco/SmartBear's own analysis, packaged into
  Jason Cohen's book and marketing content for their code-review tool (Code Collaborator). But: no raw data
  or methodology has ever been published for independent reanalysis; it's an industrial case study from one
  company using one review tool, not a controlled experiment; and every downstream citation (Atlassian,
  Swarmia, this brief's own search results) traces back to the same secondary write-up, not the primary
  data. Treat the exact numbers as **directionally credible, precisely unverifiable**.

**Basili & Perricone-style inspection-rate findings** — cited (with an inconsistent year, "1984" vs "1993",
across secondary sources) as: raising inspection rate from 50 to 200 LOC/hour dropped detected-fault rate
from 1.6% to 0.6%.
- Load-bearing: **weak, citation itself is shaky**. I could not independently confirm this specific
  LOC/hour-vs-fault-rate figure in Basili & Perricone's actual 1984 CACM paper ("Software Errors and
  Complexity: An Empirical Investigation"), which is primarily about error/complexity correlation, not
  inspection speed. The figure appears to be a garbled secondary citation repeated across blog posts. I'm
  flagging this explicitly rather than presenting it as verified — it is the kind of "everyone cites it,
  nobody can find the original table" number the question asked me to watch for.

**"Reviews longer than ~1 hour hit diminishing returns"** — widely repeated (e.g. in the SmartBear
literature and secondary commentary) as a fatigue/attention effect, independent of code size.
- Load-bearing: **plausible but not independently quantified**. It rests on the same Cisco case study plus
  general vigilance/fatigue research from other domains (proofreading, QA inspection), not a dedicated
  controlled study of code review attention decay.

**Modern large-N studies (more rigorous, but answer a different question)**:
- McIntosh et al., "An Empirical Study of the Impact of Modern Code Review Practices on Software Quality,"
  *Empirical Software Engineering* 2016 — [PDF](https://rebels.cs.uwaterloo.ca/papers/emse2016_mcintosh.pdf).
  Studies Qt, VTK, ITK. Finds review *coverage* and *participation* (not review duration per se) correlate
  with fewer post-release defects; low-coverage or rubber-stamped reviews correlate with more defects. This
  is solid, peer-reviewed, large-N evidence — but it's about whether review happens/how thoroughly, not
  about where a diminishing-returns cutoff sits within a single review.
- Jureczko, "Code review effectiveness: an empirical study on selected factors influence," *IET Software*
  2020 — [Wiley](https://ietresearch.onlinelibrary.wiley.com/doi/full/10.1049/iet-sen.2020.0134). Empirical,
  peer-reviewed; examines what factors (change size, reviewer count, etc.) predict review effectiveness.
  Directionally consistent with "bigger diffs get worse review," but doesn't give a clean cutoff number
  either.
- "Does code review speed matter for practitioners?" *Empirical Software Engineering* 2023 —
  [Springer](https://link.springer.com/article/10.1007/s10664-023-10401-z). Survey of 75 industry + 36 OSS
  practitioners. Finds practitioners overwhelmingly prioritize fast time-to-merge, and separately recognize
  a velocity/quality tension — but this is self-reported belief data, not defect-rate measurement.

**Net assessment on section 1**: the *qualitative* claim (bigger diffs and longer sessions reduce
per-defect detection efficiency) is well supported by both the vendor case study and independent academic
work. The *specific numbers* (200-400 LOC, 60-90 min, 500 LOC/hr ceiling) rest on one unreplicated,
methodologically opaque industry study repackaged for 15+ years of secondary citation. Use the numbers as
an order-of-magnitude anchor, not a precise threshold.

---

## 2. Stopping rules in mature human review practice

**Google Engineering Practices, "The Standard of Code Review"** —
[google.github.io/eng-practices](https://google.github.io/eng-practices/review/reviewer/standard.html)
(canonical doc, mirrored at [github.com/google/eng-practices](https://github.com/google/eng-practices))
- What it says, close to verbatim: reviewers should favor **approving a CL once it is in a state that
  definitely improves the overall code health of the system, even if the CL isn't perfect**, because
  perfection is unattainable and only continuous improvement is the goal. Minor, optional points should be
  prefixed `Nit:` so the author knows they're free to ignore them in this round. Reviewers should give
  "LGTM with comments" — approve now, trust the author to address remaining nits — especially across
  timezones, rather than forcing another round-trip. The doc also explicitly warns against letting a CL
  stall indefinitely over reviewer/author disagreement; it prescribes escalation (discussion, tech lead,
  eng manager) rather than more iteration.
- Load-bearing: **strong — this is the most authoritative, explicit, publicly documented policy in this
  space**. It is a real, current internal-practices document made public, not a blog gloss. It is the
  closest thing to a named, load-bearing "stopping rule" that exists in industry practice: *stop once the
  change is a net improvement; don't hold for perfection; downgrade unresolved minor comments to
  non-blocking rather than iterating again.*

**Chromium / Gerrit conventions** —
[chromium.googlesource.com/.../code_reviews.md](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/code_reviews.md)
- What it says: Code-Review +1/+2 voting; a reviewer may give CR+2 (approve) "with the stated assumption
  that the author will address outstanding minor comments," i.e., approve without forcing a further review
  round on nits. Chromium separately asks reviewers to turn around actionable feedback multiple times per
  day to keep rounds from stretching out. I could not find an explicit Chromium doc capping the *number* of
  rounds (the "nits" convention document Chromium references, `cr_respect.md`/`cl_tips.md`, wasn't
  independently confirmed in this pass).
- Load-bearing: **moderate** — real, current process documentation from a large OSS project, but the
  round-cap claim specifically is unconfirmed; the confirmed part is the same "approve without waiting to
  resolve every minor comment" pattern as Google's doc (unsurprising, same review culture/tooling).

**Explicit numeric "N-round" caps** (e.g. a named "two-round rule")
- I could not find a well-sourced, widely-adopted named convention that hard-caps review at exactly two
  rounds. Searches surfaced blog posts ("Two Simple Rules to Fix Code Reviews," Serce 2025) that use
  "two rules," not "two rounds," and focus on response latency and requiring reviewers to justify comments
  with a "because" clause — not on capping iteration count. I did not find this in Google's or Chromium's
  docs either.
- Load-bearing: **this specific practice appears to be folklore/team-local habit, not a documented
  cross-industry standard.** If your organization uses a "two-round rule," it's a local convention, not
  something traceable to a named, published source. Say so plainly rather than inventing a citation.

**Net assessment on section 2**: the real, well-documented stopping principle in mature teams is not a
round *count* — it's a **quality bar**: approve once the change is a net improvement, demote unresolved
issues to non-blocking/nit status rather than spending another round on them, and escalate (not iterate)
when reviewer and author can't converge. That is a state-based rule, not a counter.

---

## 3. Severity/blast-radius triage as the actual cutoff mechanism

This is where the evidence is most concrete, because it's operationalized in shipping tools with public
docs.

**SonarQube "Clean as You Code"** —
[docs.sonarsource.com, Clean as You Code](https://docs.sonarsource.com/sonarqube-server/10.4/user-guide/clean-as-you-code),
[Quality Gates](https://docs.sonarsource.com/sonarqube-server/10.8/instance-administration/analysis-functions/quality-gates)
- What it says: the default "Sonar way" quality gate evaluates **new code only** (code added/changed since
  a baseline), not the whole legacy codebase. The gate fails on: any new bugs, any new vulnerabilities, any
  unreviewed new security hotspots, insufficient new-code coverage, excessive new-code duplication. Legacy
  issues are explicitly *not* part of the merge gate — they're a separate backlog.
- Load-bearing: **strong as a documented, shipping vendor practice**, and it's exactly the "must-fix before
  merge vs. file a follow-up" split the question asks about — SonarQube operationalizes that split as
  "new-code only" scope + severity thresholds, rather than a round count.

**CodeRabbit's severity-tiered blocking model** — surfaced via CodeRabbit's docs/skills content
([docs.coderabbit.ai](https://docs.coderabbit.ai/changelog), [coderabbit.ai blog on Skills](https://www.coderabbit.ai/blog/coderabbit-skills-code-review))
- What it says: findings are bucketed Critical / Warning / Info. Recommended production pattern: AI review
  runs on every PR, **blocks merge only on Critical findings**, leaves Warning/Info as non-blocking
  suggestions attached to the PR.
- Load-bearing: **vendor doc, not independent evidence** — but directly on-point as a named, current
  industry pattern for exactly this problem (bound the loop by severity, not round count).

**CodeQL/Semgrep-style severity gating** — general industry pattern (CI gates typically fail builds only on
High/Critical, leave Medium/Low as advisory) — this is standard but I did not find a single authoritative
doc worth citing beyond the general static-analysis triage literature; treat as well-established convention
rather than a specific citable source.

**"Error budget" framing** — [Nobl9, Understanding Error Budgets](https://www.nobl9.com/service-level-objectives/error-budget)
- What it says: SRE error-budget thinking (tolerate a bounded amount of imperfection, escalate/halt only
  when budget is exhausted) is a genuine, well-established SRE practice — but it is about production
  reliability, not code review. I found **no established mapping of error-budget thinking onto review
  iteration** in the literature; any use of this frame for a review-stopping-rule would be an analogy you
  are constructing, not an existing named practice. Say so if you use it.

**Net assessment on section 3**: severity/blast-radius triage is the most load-bearing, most concretely
documented cutoff mechanism in the whole brief. The pattern that recurs everywhere (SonarQube, CodeRabbit,
general SAST practice) is the same: **classify by severity, gate merge only on the top tier, route
everything else to a follow-up/backlog rather than another review round.**

---

## 4. Churn/regression risk from late fixes

**Regression bug prevalence**:
- Linux kernel study: ~half of all bugs are regressions (cited via
  [IJSRET review article, 2026](https://ijsret.com/2026/03/25/why-bug-fixes-introduce-new-bugs-a-comprehensive-review-of-regression-defects-in-software-engineering/),
  which itself is a secondary literature review, not primary data — treat the underlying Linux figure as
  needing its own primary-source check if load-bearing for you).
- Chromium: regression bugs cited at ~51% of bugs (same secondary source).
- Incorrect-fix rate: "14.8%–24.4% of fixes for post-release bugs are incorrect and impact end users" —
  again from the same secondary review article's synthesis of prior work.
- Load-bearing: **moderate at best** — I only found this via a 2026 review-article aggregator
  (IJSRET), which synthesizes older empirical papers but wasn't traceable in this pass to the original
  primary studies (e.g., the specific Linux kernel regression paper). The *qualitative* claim — that a
  nontrivial fraction of bug fixes introduce new bugs, and that regressions are common — is well-established
  in the software-engineering literature generally (this is a decades-old, oft-replicated finding), but the
  exact percentages above should be treated as citation-once-removed, not verified primary numbers.
- Directly relevant mechanism: fixes touching multiple files/modules simultaneously are reported as
  significantly more likely to introduce regressions, and inadequate test coverage of adjacent code paths
  is called out as a main root cause. This supports a structural stopping/bounding argument: **the deeper
  into a fix-loop you go, the more you're making compound, less-tested edits, which is exactly the profile
  that produces fix-induced regressions.** This is a reasonable inference from the literature, not a
  number you can cite directly.

**Net assessment on section 4**: the direction of the argument (late-cycle, compounding fixes carry
elevated regression risk, so bounding rounds is prudent) is consistent with what's known about regression
bugs generally, but I don't have a clean, primary, quantified citation tying "review round N" to "regression
probability at round N." This is inference, not measurement — say so if you use it.

---

## 5. AI-specific guidance

This is the newest and thinnest area. Real numbers exist but are inconsistent across sources and mostly
vendor/comparison-site benchmarks, not peer-reviewed work.

- **False positive / precision rates** (comparison-site benchmarks, not peer-reviewed, and not consistent
  with each other across sources): one comparison cites CodeRabbit ~2 false positives per run vs. Greptile
  ~11 in a head-to-head; another cites CodeRabbit precision ~52.5% vs. Copilot ~56.5%; a third (30-PR test
  set) found CodeRabbit made 89 comments, 52 actionable/correct, 23 minor-not-worth-changing, 14
  false-positive/irrelevant (~58% actionable rate). Sources:
  [morphllm.com comparison](https://www.morphllm.com/comparisons/coderabbit-vs-copilot),
  [codeant.ai rankings](https://codeant.ai/blogs/best-ai-code-review-tools),
  [deployhq.com comparison](https://www.deployhq.com/blog/ai-code-review-tools-compared-coderabbit-copilot-sourcery-ellipsis).
  Load-bearing: **weak-to-moderate, vendor-comparison-site benchmarks with unclear/inconsistent
  methodology**, not independent academic evaluation. Directionally: even the best current tools leave
  roughly a third to half of comments non-actionable. That is a real, if imprecise, number worth
  designing around.

- **Understanding the Limits of Automated Evaluation for Code Review Bots in Practice**, arXiv 2026 —
  [arxiv.org/html/2604.24525v1](https://arxiv.org/html/2604.24525v1). What it says: even the
  best-performing LLM-as-judge configuration (G-Eval Likert, GPT-4.1-mini) for scoring bot-review-comment
  usefulness reached only 0.66 precision, and agreement ratios between different automated evaluators
  ranged 0.44–0.62. It explicitly recommends treating automated usefulness scores as **weak signals for
  triage, not as ground truth or an automatic accept/reject decision**, and recommends periodic human
  sampling rather than fully automated acceptance of bot output. It does not address round-capping directly.
  Load-bearing: **the strongest single piece of evidence in this section** — it's a dedicated empirical
  study of exactly the "can we trust automated judgment about code-review-bot output" question, and its
  answer is a caution: don't let a second AI (or the same AI) self-certify that a review round added value;
  precision of automated usefulness-judgment tops out well under 0.7 even with a good judge model.

- **CodeRabbit Skills / severity-blocking pattern** — see section 3; the same Critical/Warning/Info +
  block-on-Critical-only pattern is CodeRabbit's own published guidance for how to keep AI review from
  becoming a merge-blocker on noise.

- **"Bot-replying-to-bot" loop risk** — informally described in secondary commentary
  ([Return on Attention, dev.to](https://dev.to/cseeman/return-on-attention-why-ai-code-reviews-are-wearing-us-out-2hh0))
  as: one model's confidently-wrong finding becomes an unquestioned premise the next model or the next round
  builds on. Load-bearing: **anecdotal/blog opinion**, but it names a real, structurally-plausible failure
  mode specific to *multi-round automated* review that has no equivalent in the human-review literature —
  worth taking seriously even without a controlled study behind it, because the mechanism (context
  contamination across rounds) is well understood generally.

- **No published guidance found on capping AI review iteration counts specifically.** I looked for
  vendor documentation (CodeRabbit, GitHub Copilot review docs, Codex review docs) that states an explicit
  "stop after N rounds" or "stop when marginal findings drop below X" policy, and did not find one. This
  appears to be an open practice gap — you are on the frontier here, not implementing an established
  standard.

**Net assessment on section 5**: AI-review-specific practice is real but young. The one solid empirical
finding (arXiv 2604.24525) argues against a naive "keep looping until a bot says it's clean" design, because
automated usefulness-judgment itself is unreliable. Everything else in this section is vendor marketing,
comparison-site benchmarking, or blog-level pattern-naming — useful for design intuition, not citable as
rigorous evidence.

---

## Concrete stopping rules recommended for an automated review tool

Ranked roughly by how well-evidenced each one is.

1. **Gate merge on severity tier, not on "zero findings."** Block only on Critical/must-fix; route
   Warning/Info to a non-blocking follow-up list. *Evidence: strongest in this brief* — this is exactly
   SonarQube's Clean-as-You-Code gate design and CodeRabbit's production pattern (§3), and it's the
   mechanism that actually lets a loop terminate deterministically instead of chasing an ever-shrinking tail
   of nits.

2. **Scope every re-review pass to new/changed code since the last pass ("clean as you code" applied to
   review rounds), not the whole diff from scratch.** *Evidence: strong* — directly modeled on SonarQube's
   new-code-only baseline (§3); also reduces the chance each round re-litigates already-accepted code,
   which is the main way review loops fail to converge.

3. **Stop once a fix round produces only Nit-tier findings, and downgrade any remaining Nit-tier findings
   to non-blocking rather than issuing another round.** *Evidence: strong* — this is Google's explicit,
   named, current policy: approve once the change is a net improvement; don't hold for perfection (§2).
   Mechanically: if round N's new findings are all sub-Critical, stop after round N rather than doing round
   N+1 to fix them.

4. **Cap total rounds at a small fixed number (e.g., 2–3) as a hard backstop, independent of findings —
   but treat this as a pragmatic safety valve, not an evidence-backed threshold.** *Evidence: weak* — I
   found no well-sourced "two-round rule" or similar named convention (§2); Google's docs instead prescribe
   escalation, not iteration, when convergence stalls. Recommend this anyway as a backstop against infinite
   loops, but label it internally as a heuristic, not cite it as best practice.

5. **Don't let the same automated reviewer self-certify a fix round as sufficient; if you use an
   LLM-as-judge step to decide "is this round's output actionable," treat its verdict as a weak triage
   signal and sample/spot-check rather than fully trusting it.** *Evidence: moderate-to-strong for the
   caution, from a dedicated empirical source* — arXiv 2604.24525 found automated usefulness-judgment tops
   out around 0.66 precision even with a strong judge model (§5).

6. **Weight cutoff sensitivity by diff size/complexity, not just round count**: large or multi-file fix
   diffs should get *more* scrutiny before accepting them as final, not less, because of elevated
   fix-induced-regression risk. *Evidence: moderate* — supported by the regression-bug literature's
   observation that multi-file fixes are more regression-prone (§4), though I don't have a precise
   quantified threshold to cite here (this is inference from the direction of the evidence, not a number).

7. **Do not lean on "200-400 LOC / 60-90 minutes" or "500 LOC/hour" as literal thresholds for an automated
   tool.** *Evidence caveat, not a rule*: those numbers come from one unreplicated industrial case study
   with no public raw data (§1). They're fine as a rough intuition pump ("very large diffs get worse
   review") but would be over-claiming if presented as calibrated, precise limits in a tool's design
   rationale.

---

## Honesty notes on where practice is thin

- No one — not Google, not Chromium, not any AI review vendor found in this search — publishes an explicit,
  evidence-backed **round-count** cap. Every real, documented stopping mechanism found here is
  **state-based** (severity tier reached, code-health bar cleared, new-code scope exhausted), not
  **counter-based** (stop after N rounds). If your tool needs a hard round cap for practical/infra reasons
  (bounding cost, avoiding infinite loops), that's a legitimate engineering decision, but it should be
  labeled as a pragmatic backstop, not attributed to industry best practice.
- The 200-400 LOC / 60-90 minute numbers are real (they come from an actual Cisco/SmartBear case study,
  not an invented factoid), but they are a single, methodologically opaque, unreplicated source that the
  entire industry re-cites. Treat precision claims built on them skeptically.
- The Basili/Perricone "50→200 LOC/hour, 1.6%→0.6% fault rate" figure could not be confirmed against a
  primary source in this pass and is likely a garbled secondary citation; I'm flagging rather than
  presenting it as fact.
- AI-review-specific benchmarks (false-positive/precision rates by tool) come from comparison/marketing
  sites with inconsistent numbers across sources — useful for order-of-magnitude intuition ("expect roughly
  a third to half of bot comments to be non-actionable") but not for precise design thresholds.
- Regression/fix-induced-defect percentages in §4 trace through a 2026 secondary review article; the
  qualitative claim is well-established in SE literature generally, but I did not verify the specific
  percentages against primary studies in this pass.
