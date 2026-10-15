"""
Human-readable run report -- translates the real outputs of one pipeline run
(the per-window GA-vs-baseline comparison, the proposed schedule, the order
completion predictions, and any breakdown simulation) into a plain-language
narrative for a non-technical reader (e.g. a production manager).

WHY THIS EXISTS. Every other report in this project (evaluation_report.py,
PROJECT_STATUS.txt) is written for someone who already knows what a capacity
floor or an XAI reasoning string is. This one is not -- it reads the same
real, already-produced JSON and turns it into sentences, with no numbers or
order IDs invented and no jargon left untranslated. It only ever describes
what a run's own output actually contains: if a field is missing (no
breakdown occurred, a window's capacity floor isn't measurable yet), the
report says so plainly instead of guessing.

Nothing here is hardcoded to a specific order ID, window count, or number --
every figure is read from the input files at generation time, so this
regenerates correctly against any future run's output.

Inputs (only real, already-produced files; nothing here is invented):
  outputs/authoritative_comparison_report.json   per-window GA vs. baselines
  outputs/proposed_schedule.json                 per-operation assignments + XAI text
  outputs/order_completions.json                 predicted completion vs. deadline
  outputs/final_run_report.json (optional)       breakdown-simulation aggregate,
                                                  used only for its 'breakdown' key
  a per-operation breakdown assessment list (optional, richer than the above)

Produces: outputs/run_report.md
"""

import datetime
import json
import os
import statistics

from utils.config import OUTPUT_DIR
from utils.logger import get_logger

logger = get_logger("run_report", "agent.log")


