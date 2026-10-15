"""
Internal test dashboard for the Needle Craft scheduling agent.

Run from the project root (NOT from inside frontend/) with:
    streamlit run frontend/dashboard.py

This is a TESTING tool, not a customer-facing product — it exists so you can
exercise every agent function (full reschedule, breakdown handling, rush
orders, manager priorities, output/log inspection) from buttons instead of a
REPL. It imports your existing agent/ga/utils modules directly, so it runs
against your real database and real trained checkpoint — anything marked
"writes to your database" actually does.

Requires: pip install streamlit
"""

import os
import sys
import json
import glob
import pandas as pd
import streamlit as st

# Run as `streamlit run frontend/dashboard.py` from the project root — this
# makes `agent`, `ga`, `utils`, etc. importable the same way every other
# entry point in this project already does (see e.g. agent/decision_engine.py).
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.config import OUTPUT_DIR
from agent.event_handler import ShopFloorEventHandler
from agent.impact_assessor import assess_machine_breakdown
from agent.explainability import ExplainabilityEngine

st.set_page_config(page_title="Needle Craft Agent — Test Dashboard", layout="wide")


# ---------------------------------------------------------------------------
# Shared state: ONE handler instance for the whole session, not recreated on
# every button click / rerun. This matters specifically for manager priority
# overrides (agent/decision_engine.py's manager_forced_orders) — that's an
# in-memory set on the AIDecisionEngine instance, and Streamlit reruns the
# entire script on every interaction, so without session_state it would
# silently reset to empty after every single click.
# ---------------------------------------------------------------------------
if "handler" not in st.session_state:
    with st.spinner("Initializing agent (this loads config, no DB calls yet)..."):
        st.session_state.handler = ShopFloorEventHandler()

handler = st.session_state.handler
engine = handler.decision_engine


def load_json_safe(path):
    if not os.path.exists(path):
        return None, None
    with open(path, "r") as f:
        data = json.load(f)
    mtime = pd.Timestamp.fromtimestamp(os.path.getmtime(path))
    return data, mtime


def output_path(filename):
    return os.path.join(OUTPUT_DIR, filename)


st.sidebar.title("Needle Craft Agent")
st.sidebar.caption("Test dashboard — writes marked actions touch your real DB.")
page = st.sidebar.radio(
    "Page",
    [
        "System Status",
        "Full Reschedule",
        "Machine Breakdown",
        "Rush Order",
        "Manager Priorities",
        "Latest Outputs",
        "Explainability",
        "Logs",
    ],
)

if engine.manager_forced_orders:
    st.sidebar.warning(f"Active manager priorities: {sorted(engine.manager_forced_orders)}")
else:
    st.sidebar.caption("No manager priorities set.")


# ---------------------------------------------------------------------------
# System Status
# ---------------------------------------------------------------------------
if page == "System Status":
    st.title("System Status")
    st.caption("Quick sanity checks before running anything expensive.")

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Database")
        if st.button("Test DB connection"):
            try:
                from database.db import get_engine
                import pandas as pd
                pd.read_sql("SELECT 1", get_engine())
                st.success("Connected successfully.")
            except Exception as e:
                st.error(f"Connection failed: {e}")

    with col2:
        st.subheader("GNN Checkpoint")
        ckpt_path = os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth")
        if os.path.exists(ckpt_path):
            mtime = pd.Timestamp.fromtimestamp(os.path.getmtime(ckpt_path))
            st.success(f"Found — last trained {mtime}")
        else:
            st.error("No checkpoint found. Run `python -m models.train` first; rescheduling is blocked until a compatible checkpoint is available.")

    st.subheader("Key config values")
    from utils import config as cfg
    st.json({
        "GA_MAX_MACHINES_PER_OPERATION": cfg.GA_MAX_MACHINES_PER_OPERATION,
        "GA_LOTS_PER_OPERATION": cfg.GA_LOTS_PER_OPERATION,
        "ORDER_QUOTE_CUSHION_DAYS": cfg.ORDER_QUOTE_CUSHION_DAYS,
        "GNN_HIDDEN_CHANNELS": cfg.GNN_HIDDEN_CHANNELS,
        "SHIFT_START_HOUR / SHIFT_END_HOUR": f"{cfg.SHIFT_START_HOUR} - {cfg.SHIFT_END_HOUR}",
    })


