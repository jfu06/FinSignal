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
from app.digest import DIGEST_QUERY_COST, digest_summary, generate_digest  # noqa: E402
from app.followup import condense_followup  # noqa: E402
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

history: list[dict] = st.session_state.setdefault("chat", [])


def render_report(report: dict, key: str) -> None:
    """Render one verified report inside an assistant chat bubble."""

    if report.get("needs_onboarding"):
        st.info(report.get("message", "Company not onboarded yet."))
        return

    if report.get("supported", True) is False:
        st.warning(
            "Numeric/aggregation questions (averages, growth rates, CAGR, "
            "ratios) aren't supported yet — that's the Phase-2 structured "
            "metrics layer. Try a descriptive question instead.",
            icon="⛔",
        )
        return

    # Verified figures (Phase-2 numeric line): deterministic math over
    # official XBRL filings — every number links to its SEC filing.
    for r in report.get("numeric", []):
        st.info(f"🔢 {r['text']}")
        links = " · ".join(
            f"[{s['form']} {s['accn']}]({s['url']})" for s in r["sources"][:3]
        )
        if links:
            st.caption(f"Official filing data (deterministic, no LLM): {links}")

    if report["summary"] and not (report.get("numeric")
                                  and not report["claims"]):
        st.markdown(report["summary"])
    ok_claims = [c for c in report["claims"] if not c["unverified"]]
    warn_claims = [c for c in report["claims"] if c["unverified"]]
    st.caption(
        f"{len(report['claims'])} claims · {len(ok_claims)} verified · "
        f"{len(warn_claims)} unverified · unsupported rate "
        f"{report['unsupported_rate']:.0%} · {report.get('latency_s', 0):.0f}s"
    )

    for c in ok_claims:
        render_claim(c)
    if warn_claims:
        acknowledged = st.checkbox(
            f"Show {len(warn_claims)} unverified claim(s) — I understand they "
            f"could not be sufficiently grounded in the filing.",
            key=f"ack_{key}",
        )
        if acknowledged:
            for c in warn_claims:
                render_claim(c)
    if report["blocked_claims"]:
        st.error(
            f"⛔ {len(report['blocked_claims'])} claim(s) contradicting the "
            f"filing were blocked and are not shown.",
            icon="⛔",
        )
    if report["risk_flags"]:
        st.markdown("**Risk flags**")
        for r in report["risk_flags"]:
            st.markdown(f"- 🚩 {r}")

    retr = report.get("retrieval", {})
    with st.expander("🔍 Retrieval trace (agentic refinement loop)"):
        st.markdown(
            f"- Rounds: **{retr.get('rounds', 1)}** · decisions: "
            f"**{' → '.join(retr.get('decisions', [])) or 'ENOUGH'}**\n"
            f"- Final query: `{retr.get('final_query', '')[:120]}` · "
            f"top-k: **{retr.get('final_k', '-')}**\n"
            f"- Chunks: {', '.join(report.get('retrieved_chunk_ids', []))}"
        )
    st.download_button(
        "⬇️ Export report (JSON)",
        data=json.dumps(report, ensure_ascii=False, indent=2),
        file_name=f"finsignal_{report['ticker']}_{report['query_id']}.json",
        mime="application/json",
        key=f"dl_{key}",
    )
    st.caption(report["disclaimer"])


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


def render_digest(digest: dict, key: str) -> None:
    st.markdown(f"### 📊 {digest['ticker']} — Annual report digest")
    st.caption(
        f"Generated in {digest.get('latency_s', 0):.0f}s. Figures are "
        f"official filing data (deterministic); every narrative claim below "
        f"is independently verified."
    )
    for r in digest.get("figures", []):
        st.info(f"🔢 {r['text']}")
        links = " · ".join(
            f"[{s['form']} {s['accn']}]({s['url']})" for s in r["sources"][:2])
        if links:
            st.caption(links)
    for sec in digest.get("sections", []):
        st.markdown(f"#### {sec['title']}")
        if "error" in sec:
            st.error(f"This section failed to generate: {sec['error'][:120]}")
            continue
        render_report(sec["report"], key=sec["report"]["query_id"])


