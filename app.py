"""Exercise 5 — Streamlit approval UI for the HITL PR review agent.

Run with:
    uv run streamlit run app.py

Goal: wrap the LangGraph built in exercises 1–4 in a web UI that adapts to
the confidence bucket of each PR.

Routing thresholds (common/schemas.py):
    > 72%        auto_approve     UI shows a success card; reviewer does nothing
    58 – 72%     human_approval   UI shows Approve / Reject / Edit buttons
    <  58%       escalate         UI shows a question form for the reviewer
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from common.db import db_conn, db_path, replay_events
from exercises.exercise_4_audit import build_graph

load_dotenv()


# ─── Page setup ────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="HITL PR Review Agent — Trần Chi Vĩ (2A202602968)",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Custom CSS ────────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
    .main-header {
        font-size: 2.2rem;
        font-weight: 700;
        margin-bottom: 0.2rem;
    }
    .sub-header {
        color: #6b7280;
        font-size: 1.0rem;
        margin-bottom: 1.2rem;
    }
    .badge-green {
        background-color: #d1fae5;
        color: #065f46;
        padding: 0.25rem 0.6rem;
        border-radius: 9999px;
        font-weight: 600;
        font-size: 0.85rem;
    }
    .badge-yellow {
        background-color: #fef3c7;
        color: #92400e;
        padding: 0.25rem 0.6rem;
        border-radius: 9999px;
        font-weight: 600;
        font-size: 0.85rem;
    }
    .badge-red {
        background-color: #fee2e2;
        color: #991b1b;
        padding: 0.25rem 0.6rem;
        border-radius: 9999px;
        font-weight: 600;
        font-size: 0.85rem;
    }
    .metric-card {
        background: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 0.5rem;
        padding: 1rem;
        text-align: center;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="main-header">🛡️ HITL PR Review Agent</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="sub-header">LangGraph Human-in-the-Loop Orchestration · SQLite Audit Trail · Multi-Tier Confidence Routing<br>'
    '<b>Học viên:</b> Trần Chi Vĩ · <b>MSSV:</b> 2A202602968 · <b>GitHub:</b> '
    f'<code>{os.environ.get("GITHUB_USER", "civi0411")}</code></div>',
    unsafe_allow_html=True,
)

# ─── Session state ─────────────────────────────────────────────────────────
if "thread_id" not in st.session_state:
    st.session_state.thread_id = None
if "pr_url" not in st.session_state:
    st.session_state.pr_url = ""
if "interrupt_payload" not in st.session_state:
    st.session_state.interrupt_payload = None
if "final" not in st.session_state:
    st.session_state.final = None
if "selected_replay_thread" not in st.session_state:
    st.session_state.selected_replay_thread = None


# ─── Database Helpers ───────────────────────────────────────────────────────
async def fetch_recent_sessions():
    async with db_conn() as conn:
        async with conn.execute(
            """
            SELECT thread_id,
                   pr_url,
                   MIN(timestamp)        AS started,
                   MAX(timestamp)        AS last_event,
                   MAX(risk_level)       AS worst_risk,
                   COUNT(*)              AS events
              FROM audit_events
             GROUP BY thread_id, pr_url
             ORDER BY MAX(timestamp) DESC
             LIMIT 15
            """
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def fetch_calibration_stats():
    async with db_conn() as conn:
        async with conn.execute(
            """
            SELECT 
                COUNT(DISTINCT thread_id) AS total_sessions,
                AVG(confidence) AS avg_confidence,
                SUM(CASE WHEN decision IN ('approve', 'auto', 'committed') THEN 1 ELSE 0 END) AS approved_count,
                SUM(CASE WHEN decision = 'reject' THEN 1 ELSE 0 END) AS rejected_count,
                SUM(CASE WHEN risk_level = 'high' THEN 1 ELSE 0 END) AS high_risk_count,
                SUM(CASE WHEN risk_level = 'med' THEN 1 ELSE 0 END) AS med_risk_count,
                SUM(CASE WHEN risk_level = 'low' THEN 1 ELSE 0 END) AS low_risk_count
            FROM audit_events
            WHERE action IN ('commit', 'auto_approve', 'human_approval', 'route')
            """
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}


async def fetch_checkpoints(thread_id: str):
    """Bonus Challenge: Time-Travel checkpoint history."""
    async with AsyncSqliteSaver.from_conn_string(db_path()) as cp:
        await cp.setup()
        app = build_graph(cp)
        cfg = {"configurable": {"thread_id": thread_id}}
        history = []
        async for state in app.aget_state_history(cfg):
            history.append({
                "step": getattr(state, "metadata", {}).get("step", 0),
                "source": getattr(state, "metadata", {}).get("source", "unknown"),
                "next": state.next,
                "created_at": getattr(state, "created_at", None),
                "config_thread": state.config.get("configurable", {}).get("thread_id"),
                "has_analysis": "analysis" in state.values,
                "decision": state.values.get("decision"),
                "final_action": state.values.get("final_action"),
            })
        return history


# ─── Drive the graph ───────────────────────────────────────────────────────
async def run_graph(pr_url: str, thread_id: str, resume_value=None):
    """Invoke the graph once. Returns the final result or {'__interrupt__': ...}."""
    async with AsyncSqliteSaver.from_conn_string(db_path()) as cp:
        await cp.setup()
        app = build_graph(cp)
        cfg = {"configurable": {"thread_id": thread_id}}

        if resume_value is None:
            result = await app.ainvoke(
                {"pr_url": pr_url, "thread_id": thread_id},
                cfg,
            )
        else:
            result = await app.ainvoke(
                Command(resume=resume_value),
                cfg,
            )
        return result


# ─── Sidebar — Sessions & Bonus Analytics ──────────────────────────────────
with st.sidebar:
    st.header("🗄️ Audit Trail & History")

    # Calibration Stats (Bonus 2)
    stats = asyncio.run(fetch_calibration_stats())
    if stats and stats.get("total_sessions", 0) > 0:
        with st.expander("📊 Confidence Calibration", expanded=True):
            avg_c = stats.get("avg_confidence") or 0.0
            st.metric("Avg Model Confidence", f"{avg_c:.1%}")
            c1, c2 = st.columns(2)
            c1.metric("Total Sessions", stats.get("total_sessions", 0))
            c2.metric("Approved", stats.get("approved_count", 0))
            st.caption(
                f"Risk Breakdown: 🔴 {stats.get('high_risk_count', 0)} High · "
                f"🟡 {stats.get('med_risk_count', 0)} Med · "
                f"🟢 {stats.get('low_risk_count', 0)} Low"
            )

    sessions = asyncio.run(fetch_recent_sessions())
    if sessions:
        st.subheader("Recent Sessions")
        for s in sessions:
            risk = s.get("worst_risk", "med")
            badge = "🟢" if risk == "low" else ("🟡" if risk == "med" else "🔴")
            tid_short = s["thread_id"][:8]
            pr_label = s["pr_url"].split("/")[-1] if "/" in s["pr_url"] else s["pr_url"]
            if st.button(
                f"{badge} PR #{pr_label} ({tid_short}) · {s['events']} evts",
                key=f"session_{s['thread_id']}",
                use_container_width=True,
            ):
                st.session_state.selected_replay_thread = s["thread_id"]
                st.session_state.thread_id = s["thread_id"]
                st.session_state.pr_url = s["pr_url"]
                st.rerun()
    else:
        st.caption("No sessions recorded yet in hitl_audit.db")

    if st.button("➕ Start New Session", use_container_width=True):
        st.session_state.thread_id = None
        st.session_state.interrupt_payload = None
        st.session_state.final = None
        st.session_state.selected_replay_thread = None
        st.rerun()


# ─── Replay Viewer if a session is selected from sidebar ───────────────────
if st.session_state.selected_replay_thread:
    sel_tid = st.session_state.selected_replay_thread
    st.info(f"Viewing Historical Session: `{sel_tid}`")
    evts = asyncio.run(replay_events(sel_tid))
    if evts:
        df_evts = pd.DataFrame(evts)
        st.dataframe(
            df_evts[[
                "timestamp", "action", "confidence", "risk_level",
                "decision", "reviewer_id", "execution_time_ms", "reason"
            ]],
            use_container_width=True,
        )

    # Time-Travel Checkpoint Inspection (Bonus 1)
    with st.expander("⏳ Time-Travel Checkpoint State History (Bonus Challenge 1)"):
        cps = asyncio.run(fetch_checkpoints(sel_tid))
        if cps:
            st.dataframe(pd.DataFrame(cps), use_container_width=True)
        else:
            st.write("No checkpoints found.")


# ─── Top Form — Run PR Review ──────────────────────────────────────────────
st.subheader("🚀 Run Pull Request Review")

col_input, col_presets = st.columns([3, 2])
with col_presets:
    st.write("**Quick Presets:**")
    cp1, cp2 = st.columns(2)
    if cp1.button("PR #1 (Priority ~65%)"):
        st.session_state.pr_url = "https://github.com/VinUni-AI20k/PR-Demo/pull/1"
        st.rerun()
    if cp2.button("PR #2 (Login & Sync <55%)"):
        st.session_state.pr_url = "https://github.com/VinUni-AI20k/PR-Demo/pull/2"
        st.rerun()

with col_input:
    with st.form("start"):
        pr_url_val = st.text_input(
            "GitHub Pull Request URL",
            value=st.session_state.pr_url,
            placeholder="https://github.com/VinUni-AI20k/PR-Demo/pull/1",
        )
        submitted = st.form_submit_button("🔍 Run HITL Review", type="primary")


# ─── Renderers per interrupt kind ──────────────────────────────────────────
def render_approval_card(payload: dict) -> dict | None:
    """58–72% bucket: show the LLM review + 3 buttons. Return resume dict or None."""
    conf = payload["confidence"]
    st.markdown("---")
    st.markdown(
        f"### 🟡 Human Approval Requested · Confidence: `{conf:.0%}` "
        f'<span class="badge-yellow">HUMAN-IN-THE-LOOP</span>',
        unsafe_allow_html=True,
    )
    st.info(f"**Agent Reasoning:** {payload['confidence_reasoning']}")

    st.markdown(f"**Summary:** {payload['summary']}")

    comments = payload.get("comments", [])
    if comments:
        st.markdown(f"**Proposed Review Comments ({len(comments)}):**")
        for c in comments:
            sev = c.get("severity", "suggestion")
            sev_icon = "🔴" if sev in ("blocker", "issue") else "💡"
            st.markdown(
                f"- {sev_icon} **`[{sev.upper()}]`** `{c['file']}:{c.get('line') or '?'}` — {c['body']}"
            )

    with st.expander("📄 View Unified PR Diff Preview", expanded=False):
        st.code(payload.get("diff_preview", ""), language="diff")

    st.markdown("#### Reviewer Decision")
    feedback = st.text_input(
        "Reviewer Note / Feedback (Required if editing or rejecting)",
        key="approval_feedback",
        placeholder="e.g. Please validate priority range 0-2 before inserting.",
    )

    col1, col2, col3 = st.columns(3)
    if col1.button("✅ Approve & Post Review", type="primary", use_container_width=True):
        return {"choice": "approve", "feedback": feedback}
    if col2.button("❌ Reject Review", use_container_width=True):
        return {"choice": "reject", "feedback": feedback or "Rejected by human reviewer"}
    if col3.button("✍️ Auto-Edit & Refine (Bonus)", use_container_width=True):
        if not feedback:
            st.warning("Please provide feedback notes above to guide the Auto-Edit refinement.")
            return None
        return {"choice": "edit", "feedback": feedback}
    return None


def render_escalation_card(payload: dict) -> dict | None:
    """< 58% bucket: show risk factors + question form. Return {question: answer} or None."""
    conf = payload["confidence"]
    st.markdown("---")
    st.markdown(
        f"### 🔴 Strong Escalation Triggered · Confidence: `{conf:.0%}` "
        f'<span class="badge-red">HIGH RISK / ESCALATION</span>',
        unsafe_allow_html=True,
    )
    st.error(f"**Security & Risk Reasoning:** {payload['confidence_reasoning']}")

    if payload.get("risk_factors"):
        st.warning("⚠️ **Identified Critical Risk Factors:**\n- " + "\n- ".join(payload["risk_factors"]))

    st.markdown(f"**PR Overview:** {payload['summary']}")

    questions = payload.get("questions", [])
    st.markdown(f"#### ❓ Specific Escalation Questions for Reviewer ({len(questions)}):")
    st.caption("The agent cannot safely review this PR without clarifying context from the authors/engineers.")

    with st.form("escalation"):
        answers: dict[str, str] = {}
        for idx, q in enumerate(questions):
            st.markdown(f"**Q{idx+1}: {q}**")
            ans = st.text_area(
                f"Answer for Q{idx+1}",
                key=f"escalate_q_{idx}",
                placeholder="Enter context, architectural rationale, or planned follow-ups...",
                height=70,
            )
            answers[q] = ans

        col_sub, _ = st.columns([2, 3])
        sub_escalate = col_sub.form_submit_button(
            "📨 Submit Clarifications & Re-Synthesize Review",
            type="primary",
            use_container_width=True,
        )

        if sub_escalate:
            # Validate that answers are provided
            missing = [q for q, a in answers.items() if not a.strip()]
            if missing:
                st.warning(f"Please provide answers to all questions (missing {len(missing)}).")
                return None
            return answers
    return None


# ─── Main Flow Execution ───────────────────────────────────────────────────
if submitted and pr_url_val:
    st.session_state.pr_url = pr_url_val
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.interrupt_payload = None
    st.session_state.final = None
    st.session_state.selected_replay_thread = None

    with st.spinner("🤖 Agent fetching PR, running security analysis and confidence routing..."):
        result = asyncio.run(run_graph(pr_url_val, st.session_state.thread_id))

    if "__interrupt__" in result:
        st.session_state.interrupt_payload = result["__interrupt__"][0].value
    else:
        st.session_state.final = result
    st.rerun()

# ─── Render Active Interrupt ───────────────────────────────────────────────
payload = st.session_state.interrupt_payload
if payload is not None:
    kind = payload.get("kind")
    if kind == "approval_request":
        answer = render_approval_card(payload)
    else:
        answer = render_escalation_card(payload)

    if answer is not None:
        with st.spinner("⚡ Resuming LangGraph execution with human input..."):
            result = asyncio.run(
                run_graph(
                    st.session_state.pr_url,
                    st.session_state.thread_id,
                    resume_value=answer,
                )
            )
        if "__interrupt__" in result:
            st.session_state.interrupt_payload = result["__interrupt__"][0].value
        else:
            st.session_state.interrupt_payload = None
            st.session_state.final = result
        st.rerun()

# ─── Render Final Result Card ──────────────────────────────────────────────
if st.session_state.final is not None:
    st.markdown("---")
    final = st.session_state.final
    action = final.get("final_action", "unknown")

    if action.startswith("auto") or action.startswith("committed"):
        st.markdown(
            f"""
            <div style="background-color: #ecfdf5; border-left: 5px solid #10b981; padding: 1.2rem; border-radius: 0.5rem; margin-bottom: 1rem;">
                <h3 style="color: #065f46; margin-top: 0;">🎉 Review Successfully Committed to GitHub!</h3>
                <p style="color: #047857; margin-bottom: 0.5rem;"><b>Final Action:</b> <code>{action}</code></p>
                <p>The automated PR review comments have been published directly to the repository issue timeline.</p>
                <a href="{st.session_state.pr_url}" target="_blank" style="display: inline-block; background-color: #10b981; color: white; padding: 0.5rem 1rem; border-radius: 0.375rem; text-decoration: none; font-weight: 600;">🔗 View Review on GitHub PR</a>
            </div>
            """,
            unsafe_allow_html=True,
        )
    elif action == "rejected":
        st.markdown(
            f"""
            <div style="background-color: #fffbeb; border-left: 5px solid #f59e0b; padding: 1.2rem; border-radius: 0.5rem; margin-bottom: 1rem;">
                <h3 style="color: #92400e; margin-top: 0;">⚠️ Review Rejected by Reviewer</h3>
                <p style="color: #b45309;">No comments were posted to the PR.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.info(f"Final Action: `{action}`")

    # Render analysis details if available
    analysis = final.get("analysis")
    if analysis:
        st.markdown(f"**Final Review Confidence:** `{analysis.confidence:.0%}`")
        st.markdown(f"**Summary:** {analysis.summary}")
        if analysis.comments:
            st.markdown(f"**Committed Comments ({len(analysis.comments)}):**")
            for c in analysis.comments:
                st.markdown(f"- **`[{c.severity.upper()}]`** `{c.file}:{c.line or '?'}` — {c.body}")

    st.markdown(
        f"**Audit Thread ID:** ` {st.session_state.thread_id} ` · "
        f"Replay via CLI: `uv run python -m audit.replay --thread {st.session_state.thread_id}`"
    )

    # Show live session timeline
    with st.expander("📜 Live Audit Trail Timeline for this Session", expanded=True):
        cur_evts = asyncio.run(replay_events(st.session_state.thread_id))
        if cur_evts:
            df_cur = pd.DataFrame(cur_evts)
            st.dataframe(
                df_cur[[
                    "timestamp", "action", "confidence", "risk_level",
                    "decision", "reviewer_id", "execution_time_ms", "reason"
                ]],
                use_container_width=True,
            )