# ---------------------------------------------------------------------------
# Full Reschedule
# ---------------------------------------------------------------------------
elif page == "Full Reschedule":
    st.title("Full Reschedule")
    st.caption(
        "Calls agent.decision_engine.trigger_rescheduling() — rebuilds the graph, "
        "runs GNN inference, runs the GA, and writes 3 candidate schedules. "
        "This is the FULL, factory-wide pass — for testing a single disruption "
        "without touching every order, use the Machine Breakdown page instead."
    )
    st.warning("⚠️ Writes to your database (Optimization_Metrics_Log) and overwrites files in outputs/. Can take a few minutes.")

    n_solutions = st.slider("Number of candidate solutions", 1, 5, 3)

    if st.button("▶ Run Full Reschedule", type="primary"):
        with st.spinner("Running full reschedule — this can take a few minutes for a large factory..."):
            try:
                engine.trigger_rescheduling(n_solutions=n_solutions)
                st.success("Reschedule complete.")
            except Exception as e:
                st.error(f"Reschedule failed: {e}")
                st.exception(e)

    summary, mtime = load_json_safe(output_path("schedule_options_summary.json"))
    if summary:
        st.subheader(f"Latest comparison (generated {mtime})")
        st.info(summary["recommendation_text"])
        st.dataframe(pd.DataFrame(summary["options"]), use_container_width=True)
    else:
        st.info("No schedule_options_summary.json yet — run a reschedule above.")


# ---------------------------------------------------------------------------
# Machine Breakdown
# ---------------------------------------------------------------------------
elif page == "Machine Breakdown":
    st.title("Simulate a Machine Breakdown")
    st.caption(
        "Tests the fuzzy impact-gate: assesses whether a breakdown is a "
        "no-op, a local patch, or needs a full reschedule — WITHOUT running "
        "the whole factory-wide GA for a single machine."
    )

    machine_id = st.number_input("Machine (asset_id)", min_value=1, step=1, value=1)
    affected_line_id = st.number_input("Line ID (for the DB status update, if you execute below)", min_value=1, step=1, value=1)

    st.subheader("Step 1 — Assess impact (read-only, no DB writes)")
    if st.button("🔍 Assess Impact"):
        with st.spinner("Assessing..."):
            try:
                assessments = assess_machine_breakdown(
                    int(machine_id), manager_forced_orders=engine.manager_forced_orders
                )
                st.session_state["last_assessment"] = assessments
            except Exception as e:
                st.error(f"Assessment failed: {e}")
                st.exception(e)

    assessments = st.session_state.get("last_assessment")
    if assessments is not None:
        if not assessments:
            st.success("No queued/running operations on this machine — a true no-op, nothing to reschedule.")
        else:
            df = pd.DataFrame(assessments)
            tier_emoji = {"NO_ACTION": "🟢", "LOCAL_PATCH": "🟡", "FULL_RESCHEDULE": "🔴"}
            df["tier"] = df["tier"].apply(lambda t: f"{tier_emoji.get(t, '')} {t}")
            st.dataframe(df, use_container_width=True)

            tier_counts = pd.Series([a["tier"] for a in assessments]).value_counts()
            st.write("Tier breakdown:", tier_counts.to_dict())

    st.divider()
    st.subheader("Step 2 — Execute for real")
    st.warning("⚠️ Writes to your database: marks the machine as Breakdown, applies local patches, and/or triggers a full reschedule for affected orders.")
    if st.button("💥 Execute Breakdown Event", type="primary"):
        with st.spinner("Handling breakdown event..."):
            try:
                handler.handle_machine_breakdown(int(machine_id), int(affected_line_id))
                st.success("Breakdown event handled — check the Logs page (agent.log) for the tier decisions and any local patches applied.")
            except Exception as e:
                st.error(f"Failed: {e}")
                st.exception(e)


