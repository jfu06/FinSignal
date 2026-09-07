# Lessons Learned — Building a Trustworthy LLM System, Eval-First

FinSignal was built in an intense AI-pair-programming loop with an external
adversarial reviewer in the cycle. Sixteen review rounds surfaced real
failures that internal evals had passed. This file records what broke, what
weakness of AI-built systems each failure exposes, and the general fixes
that worked — with receipts.

## What broke (and what it exposes)

### 1. Plausible code, wrong in the domain corners

- A **proxy statement's 1000×-mis-scaled fact outvoted the 10-K** in
  dedup-by-latest-filed → the UI printed \$8.9M net income (true: \$8.85B)
  and a 0.0% margin, under an "official data" banner.
- **Tag migration**: "latest = first registry tag with data" shipped a
  four-year-old revenue as current (an 8× error) when NVIDIA moved to a new
  XBRL tag. No eval covered it.
- **Fiscal-year labels run ahead of the calendar**: the router intermittently
  refused an already-filed FY as "forward-looking".
- A **CAGR across a buyback halt-and-restart** (+60.8%): arithmetically true,
  narratively a lie.

*Exposed:* AI writes happy-path code that is correct in general and wrong in
the corners only domain experience knows about.

### 2. Prompts are global variables with no type system

One sentence added to raise informativeness ("prefer specific facts") pushed
generation past its evidence → the faithfulness gate failed at 4.65% (from
0.7%). Nothing else changed; no test failed until the gate ran.

### 3. LLM components are stochastic; gates flake

The same golden question routed correctly in one gate run and wrongly in the
next. An informativeness judge swung 0% → 23% → 37% across runs — the real
root cause was missing context (it never saw the question the claim
answered), but only repeated measurement separated bias from variance.

### 4. Platform semantics that only production teaches

- Streamlit Cloud hot-reloads the UI script but **caches imported modules**
  — new UI + stale backend shipped an ImportError and, worse, a
  new-copy/old-logic hybrid. Hit three times before it became a rule.
- Streamlit "magic" rendered a bare ternary's `None` five times into the UI.
- Chrome's `sidePanel.open()` dies if any `await` precedes it in the
  gesture chain.

### 5. Self-grading is blind; metrics measure what they measure

- 0% unsupported rate while an answer was six verified pieces of boilerplate
  ("success depends on innovation") — faithful and useless.
- A 200-char citation preview cut off the supporting sentence, making a
  *correct* citation look fabricated.
- The retrieval-hit smoke metric failed companies whose answers were
  flawless, because MMR diversification (a deliberate improvement)
  legitimately lowered an internal metric.

## What worked

| Practice | Receipt |
|---|---|
| **Eval-first release gate** — prompt/retrieval changes never ship without passing the 36-case golden set | Caught the 4.65% faithfulness regression; steady state 0–2% unsupported |
| **Determinism where correctness matters** — the numeric channel has no LLM; prompts *request*, code *enforces* (routing contracts, sanity bounds, as-of anchors) | Silent metric substitution and out-of-bounds ratios became structurally impossible |
| **Cross-checking layers with disjoint blind spots** — cross-vendor judge + deterministic re-checks + numeric probes | The probes caught the 8× stale-tag bug on their first run |
| **Every incident becomes a regression test** | 246 → 295 tests over the project; no incident has recurred |
| **An external adversarial reviewer in the loop** | 16 rounds; every round found something internal evals had passed |
| **Failure as a designed state** — refusals carry facts + a stated boundary; technical faults say so plainly | The two worst outcomes (crash, fabrication) are excluded by construction |
| **Add a second metric axis when the first saturates** | Boilerplate rate (question-aware) joined the gate once unsupported hit 0%; 37% → ~10–20% |
| **Write operational pitfalls down as rules** | "app/ change ⇒ Reboot"; "never run the gate concurrently with ingestion" (shared connection pool produced 29 phantom crashes once) |

## The one-sentence version

The risk of AI-assisted coding is not that the model can't write code — it's
that the code is quietly wrong on dimensions neither of you thought to test.
The countermeasures are structural, not prompt-craft: **de-LLM the critical
path, layer verifiers with disjoint blind spots, gate every change, turn
every incident into a test, and keep an outside pair of eyes in the loop.**