def _load_json(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _fmt_date(iso_str):
    if not iso_str:
        return "an unknown date"
    try:
        return datetime.datetime.fromisoformat(iso_str).strftime('%d %b %Y')
    except ValueError:
        return iso_str


def _fmt_hours(h):
    if h is None:
        return "0 hours"
    if h < 24:
        return f"{h:.0f} hours"
    return f"{h / 24:.1f} days"


# --------------------------------------------------------------- executive
def _executive_summary(comparison, completions):
    total_orders = len(completions)
    on_time = sum(1 for o in completions if not o.get('breach_risk'))
    on_time_pct = 100.0 * on_time / total_orders if total_orders else 0.0

    lines = [
        "## Executive summary",
        "",
    ]

    ga_agg = (comparison or {}).get('ga_aggregate') or {}
    floor_days = (comparison or {}).get('overall_capacity_floor_days')
    floor_sentence = ""
    if ga_agg.get('makespan_days') and floor_days:
        ratio = ga_agg['makespan_days'] / floor_days
        book_orders = ga_agg.get('orders')
        scope = (
            f"Looking beyond this window at the entire order book being tracked "
            f"({book_orders} orders" if book_orders else "Looking at the entire order book being tracked"
        )
        floor_sentence = (
            f" {scope}), it is projected to all clear in {ga_agg['makespan_days']:.0f} days, "
            f"which is {ratio:.2f}x the fastest that is physically possible given the factory's "
            f"capacity ({floor_days:.0f} days) -- in other words, the schedule is "
            f"{'essentially at' if ratio <= 1.02 else 'close to' if ratio <= 1.15 else 'noticeably above'} "
            f"the theoretical best case."
        )

    summary = (
        f"This run's immediate operational window scheduled {total_orders} orders, of which "
        f"{on_time} ({on_time_pct:.0f}%) are predicted to finish on time.{floor_sentence}"
    )
    lines.append(summary)

    # Dense vs. slack pattern, from the per-window comparison against
    # simple heuristics -- only stated if that data is actually present.
    per_window = (comparison or {}).get('per_window') or {}
    ga_windows = per_window.get('GA') or []
    dense = [w for w in ga_windows if w.get('classification') == 'dense'
             and w.get('margin_pct_vs_best_of_5_heuristics') is not None]
    slack = [w for w in ga_windows if w.get('classification') == 'slack'
             and w.get('margin_pct_vs_best_of_5_heuristics') is not None]
    # ORDER-WEIGHTED, not an average of percentages (audit #3).
    #
    # This used statistics.mean() over per-window margin percentages and then
    # presented the result as a book-wide figure. The windows do not hold
    # equal numbers of orders (300/300/300/129 on the current run), so an
    # unweighted mean silently gives a 129-order window the same say as a
    # 300-order one. Each window's margin is now weighted by the orders it
    # actually committed, and the window sizes are stated so the reader can
    # see they are unequal rather than having to assume they are not.
    def _weighted_margin(windows):
        """Order-weighted mean of PER-WINDOW RELATIVE FITNESS IMPROVEMENT.

        Named precisely because it is not what a casual reader assumes
        (Batch-4 fix 8). Each window contributes its own percentage
        improvement in the GA's scalarised fitness against that window's best
        heuristic; weighting by committed orders stops a 129-order window
        counting as much as a 300-order one. It is NOT a whole-book fitness
        improvement: the windows are scored against different baselines and
        different carried-forward machine states, so the parts are not
        summable into one book-level figure.
        """
        total = sum(w.get('committed_orders') or 0 for w in windows)
        if not total:
            # No order counts recorded -- fall back to the plain mean, but
            # say so rather than passing it off as order-weighted.
            return statistics.mean(
                w['margin_pct_vs_best_of_5_heuristics'] for w in windows), None
        return sum(
            w['margin_pct_vs_best_of_5_heuristics'] * (w.get('committed_orders') or 0)
            for w in windows
        ) / total, total

    def _sizes(windows):
        return ", ".join(str(w.get('committed_orders') or '?') for w in windows)

    if dense or slack:
        parts = []
        if dense:
            dense_margin, dense_orders = _weighted_margin(dense)
            scope = (f"across {dense_orders} orders" if dense_orders
                     else "averaged per window (order counts unavailable)")
            parts.append(
                f"in the {len(dense)} busy period(s) this run covered "
                f"({_sizes(dense)} orders each), its schedules scored "
                f"{dense_margin:.1f}% better on the planning objective than the "
                f"best simple rule-of-thumb method -- an order-weighted mean of "
                f"the per-window relative fitness improvement, {scope}"
            )
        if slack:
            slack_margin, slack_orders = _weighted_margin(slack)
            scope = (f"across {slack_orders} orders" if slack_orders
                     else "averaged per window (order counts unavailable)")
            parts.append(
                f"in the {len(slack)} quiet period(s) ({_sizes(slack)} orders each), it "
                f"performed about the same as those simpler methods "
                f"({slack_margin:+.1f}%, same order-weighted mean of per-window "
                f"relative fitness improvement, {scope}), which is expected when "
                f"there is little room to improve on an easy problem"
            )
        lines.append("")
        lines.append(
            "Put simply, the system performs strongly when demand is heavy and safely matches "
            "simple methods when things are quiet: " + "; and ".join(parts) + "."
        )

    return "\n".join(lines)


# ------------------------------------------------------------ order detail
def _ops_for_order(schedule, order_id):
    ops = [o for o in schedule if str(o.get('production_order_id')) == str(order_id)]
    ops.sort(key=lambda o: o.get('proposed_start_time') or '')
    return ops


def _narrate_operation(op, verb="Its final operation"):
    machines = op.get('machines_used', 0)
    operators = op.get('operators_used', 0)
    line = op.get('assigned_line_number', 'an unassigned line')
    start = _fmt_date(op.get('proposed_start_time'))
    end = _fmt_date(op.get('cushioned_end_time') or op.get('base_end_time'))
    reason = op.get('xai_reason', '').strip()
    text = (
        f"{verb} runs on {line} from {start} to {end}, using {machines} machine(s) and "
        f"{operators} operator(s) (sourced {op.get('operator_sourcing_tier', 'unknown')}-tier). "
    )
    if reason:
        text += f"The system's own reasoning for this assignment: \"{reason}\""
    return text


def _order_highlights(schedule, completions):
    lines = ["## Order-by-order highlights", ""]

    by_id = {str(o['production_order_id']): o for o in completions}

    # Most at-risk: the breach with the largest overrun.
    at_risk = [o for o in completions if o.get('breach_risk')]
    if at_risk:
        worst = max(at_risk, key=lambda o: o.get('late_by_hours') or 0)
        ops = _ops_for_order(schedule, worst['production_order_id'])
        lines.append(
            f"**Most at-risk order** -- order {worst['production_order_id']} is the least safe "
            f"order in this run: it is predicted to finish on {_fmt_date(worst['predicted_completion'])}, "
            f"which is {_fmt_hours(worst.get('late_by_hours'))} after its committed deadline of "
            f"{_fmt_date(worst.get('existing_committed_deadline'))}."
        )
        if ops:
            lines.append(_narrate_operation(ops[-1]))
        lines.append("")
    else:
        lines.append(
            "**Most at-risk order** -- no order in this run is currently predicted to miss its "
            "committed deadline."
        )
        lines.append("")

    # Typical order: the median order by lateness, representing the
    # ordinary case rather than a cherry-picked best or worst one.
    if completions:
        ranked = sorted(completions, key=lambda o: o.get('late_by_hours') or 0)
        typical = ranked[len(ranked) // 2]
        ops = _ops_for_order(schedule, typical['production_order_id'])
        status = (
            f"predicted to finish {_fmt_hours(typical.get('late_by_hours'))} late"
            if typical.get('breach_risk')
            else "predicted to finish on or ahead of its deadline"
        )
        lines.append(
            f"**A typical order** -- order {typical['production_order_id']} represents a "
            f"median case: it is {status} "
            f"(deadline {_fmt_date(typical.get('existing_committed_deadline'))}), "
            f"made up of {len(ops)} operation(s)."
        )
        if ops:
            lines.append(_narrate_operation(ops[0], verb="Its first operation"))
        lines.append("")

    # Genuinely infeasible: deadline had already passed before this
    # schedule was even proposed.
    infeasible = [o for o in completions if o.get('deadline_already_passed')]
    if infeasible:
        ids = ", ".join(o['production_order_id'] for o in infeasible)
        lines.append(
            f"**Infeasible orders** -- {len(infeasible)} order(s) already had a deadline in the "
            f"past before this schedule was even proposed, and cannot be delivered on time no "
            f"matter how they are scheduled: {ids}."
        )
    else:
        lines.append(
            "**Infeasible orders** -- none. Every order in this run still had a deadline in the "
            "future at the time this schedule was proposed."
        )

    return "\n".join(lines)


# ----------------------------------------------------------------- disruption
_TIER_SENTENCES = {
    'NO_ACTION': "absorbed by another machine that was already free, with no change to the schedule",
    'LOCAL_PATCH': "moved to a different machine, with a small knock-on delay",
    'FULL_RESCHEDULE': "escalated for a full reschedule, since no local patch could cover it",
}


def _disruption_narrative(breakdown):
    if not breakdown:
        return None
    lines = ["## Disruption narrative", ""]

    # Rich form: a list of per-operation assessment dicts (tier, reason,
    # donor_plan, from_standby_reserve, impact_hours...).
    if isinstance(breakdown, list):
        if not breakdown:
            return None
        asset = breakdown[0].get('original_asset_id', 'a machine')
        lines.append(
            f"A breakdown of machine {asset} was simulated, affecting {len(breakdown)} queued "
            f"operation(s)."
        )
        by_tier = {}
        for a in breakdown:
            by_tier.setdefault(a.get('tier'), []).append(a)
        for tier, items in by_tier.items():
            verb = "was" if len(items) == 1 else "were"
            sentence = _TIER_SENTENCES.get(tier, f"resolved with outcome '{tier}'")
            lines.append(f"- {len(items)} operation(s) {verb} {sentence}.")
        reserve = [a for a in breakdown if a.get('from_standby_reserve')]
        donors = [a for a in breakdown if a.get('donor_plan')]
        if reserve:
            lines.append(f"- {len(reserve)} operation(s) were covered using standby reserve capacity.")
        if donors:
            lines.append(
                f"- {len(donors)} operation(s) required taking a slot from another order's "
                f"schedule (donor preemption) to stay on track."
            )
        if not reserve and not donors:
            lines.append(
                "- None of the affected operations needed standby reserve capacity or another "
                "order's slot -- ordinary spare machine capacity covered the disruption."
            )
        impacts = [a['impact_hours'] for a in breakdown if a.get('impact_hours') is not None]
        if impacts:
            lines.append(f"\nWorst-case delay from this disruption: {_fmt_hours(max(impacts))}.")
        return "\n".join(lines)

    # Aggregate form: the {'asset','affected_operations','tiers',...} dict
    # the pipeline currently records in final_run_report.json.
    affected = breakdown.get('affected_operations', 0)
    if not affected:
        lines.append(
            f"A breakdown of machine {breakdown.get('asset', 'a machine')} was simulated, but it "
            f"affected zero queued operations -- a true no-op."
        )
        return "\n".join(lines)

    lines.append(
        f"A breakdown of machine {breakdown.get('asset', 'a machine')} was simulated, affecting "
        f"{affected} queued operation(s)."
    )
    tiers = breakdown.get('tiers') or {}
    for tier, count in tiers.items():
        verb = "was" if count == 1 else "were"
        sentence = _TIER_SENTENCES.get(tier, f"resolved with outcome '{tier}'")
        lines.append(f"- {count} operation(s) {verb} {sentence}.")
    reserve = breakdown.get('covered_from_reserve', 0)
    donors = breakdown.get('donor_preemptions', 0)
    if reserve:
        lines.append(f"- {reserve} operation(s) were covered using standby reserve capacity.")
    if donors:
        lines.append(
            f"- {donors} operation(s) required taking a slot from another order's schedule "
            f"(donor preemption) to stay on track."
        )
    if not reserve and not donors:
        lines.append(
            "- None of the affected operations needed standby reserve capacity or another "
            "order's slot -- ordinary spare machine capacity covered the disruption."
        )
    max_impact = breakdown.get('max_impact_hours')
    if max_impact:
        lines.append(f"\nWorst-case delay from this disruption: {_fmt_hours(max_impact)}.")

    return "\n".join(lines)


# --------------------------------------------------------------------- caveats
def _caveats(comparison, completions, breakdown):
    notes = []

    per_window = (comparison or {}).get('per_window') or {}
    ga_windows = per_window.get('GA') or []
    unmeasured = [w for w in ga_windows
                  if w.get('capacity_floor_ratio_cum_makespan_over_floor') is None]
    if unmeasured:
        nums = ", ".join(str(w.get('window')) for w in unmeasured)
        notes.append(
            f"The capacity-floor comparison isn't meaningful yet for window(s) {nums} -- there "
            f"isn't enough cumulative work scheduled yet for that comparison to mean anything."
        )

    infeasible = [o for o in (completions or []) if o.get('deadline_already_passed')]
    if infeasible:
        notes.append(
            f"{len(infeasible)} order(s) in this run were already overdue before scheduling even "
            f"started (see Infeasible orders, above)."
        )

    if isinstance(breakdown, dict) and breakdown.get('affected_operations'):
        if not breakdown.get('covered_from_reserve') and not breakdown.get('donor_preemptions'):
            notes.append(
                "This run's simulated disruption never needed the standby-reserve or "
                "donor-preemption logic -- those code paths were not exercised by this run's data."
            )
    elif isinstance(breakdown, list) and breakdown:
        if not any(a.get('from_standby_reserve') for a in breakdown) and \
           not any(a.get('donor_plan') for a in breakdown):
            notes.append(
                "This run's simulated disruption never needed the standby-reserve or "
                "donor-preemption logic -- those code paths were not exercised by this run's data."
            )

    if not notes:
        return None
    return "## Honest caveats\n\n" + "\n".join(f"- {n}" for n in notes)


# ------------------------------------------------------------------------ main
def generate_run_report(comparison_path=None, schedule_path=None, completions_path=None,
                         breakdown_path=None, pipeline_report_path=None, out_path=None):
    """
    Reads real pipeline output (nothing invented) and writes a plain-language
    markdown narrative to outputs/run_report.md (or out_path).

    comparison_path:    outputs/authoritative_comparison_report.json by default
    schedule_path:      outputs/proposed_schedule.json by default
    completions_path:   outputs/order_completions.json by default
    breakdown_path:      optional -- a JSON file holding either the raw list
                         returned by agent.impact_assessor.assess_machine_breakdown(),
                         or the aggregate dict the pipeline stores under
                         final_run_report.json's 'breakdown' key. If omitted,
                         falls back to pipeline_report_path's 'breakdown' key.
    pipeline_report_path: outputs/final_run_report.json by default -- only its
                         'breakdown' key is used, and only if breakdown_path
                         wasn't given directly.

    Returns the markdown text (also written to disk).
    """
    comparison_path = comparison_path or os.path.join(OUTPUT_DIR, "authoritative_comparison_report.json")
    schedule_path = schedule_path or os.path.join(OUTPUT_DIR, "proposed_schedule.json")
    completions_path = completions_path or os.path.join(OUTPUT_DIR, "order_completions.json")
    pipeline_report_path = pipeline_report_path or os.path.join(OUTPUT_DIR, "final_run_report.json")
    out_path = out_path or os.path.join(OUTPUT_DIR, "run_report.md")

    comparison = _load_json(comparison_path)
    schedule = _load_json(schedule_path) or []
    completions = _load_json(completions_path) or []

    if breakdown_path:
        breakdown = _load_json(breakdown_path)
    else:
        pipeline_report = _load_json(pipeline_report_path)
        breakdown = (pipeline_report or {}).get('breakdown')

    from utils.run_artifacts import require_same_run, stamp_artifact
    # Check EVERY contributing artifact, including list-valued schedules,
    # completions and explicitly supplied disruption assessments.
    identity_paths = [comparison_path, schedule_path, completions_path,
                      breakdown_path if breakdown_path else pipeline_report_path]
    report_run_id = require_same_run([p for p in identity_paths if p])

    if not completions:
        logger.warning(f"No order completions found at {completions_path} -- report will be sparse.")
    if not schedule:
        logger.warning(f"No proposed schedule found at {schedule_path} -- order narratives will be sparse.")
    if not comparison:
        logger.warning(f"No comparison report found at {comparison_path} -- executive summary will be sparse.")

    sections = [
        f"# Run report",
        f"Run identity: {report_run_id}",
        "",
        f"_Generated {datetime.datetime.now().strftime('%d %b %Y %H:%M')}_",
        "",
        _executive_summary(comparison, completions),
        "",
        _order_highlights(schedule, completions),
    ]

    disruption = _disruption_narrative(breakdown)
    if disruption:
        sections += ["", disruption]

    caveats = _caveats(comparison, completions, breakdown)
    if caveats:
        sections += ["", caveats]

    text = "\n".join(sections) + "\n"

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(text)
    stamp_artifact(out_path, report_run_id)
    logger.info(f"Run report written to {out_path}")
    return text


if __name__ == "__main__":
    report = generate_run_report()
    print(report)
