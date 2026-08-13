"""FinSignal — Streamlit demo UI.

Implements the three UI-dependent credibility requirements from
requirements.md on top of the existing pipeline (zero pipeline changes):

- Click-through citations: every claim expands to the exact source chunk.
- WARNING acknowledgment: unverified claims stay hidden until the user
  explicitly acknowledges they are unverified.
- Report export: download the full report as JSON.

Plus: on-demand company onboarding from SEC EDGAR (~1 min) and an optional
self-supervised smoke health-check per onboarded company.

Run (from backend/):
    streamlit run ui/app.py
"""

from __future__ import annotations

import hmac
import json
import sys
from pathlib import Path

import streamlit as st

# Allow `import app.*` when launched via `streamlit run ui/app.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.edgar import EdgarError  # noqa: E402
from app.onboarding import OnboardingError, ensure_ticker, known_tickers  # noqa: E402
from app.pipeline import PipelineError, answer_question  # noqa: E402
from app.smoke_eval import load_smoke_result, run_smoke_eval  # noqa: E402
from app.usage import daily_budget_left  # noqa: E402

st.set_page_config(page_title="FinSignal", page_icon="📑", layout="wide")

EVAL_COVERED = {"AAPL", "MSFT", "TSLA"}  # tickers the offline golden set covers


@st.cache_resource
def settings():
    return get_settings()


# --- access gate (public-deployment guardrail; disabled when no ACCESS_CODE) ---
_s = settings()
if _s.access_code and not st.session_state.get("authed"):
    st.title("📑 FinSignal")
    st.caption("This demo is access-protected to keep API costs bounded.")
    code = st.text_input("Access code", type="password")
    if st.button("Enter", type="primary"):
        if hmac.compare_digest(code.strip(), _s.access_code):
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("Wrong access code.")
    st.stop()


@st.cache_data(ttl=600)
def tickers() -> list[str]:
    return sorted(known_tickers(settings()))


def _smoke_button(label: str, key: str, target: str) -> None:
    if st.button(label, use_container_width=True, key=key):
        with st.status("Running smoke check…", expanded=True) as s:
            try:
                r = run_smoke_eval(target, settings=settings(), progress=st.write)
                s.update(
                    label="Health check passed ✅" if r["passed"]
                    else "Health check FAILED ⚠️",
                    state="complete" if r["passed"] else "error",
                )
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                s.update(label="Health check error", state="error")
                st.error(str(exc))


# ----------------------------- sidebar -----------------------------------
with st.sidebar:
    st.title("📑 FinSignal")
    st.caption("Verifiable, citation-grounded 10-K Q&A — Phase 1 (RAG line)")
    ticker = st.selectbox("Company (ticker)", tickers())

    if ticker not in EVAL_COVERED:
        smoke = load_smoke_result(ticker)
        if smoke and smoke.get("passed"):
            st.caption(
                f"🩺✅ Automated smoke check passed: evidence hit rate "
                f"{smoke['retrieval_hit_rate']:.0%}, unsupported rate "
                f"{smoke['unsupported_rate']:.0%}. (Not yet covered by the "
                f"human-labeled eval set.)"
            )
        elif smoke:
            st.warning(
                f"🩺 Smoke check FAILED (evidence hit rate "
                f"{smoke['retrieval_hit_rate']:.0%}, unsupported rate "
                f"{smoke['unsupported_rate']:.0%}, crashed questions "
                f"{smoke['n_crashed']}). Treat answers for this company "
                f"with caution."
            )
            _smoke_button("🩺 Re-run smoke check (~2-4 min)", "rerun_smoke", ticker)
        else:
            st.caption(
                "ℹ️ Added on demand — not yet evaluated. Every answer is still "
                "claim-checked, but consider running the health check."
            )
            _smoke_button("🩺 Run smoke check (~2-4 min)", "run_smoke", ticker)

    # --- on-demand onboarding: any US-listed ticker via SEC EDGAR ---
    with st.expander("➕ Add a company"):
        new_ticker = st.text_input(
            "US ticker", placeholder="e.g. NVDA", max_chars=10, key="new_ticker"
        )
        if st.button("Fetch 10-K from SEC EDGAR (~1 min)",
                     use_container_width=True):
            if not new_ticker.strip():
                st.error("Please enter a ticker symbol.")
            else:
                with st.status(f"Adding {new_ticker.upper()}…",
                               expanded=True) as s:
                    try:
                        result = ensure_ticker(
                            new_ticker, settings=settings(), progress=st.write
                        )
                        if result["status"] == "exists":
                            s.update(label="Already available — ask away.",
                                     state="complete")
                        else:
                            s.update(
                                label=(
                                    f"✅ {result['company']} — "
                                    f"{result['chunks']} chunks ingested. "
                                    f"Ready to query."
                                ),
                                state="complete",
                            )
                        tickers.clear()          # refresh the dropdown
                        st.rerun()
                    except (EdgarError, OnboardingError) as exc:
                        s.update(label="Onboarding failed", state="error")
                        st.error(str(exc))
                    except Exception as exc:  # noqa: BLE001
                        s.update(label="Onboarding failed", state="error")
                        st.error(f"Something went wrong, please retry: {exc}")

    st.divider()
    st.markdown(
        "**How this differs from a plain AI summarizer**\n\n"
        "Every claim is independently verified in one batch judge call:\n"
        "- ✅ Supported by the filing → shown with click-through citations\n"
        "- ⚠️ Insufficient evidence → marked *unverified*, shown only after "
        "you acknowledge\n"
        "- ⛔ Contradicts the filing → blocked, never displayed\n"
    )
    st.divider()
    st.caption(
        "For reference only. Generated automatically from cited public-filing "
        "excerpts. Not investment advice."
    )