# ---------------------------------------------------------------------------
# Rush Order
# ---------------------------------------------------------------------------
elif page == "Rush Order":
    st.title("Simulate a Rush Order")
    st.caption(
        "Calls agent.event_handler.handle_rush_order_insertion() — registers "
        "a PERSISTENT manager priority for this order (see the Manager "
        "Priorities page) and triggers a full reschedule with it prioritized."
    )
    st.warning("⚠️ Writes to your database and triggers a full factory-wide reschedule.")

    order_id = st.text_input("Production Order ID", value="")

    if st.button("🚀 Insert Rush Order", type="primary", disabled=not order_id):
        with st.spinner("Re-optimizing with this order prioritized..."):
            try:
                handler.handle_rush_order_insertion(order_id)
                st.success(f"Order {order_id} prioritized and schedule re-optimized.")
            except Exception as e:
                st.error(f"Failed: {e}")
                st.exception(e)


# ---------------------------------------------------------------------------
# Manager Priorities
# ---------------------------------------------------------------------------
elif page == "Manager Priorities":
    st.title("Manager Priority Overrides")
    st.caption(
        "These persist across every future reschedule/breakdown assessment "
        "in this dashboard session until cleared — see "
        "agent.decision_engine.AIDecisionEngine.set_manager_priority()."
    )

    if engine.manager_forced_orders:
        st.write("Currently forced:", sorted(engine.manager_forced_orders))
    else:
        st.info("No orders currently forced to top priority.")

    col1, col2 = st.columns(2)
    with col1:
        add_id = st.text_input("Order ID to set as top priority")
        if st.button("📌 Set Priority", disabled=not add_id):
            engine.set_manager_priority(add_id)
            st.rerun()
    with col2:
        if engine.manager_forced_orders:
            remove_id = st.selectbox("Order ID to clear", sorted(engine.manager_forced_orders))
            if st.button("❌ Clear Priority"):
                engine.clear_manager_priority(remove_id)
                st.rerun()


# ---------------------------------------------------------------------------
# Latest Outputs
# ---------------------------------------------------------------------------
elif page == "Latest Outputs":
    st.title("Latest Output Files")
    st.caption("Read-only view of outputs/*.json — refresh after running a reschedule.")

    files = sorted(glob.glob(output_path("proposed_schedule*.json"))) + \
        sorted(glob.glob(output_path("order_completions*.json")))
    summary_path = output_path("schedule_options_summary.json")
    if os.path.exists(summary_path):
        files = [summary_path] + files

    if not files:
        st.info("No output files found yet — run a reschedule first.")
    else:
        chosen = st.selectbox("File", [os.path.basename(f) for f in files])
        data, mtime = load_json_safe(output_path(chosen))
        st.caption(f"Last modified: {mtime}")
        if isinstance(data, list):
            st.dataframe(pd.DataFrame(data), use_container_width=True)
        else:
            st.json(data)


# ---------------------------------------------------------------------------
# Explainability
# ---------------------------------------------------------------------------
elif page == "Explainability":
    st.title("Explainability (XAI)")
    xai = ExplainabilityEngine()

    st.subheader("Executive summary")
    if st.button("Generate Summary"):
        summary = xai.generate_executive_summary()
        if "error" in summary:
            st.warning(summary["error"])
        else:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Total Operations", summary["total_scheduled_operations"])
            c2.metric("High-Risk Reroutes", summary["high_risk_reroutes_count"])
            c3.metric("Changeovers Applied", summary["style_changeovers_applied"])
            c4.metric("Deadline Warnings", summary["deadline_warning_count"])
            st.dataframe(pd.DataFrame(summary["audit_trail"]), use_container_width=True)

    st.divider()
    st.subheader("Look up a specific operation")
    col1, col2 = st.columns(2)
    with col1:
        lookup_order = st.text_input("Order ID", key="xai_order")
    with col2:
        lookup_op = st.text_input("Operation ID", key="xai_op")
    if st.button("Explain", disabled=not (lookup_order and lookup_op)):
        st.write(xai.format_operation_card(lookup_order, lookup_op))


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
elif page == "Logs":
    st.title("Logs")
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    log_files = sorted(glob.glob(os.path.join(log_dir, "*.log")))

    if not log_files:
        st.info(f"No log files found in {log_dir}")
    else:
        chosen_log = st.selectbox("Log file", [os.path.basename(f) for f in log_files])
        n_lines = st.slider("Lines to show (from the end)", 20, 1000, 100)
        if st.button("🔄 Refresh"):
            st.rerun()
        path = os.path.join(log_dir, chosen_log)
        with open(path, "r", errors="replace") as f:
            lines = f.readlines()
        st.code("".join(lines[-n_lines:]), language="log")