# --- replay the conversation ---
for i, turn in enumerate(history):
    with st.chat_message("user"):
        st.markdown(f"**[{turn['ticker']}]** {turn['question']}")
        if turn.get("standalone") and turn["standalone"] != turn["question"]:
            st.caption(f"Interpreted as: {turn['standalone']}")
    with st.chat_message("assistant"):
        if turn.get("error"):
            st.error(turn["error"])
        elif turn.get("digest"):
            render_digest(turn["digest"], key=f"digest_{i}")
        else:
            render_report(turn["report"], key=turn["report"]["query_id"])

if not history:
    st.caption(
        "Ask anything about the selected company's 10-K — e.g. "
        "\u201cWhat drove revenue growth last year? Any risk factors?\u201d "
        "Follow-up questions are welcome; each answer is claim-checked "
        "against the filing."
    )

# --- one-click digest (cold-start entry point) ---
if st.button(f"📊 Generate {ticker} annual report digest (~2 min)",
             use_container_width=True):
    if len(history) >= settings().session_query_limit:
        st.warning("Session limit reached — refresh the page to start over.")
    elif daily_budget_left(settings().log_path,
                           settings().daily_query_budget) < DIGEST_QUERY_COST:
        st.warning("Not enough daily budget left for a digest — try tomorrow.")
    else:
        with st.chat_message("user"):
            st.markdown(f"**[{ticker}]** 📊 Annual report digest")
        with st.chat_message("assistant"):
            with st.status("Building digest…", expanded=True) as s:
                try:
                    d = generate_digest(ticker, settings=settings(),
                                        progress=st.write)
                    s.update(label=f"Digest ready ({d['latency_s']:.0f}s)",
                             state="complete")
                    history.append({
                        "question": "📊 Annual report digest",
                        "ticker": ticker, "digest": d,
                        "summary_for_context": digest_summary(d),
                    })
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    s.update(label="Digest failed", state="error")
                    st.error(f"Digest failed, please retry. ({exc})")

prompt = st.chat_input(f"Ask about {ticker} or ANY company… (any language, follow-ups OK)")
if prompt and prompt.strip():
    prompt = prompt.strip()
    # --- usage guardrails: per-session limit + global daily budget ---
    if len(history) >= settings().session_query_limit:
        st.warning(
            f"Session limit reached ({settings().session_query_limit} "
            f"questions). Refresh the page to start a new session."
        )
    elif daily_budget_left(settings().log_path, settings().daily_query_budget) <= 0:
        st.warning(
            "Today's global query budget is used up — please come back "
            "tomorrow. (This demo caps daily LLM spend.)"
        )
    else:
        with st.chat_message("user"):
            st.markdown(f"**[{ticker}]** {prompt}")
        with st.chat_message("assistant"):
            with st.spinner("Retrieve → generate → batch-verify… (typically < 60 s)"):
                turn: dict = {"question": prompt, "ticker": ticker}
                try:
                    qa_history = []
                    for t in history:
                        if t.get("digest"):
                            qa_history.append(
                                {"question": t["question"],
                                 "summary": t.get("summary_for_context", "")})
                        elif t.get("report") and t["report"].get("supported", True):
                            qa_history.append(
                                {"question": t["question"],
                                 "summary": t["report"]["summary"]})
                    standalone = condense_followup(
                        prompt, qa_history, settings=settings()
                    )
                    turn["standalone"] = standalone
                    report = answer_question(
                        standalone, ticker, settings=settings()
                    )
                    if report.get("needs_onboarding"):
                        # Question mentions a company we don't have yet:
                        # onboard it inline, then answer for real.
                        tk = report["needs_onboarding"]
                        st.write(f"➕ {tk} isn't in the corpus — adding it…")
                        ensure_ticker(tk, settings=settings(),
                                      progress=st.write)
                        tickers.clear()
                        turn["ticker"] = tk
                        report = answer_question(
                            standalone, tk, settings=settings()
                        )
                    turn["report"] = report
                except PipelineError as exc:
                    turn["error"] = f"Invalid input: {exc}"
                except Exception as exc:  # noqa: BLE001
                    turn["error"] = f"Analysis failed, please retry. ({exc})"
        history.append(turn)
        st.rerun()