# ----------------------------- main --------------------------------------
st.header("Ask the 10-K")

with st.form("ask"):
    question = st.text_input(
        "Your question (any language)",
        placeholder="e.g. What drove revenue growth last year? Any risk factors?",
    )
    submitted = st.form_submit_button("Analyze", type="primary")

if submitted and question.strip():
    # --- usage guardrails: per-session limit + global daily budget ---
    asked = st.session_state.get("questions_asked", 0)
    if asked >= settings().session_query_limit:
        st.warning(
            f"Session limit reached ({settings().session_query_limit} questions). "
            f"Refresh the page to start a new session."
        )
    elif daily_budget_left(settings().log_path, settings().daily_query_budget) <= 0:
        st.warning(
            "Today's global query budget is used up — please come back tomorrow. "
            "(This demo caps daily LLM spend.)"
        )
    else:
        st.session_state["questions_asked"] = asked + 1
        with st.spinner("Retrieve → generate → batch-verify… (typically < 60 s)"):
            try:
                st.session_state["report"] = answer_question(
                    question, ticker, settings=settings()
                )
            except PipelineError as exc:
                st.session_state.pop("report", None)
                st.error(f"Invalid input: {exc}")
            except Exception as exc:  # noqa: BLE001 — surface, don't crash the app
                st.session_state.pop("report", None)
                st.error(f"Analysis failed, please retry. ({exc})")

report = st.session_state.get("report")
if not report:
    st.stop()

# --- numeric-boundary reply ---
if report.get("supported", True) is False:
    st.warning(
        "Numeric/aggregation questions (averages, growth rates, CAGR, ratios) "
        "aren't supported yet — that's the Phase-2 structured metrics layer. "
        "Please ask a descriptive question instead, e.g. “What drove revenue "
        "growth?”",
        icon="⛔",
    )
    st.stop()

# --- summary + metrics ---
st.subheader("Key insights")
st.info(report["summary"])

ok_claims = [c for c in report["claims"] if not c["unverified"]]
warn_claims = [c for c in report["claims"] if c["unverified"]]
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Claims", len(report["claims"]))
m2.metric("Verified", len(ok_claims))
m3.metric("Unverified ⚠️", len(warn_claims))
m4.metric("Unsupported rate", f"{report['unsupported_rate']:.0%}")
m5.metric("Latency", f"{report.get('latency_s', 0):.0f} s")


def render_claim(c: dict) -> None:
    box = st.warning if c["unverified"] else st.success
    kind_badge = "🚩 Risk" if c["kind"] == "risk" else "💡 Insight"
    verified_badge = "⚠️ Unverified" if c["unverified"] else "✅ Verified"
    box(f"{c['text']}")
    meta = f"{kind_badge} · {verified_badge} · credibility {c['credibility']:.1f}"
    span = c.get("span_overlap") or {}
    if span.get("flagged"):
        meta += " · ⚑ figures not found verbatim in citations — review advised"
    elif span.get("number_match") == 1.0:
        meta += " · 🔢 figures match citations"
    st.caption(meta)
    if c["unverified"] and c.get("judge_reason"):
        st.caption(f"Why unverified: {c['judge_reason']}")
    for cite in c["citations"]:
        section = cite.get("section") or "(no section label)"
        with st.expander(f"📄 View source — {cite['chunk_id']} · {section}"):
            st.text(cite["preview"])
    st.divider()


# --- verified claims ---
st.subheader("Findings (verified)")
if ok_claims:
    for c in ok_claims:
        render_claim(c)
else:
    st.caption(
        "No verified claims this time — the filing may not contain the "
        "requested information. The system does not invent answers."
    )

# --- WARNING claims: hidden until acknowledged (requirements.md rule) ---
if warn_claims:
    st.subheader(f"Unverified claims ({len(warn_claims)})")
    acknowledged = st.checkbox(
        "I understand: the claims below could **not be sufficiently grounded "
        "in the filing**. Lower credibility — for reference only.",
        key="ack_warnings",
    )
    if acknowledged:
        for c in warn_claims:
            render_claim(c)
    else:
        st.caption("Check the box above to reveal them.")

# --- blocked claims: never displayed, count only ---
if report["blocked_claims"]:
    st.error(
        f"⛔ {len(report['blocked_claims'])} claim(s) contradicting the filing "
        f"were blocked and are not shown (recorded for offline review).",
        icon="⛔",
    )

# --- risk flags ---
if report["risk_flags"]:
    st.subheader("Risk flags")
    for r in report["risk_flags"]:
        st.markdown(f"- 🚩 {r}")

# --- how the answer was produced (agentic trace) ---
retr = report.get("retrieval", {})
with st.expander("🔍 Retrieval trace (agentic refinement loop)"):
    st.markdown(
        f"- Rounds: **{retr.get('rounds', 1)}**\n"
        f"- Assessor decisions: **{' → '.join(retr.get('decisions', [])) or 'ENOUGH'}**\n"
        f"- Final query: `{retr.get('final_query', '')[:120]}`\n"
        f"- Final top-k: **{retr.get('final_k', '-')}**\n"
        f"- Retrieved chunks: {', '.join(report.get('retrieved_chunk_ids', []))}"
    )

# --- export (requirements.md: report export) ---
st.download_button(
    "⬇️ Export report (JSON)",
    data=json.dumps(report, ensure_ascii=False, indent=2),
    file_name=f"finsignal_{report['ticker']}_{report['query_id']}.json",
    mime="application/json",
)

st.caption(report["disclaimer"])
