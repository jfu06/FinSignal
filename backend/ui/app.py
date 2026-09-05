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
import html as html_lib
import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@500&display=swap');
html, body, [data-testid="stAppViewContainer"] * {
  font-family: 'Inter', -apple-system, 'PingFang SC', 'Noto Sans SC', sans-serif;
}
code, pre, .fs-mono { font-family: 'IBM Plex Mono', ui-monospace, monospace; }
/* the blanket font override must NOT catch Streamlit's icon font */
[data-testid="stIconMaterial"], .material-icons {
  font-family: 'Material Symbols Rounded' !important;
}

.fs-brand { font-size: 1.65rem; font-weight: 700; letter-spacing: -0.02em; }
.fs-brand span { color: #0E7A6E; }
.fs-tagline { color: #5D6771; font-size: 0.86rem; line-height: 1.45; margin-top: 2px; }

.fs-badge {
  display: inline-block; font-size: 0.74rem; font-weight: 600;
  padding: 3px 10px; border-radius: 99px; margin: 0 6px 10px 0;
  letter-spacing: 0.01em;
}
.fs-badge-num { background: #F5EBDD; color: #8A5210; }
.fs-badge-doc { background: #E4F1EF; color: #0E6A60; }

.fs-num {
  display: flex; gap: 10px; align-items: baseline;
  background: #FBF6EE; border: 1px solid #EAD9BE; border-left: 3px solid #C08A3E;
  border-radius: 8px; padding: 10px 14px; margin: 4px 0 2px;
  font-size: 0.95rem; font-weight: 500;
}

.fs-claim {
  border: 1px solid #DDE3DD; border-left: 3px solid #2F7D4F;
  border-radius: 8px; padding: 10px 14px; margin: 8px 0 2px;
  background: #FFFFFF; font-size: 0.93rem; line-height: 1.5;
}
.fs-claim.warn { border-left-color: #C99A3B; background: #FDFBF4; }
.fs-claim-meta { color: #75808A; font-size: 0.76rem; margin-top: 6px; }

.fs-cat {
  font-size: 0.78rem; font-weight: 700; letter-spacing: 0.03em;
  text-transform: uppercase; margin: 4px 0 6px;
}
.fs-cat small { display:block; font-weight: 400; text-transform: none;
  letter-spacing: 0; color: #75808A; margin-top: 1px; }
.fs-cat.num { color: #8A5210; }
.fs-cat.doc { color: #0E6A60; }
.fs-cat.hyb { color: #4A5490; }
.fs-step { color: #5D6771; font-size: 0.9rem; margin-bottom: 10px; }
.fs-step b { color: #1F262B; }
</style>
""", unsafe_allow_html=True)

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
    st.markdown(
        '<div class="fs-brand">Fin<span>Signal</span></div>'
        '<div class="fs-tagline">SEC-filings Q&A you can check. Numbers are '
        'computed from official XBRL data — never written by an LLM. Every '
        'written claim is verified against the filing.</div>',
        unsafe_allow_html=True,
    )
    st.write("")
    ticker = st.selectbox("1️⃣ Pick a company", tickers(),
                          help="Every question below is answered for this "
                               "company unless you name another one.")

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

    # Route badge: sets latency expectations and shows WHERE the answer
    # came from (computed vs read) — the product's core differentiation.
    has_numeric = bool(report.get("numeric"))
    has_claims = bool(report.get("claims"))
    badges = ""
    if has_numeric:
        badges += ('<span class="fs-badge fs-badge-num">🔢 Computed · '
                   'official SEC XBRL data · zero-LLM math</span>')
    if has_claims or not has_numeric:
        badges += ('<span class="fs-badge fs-badge-doc">📄 From the filing · '
                   'every claim verified</span>')
    st.markdown(badges, unsafe_allow_html=True)

    # Verified figures (numeric line): deterministic math over official
    # XBRL filings — every number links to its SEC filing.
    for r in report.get("numeric", []):
        st.markdown(
            f'<div class="fs-num"><span>🔢</span>'
            f'<span>{html_lib.escape(r["text"])}</span></div>',
            unsafe_allow_html=True,
        )
        seen_accn: set[str] = set()
        links = " · ".join(
            f"[{s['form']} {s['accn']}]({s['url']})"
            for s in r["sources"]
            if s["accn"] not in seen_accn and not seen_accn.add(s["accn"])
        )
        # Exact XBRL concept + full-precision reported value: search the tag
        # in the iXBRL viewer the link opens, then match the digits — the
        # consolidated fact matches exactly; dimensional slices (e.g.
        # Product-only revenue under the same tag) won't.
        def _fact(s: dict) -> str:
            out = f"`{s['tag']}`"
            if s.get("raw_value") is not None:
                v = s["raw_value"]
                out += f" = {v:,.0f}" if v == int(v) else f" = {v:,.2f}"
            return out + f" ({s['period_end']})"
        tags = " · ".join(dict.fromkeys(
            _fact(s) for s in r["sources"] if s.get("tag")))
        if links:
            st.caption(f"Source: {links}" + (f" — {tags}" if tags else ""))

    if report["summary"] and not (report.get("numeric")
                                  and not report["claims"]):
        st.markdown(report["summary"])
    ok_claims = [c for c in report["claims"] if not c["unverified"]]
    warn_claims = [c for c in report["claims"] if c["unverified"]]
    if report["claims"]:
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
    if not report.get("retrieved_chunk_ids"):
        retr = None  # pure-numeric answer: no retrieval happened
    if retr is not None:
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
    kind_badge = "🚩 Risk" if c["kind"] == "risk" else "💡 Insight"
    verified_badge = "⚠️ Unverified" if c["unverified"] else "✅ Verified"
    meta = f"{kind_badge} · {verified_badge} · credibility {c['credibility']:.1f}"
    span = c.get("span_overlap") or {}
    if span.get("flagged"):
        meta += " · ⚑ figures not found verbatim in citations — review advised"
    elif span.get("number_match") == 1.0:
        meta += " · 🔢 figures match citations"
    cls = "fs-claim warn" if c["unverified"] else "fs-claim"
    st.markdown(
        f'<div class="{cls}">{html_lib.escape(c["text"])}'
        f'<div class="fs-claim-meta">{meta}</div></div>',
        unsafe_allow_html=True,
    )
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
        st.markdown(
            f'<div class="fs-num"><span>🔢</span>'
            f'<span>{html_lib.escape(r["text"])}</span></div>',
            unsafe_allow_html=True,
        )
        seen_accn: set[str] = set()
        links = " · ".join(
            f"[{s['form']} {s['accn']}]({s['url']})"
            for s in r["sources"]
            if s["accn"] not in seen_accn and not seen_accn.add(s["accn"])
        )
        if links:
            st.caption(links)
    for sec in digest.get("sections", []):
        st.markdown(f"#### {sec['title']}")
        if "error" in sec:
            st.error(f"This section failed to generate: {sec['error'][:120]}")
            continue
        render_report(sec["report"], key=sec["report"]["query_id"])


# Map pipeline events (jsonl log) to user-facing progress-stage labels.
_STAGES = [
    ("routed",          "🧭 Question routed"),
    ("retrieval",       "🔍 Searching the filing…"),
    ("assess",          "🤔 Checking whether the evidence suffices…"),
    ("generation",      "✍️ Writing the answer — every claim must cite its source…"),
    ("judge",           "🧑‍⚖️ Cross-checking every claim against the cited text…"),
    ("regeneration",    "♻️ A claim contradicted the filing — regenerating once…"),
]


def _stage_for(event: dict) -> str | None:
    name = event.get("event", "")
    stage = event.get("stage", "")
    if name == "routed":
        route = event.get("route", "narrative")
        return ("🧭 Routed → numeric: computing from official filing data…"
                if route == "numeric" else f"🧭 Routed → {route}")
    if name in ("retrieval", "retrieval_refined"):
        return "🔍 Searching the filing…"
    if name == "regeneration_triggered":
        return "♻️ A claim contradicted the filing — regenerating once…"
    if name == "llm_call":
        for key, label in _STAGES:
            if stage == key:
                return label
    return None


def answer_with_progress(question: str, tk: str) -> dict:
    """Run answer_question in a thread while tailing the event log so the
    user sees real pipeline stages instead of a blind spinner. Any failure
    of the progress machinery falls back to a plain blocking call."""

    qid = f"ui{uuid.uuid4().hex[:8]}"
    log_path = settings().log_path
    try:
        offset = log_path.stat().st_size if log_path.exists() else 0
    except OSError:
        return answer_question(question, tk, settings=settings())

    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(answer_question, question, tk,
                          settings=settings(), query_id=qid)
        with st.status("🧭 Routing the question…", expanded=True) as s:
            seen: set[str] = set()
            try:
                while not fut.done():
                    time.sleep(0.4)
                    try:
                        with open(log_path, encoding="utf-8") as fh:
                            fh.seek(offset)
                            new_text = fh.read()
                            offset = fh.tell()
                    except OSError:
                        continue
                    for line in new_text.splitlines():
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if str(ev.get("query_id") or "").startswith(qid):
                            label = _stage_for(ev)
                            if label and label not in seen:
                                seen.add(label)
                                st.write(label)
                                s.update(label=label)
            except Exception:  # noqa: BLE001 — progress is cosmetic
                pass
            report = fut.result()  # re-raises pipeline errors to the caller
            s.update(
                label=f"Done in {report.get('latency_s', 0):.0f}s",
                state="complete", expanded=False,
            )
    return report


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
    st.markdown(
        f'<div class="fs-step"><b>1️⃣ Pick a company</b> in the sidebar '
        f'(currently <b>{ticker}</b>) &nbsp;→&nbsp; '
        f'<b>2️⃣ Start from a common question</b> below, or type your own '
        f'in any language.</div>',
        unsafe_allow_html=True,
    )
    # Generic templates — no company names: the selected ticker is filled
    # in automatically, so switching companies never leaves a stale example.
    _CATEGORIES = [
        ("num", "🔢 Figures — computed",
         "official XBRL data, ~3 s",
         ["What was last fiscal year's revenue, and how fast did it grow?",
          "What is the 3-year trend in gross margin?",
          "How much cash was spent on share buybacks?"]),
        ("doc", "📄 From the filing — verified",
         "every claim cited & checked, ~40 s",
         ["What are the biggest risk factors?",
          "What drove revenue growth last year?",
          "How does management describe the competitive landscape?"]),
        ("hyb", "🔢+📄 Both",
         "figures + verified explanation",
         ["How much was spent on R&D, and what is it going toward?",
          "Did profitability improve, and what explains the change?",
          "How large is the debt load, and how is it managed?"]),
    ]
    cols = st.columns(3)
    for col, (css, title, hint, questions) in zip(cols, _CATEGORIES):
        with col:
            st.markdown(
                f'<div class="fs-cat {css}">{title}'
                f'<small>{hint}</small></div>',
                unsafe_allow_html=True,
            )
            for i, q in enumerate(questions):
                if st.button(q, key=f"ex_{css}_{i}", use_container_width=True):
                    st.session_state["queued_q"] = q
                    st.rerun()

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
if not prompt:
    prompt = st.session_state.pop("queued_q", None)
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
            turn: dict = {"question": prompt, "ticker": ticker}
            try:
                qa_history = []
                for h in history:
                    if h.get("digest"):
                        qa_history.append(
                            {"question": h["question"],
                             "summary": h.get("summary_for_context", "")})
                    elif h.get("report") and h["report"].get("supported", True):
                        qa_history.append(
                            {"question": h["question"],
                             "summary": h["report"]["summary"]})
                standalone = condense_followup(
                    prompt, qa_history, settings=settings()
                )
                turn["standalone"] = standalone
                report = answer_with_progress(standalone, ticker)
                if report.get("needs_onboarding"):
                    # Question mentions a company we don't have yet:
                    # onboard it inline, then answer for real.
                    tk = report["needs_onboarding"]
                    st.write(f"➕ {tk} isn't in the corpus — adding it…")
                    ensure_ticker(tk, settings=settings(),
                                  progress=st.write)
                    tickers.clear()
                    turn["ticker"] = tk
                    report = answer_with_progress(standalone, tk)
                turn["report"] = report
            except PipelineError as exc:
                turn["error"] = f"Invalid input: {exc}"
            except Exception as exc:  # noqa: BLE001
                turn["error"] = f"Analysis failed, please retry. ({exc})"
        history.append(turn)
        st.rerun()
