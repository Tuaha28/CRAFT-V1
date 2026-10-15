"""
Standing evaluation report -- generated on every full pipeline run, not
produced by hand.

WHY THIS EXISTS. Every number in this project's write-ups was, until now,
recomputed ad hoc in a one-off script each time it was needed -- which is
exactly how a ~0.14% deadline drift and a "GA losing by 115%" measurement
artefact both slipped in earlier this project (see PROJECT_STATUS.txt
Section 4). This module reads the SAME data structures the pipeline already
produces (ga.rolling_horizon's aggregate/windows, models.evaluate's curve
data, agent.decision_engine's export summaries) and turns them into one
consistent report every time, so "what was the on-time rate" always has one
answer sourced from one place.

Produces, under outputs/:
  evaluation_report.json   everything, machine-readable
  evaluation_report.txt    human-readable summary
  plots/pr_curve.png       delay classifier precision-recall curve
  plots/roc_curve.png      delay classifier ROC curve
  plots/ga_convergence.png representative window's generation-vs-fitness
  plots/local_search_gain.png   per-window local-search improvement
  plots/labour_by_shift.png     whole-run labour hours by shift band
  plots/labour_by_sourcing.png  whole-run labour hours by sourcing tier

Plotting failures (e.g. matplotlib not installed) degrade to JSON+text only,
logged rather than raising -- a missing chart should never take down a
pipeline run that otherwise succeeded.
"""

import datetime
import json
import os

from utils.config import OUTPUT_DIR
from utils.logger import get_logger

logger = get_logger("evaluation_report", "agent.log")

PLOTS_DIR = os.path.join(OUTPUT_DIR, "plots")


def _save_fig(fig, name):
    import matplotlib.pyplot as plt
    os.makedirs(PLOTS_DIR, exist_ok=True)
    path = os.path.join(PLOTS_DIR, name)
    fig.savefig(path, dpi=120, bbox_inches='tight')
    plt.close(fig)
    return path


def _plot_pr_curve(curves):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(5, 4))
    ax.plot(curves['recall'], curves['precision'], lw=2, label='delay classifier')
    ax.axhline(curves['positive_rate'], color='gray', linestyle='--',
              label=f"no-skill baseline ({curves['positive_rate']:.2f})")
    ax.set_xlabel('Recall')
    ax.set_ylabel('Precision')
    # Title states only what THIS run measured (Batch-4 fix 8). It used to
    # assert "label is 99.9% 'operation started', not risk" -- a conclusion
    # about a label that has since been REPLACED (PROJECT_STATUS.txt Section
    # 0D), printed onto every figure regardless of the data behind it.
    ax.set_title(f"Delay classifier PR curve (AP={curves['average_precision']:.3f}, "
                f"no-skill baseline {curves['positive_rate']:.3f}, "
                f"n={curves.get('n_samples', 'n/a')})")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1.02)
    ax.legend(loc='upper right', fontsize=8)
    return _save_fig(fig, 'pr_curve.png')


def _plot_roc_curve(curves):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(5, 4))
    ax.plot(curves['fpr'], curves['tpr'], lw=2, label=f"ROC (AUC={curves['roc_auc']:.3f})")
    ax.plot([0, 1], [0, 1], color='gray', linestyle='--', label='chance (AUC=0.5)')
    ax.set_xlabel('False Positive Rate')
    ax.set_ylabel('True Positive Rate')
    ax.set_title('Delay classifier ROC curve')
    ax.legend(loc='lower right', fontsize=8)
    return _save_fig(fig, 'roc_curve.png')


def _plot_ga_convergence(logbook):
    import matplotlib.pyplot as plt
    gens = [r['gen'] for r in logbook]
    best = [r['min'] for r in logbook]
    avg = [r['avg'] for r in logbook]
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.plot(gens, best, lw=2, label='best fitness')
    ax.plot(gens, avg, lw=1, alpha=0.7, label='population average')
    ax.set_xlabel('Generation')
    ax.set_ylabel('Fitness (lower is better)')
    # Computed from THIS logbook (Batch-4 fix 8). The previous title asserted
    # "~0.4-0.9% improvement" and that the curve "should read almost flat" --
    # a finding from a different run, printed as though it described this one,
    # and telling the reader what to conclude before they looked at the data.
    if best and best[0] and best[-1] is not None:
        gain_pct = 100.0 * (best[0] - best[-1]) / abs(best[0])
        gain_text = f"best improved {gain_pct:+.2f}% over {len(gens)} generations"
    else:
        gain_text = f"{len(gens)} generations"
    ax.set_title(f'GA convergence\n{gain_text}')
    ax.legend(fontsize=8)
    return _save_fig(fig, 'ga_convergence.png')


def _plot_local_search_gain(windows):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6, 4))
    w = [x['window'] for x in windows]
    g = [x['local_search_gain_pct'] for x in windows]
    ax.bar(w, g, color=['#3b6' if v > 0 else '#999' for v in g])
    ax.set_xlabel('Window')
    ax.set_ylabel('Local search improvement over GA output (%)')
    ax.set_title('Local search gain, per window')
    ax.set_xticks(w)
    return _save_fig(fig, 'local_search_gain.png')


def _plot_labour_breakdown(minutes_dict, title, filename):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(5, 4))
    labels = list(minutes_dict.keys())
    values = [minutes_dict[k] / 60.0 for k in labels]
    ax.bar(labels, values)
    ax.set_ylabel('Labour hours')
    ax.set_title(title)
    return _save_fig(fig, filename)


def generate_evaluation_report(rolling_result, delay_curves=None, gnn_metrics=None,
                               checkpoint_path=None, write_plots=True):
    """
    rolling_result: the dict returned by
        ga.rolling_horizon.run_rolling_horizon(..., export=True) -- must
        have been run with export=True or the labour-by-shift/sourcing
        breakdown will be absent (schedule-level metrics are unaffected).

    delay_curves: pass a pre-computed dict from
        models.evaluate.compute_delay_curves() to avoid recomputing it (e.g.
        if the caller already ran GNN inference this session); None computes
        it here. Supplies positive_rate/average_precision/roc_auc.

    gnn_metrics: pass a pre-computed dict from models.evaluate.evaluate_gnn()
        to avoid recomputing it; None computes it here. Supplies
        delay_accuracy/delay_precision/delay_recall/delay_f1 -- reported
        alongside delay_curves' positive_rate/AP/ROC-AUC so a single section
        of the report answers "is this classifier still degenerate?" without
        cross-referencing a second file.

    Returns the report dict (also written to outputs/evaluation_report.json).
    """
    if delay_curves is None:
        from models.evaluate import compute_delay_curves
        logger.info("Computing delay classifier PR/ROC curves...")
        delay_curves = compute_delay_curves(checkpoint_path)
    if gnn_metrics is None:
        from models.evaluate import evaluate_gnn
        logger.info("Computing delay classifier accuracy/precision/recall/F1...")
        gnn_metrics = evaluate_gnn(checkpoint_path)

    aggregate = dict(rolling_result['aggregate'])
    report = {
        'generated_at': datetime.datetime.now().isoformat(),
        'standard_metrics': aggregate,
        'delay_classifier_curves': delay_curves,
        'gnn_metrics': gnn_metrics,
        'ga_convergence': rolling_result.get('representative_logbook'),
        'windows': rolling_result.get('windows'),
    }

    plot_paths = {}
    if write_plots:
        try:
            if delay_curves:
                plot_paths['pr_curve'] = _plot_pr_curve(delay_curves)
                plot_paths['roc_curve'] = _plot_roc_curve(delay_curves)
            if rolling_result.get('representative_logbook'):
                plot_paths['ga_convergence'] = _plot_ga_convergence(
                    rolling_result['representative_logbook'])
            if rolling_result.get('windows'):
                plot_paths['local_search_gain'] = _plot_local_search_gain(
                    rolling_result['windows'])
            if aggregate.get('labour_minutes_by_shift'):
                plot_paths['labour_by_shift'] = _plot_labour_breakdown(
                    aggregate['labour_minutes_by_shift'],
                    'Labour by shift band (whole run)', 'labour_by_shift.png')
            if aggregate.get('labour_minutes_by_sourcing_tier'):
                plot_paths['labour_by_sourcing'] = _plot_labour_breakdown(
                    aggregate['labour_minutes_by_sourcing_tier'],
                    'Labour by sourcing tier (whole run)', 'labour_by_sourcing.png')
        except Exception as e:
            # A missing plot must never take down a pipeline run that
            # otherwise succeeded -- the JSON/text report still has the data.
            logger.warning(f"Plot generation failed ({e!r}) — continuing with JSON/text only.")
    report['plots'] = plot_paths

    out_json = os.path.join(OUTPUT_DIR, "evaluation_report.json")
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, default=str)
    from utils.run_artifacts import stamp_artifact
    stamp_artifact(out_json)

    _write_text_summary(report)
    logger.info(f"Evaluation report written to {out_json}")
    return report


def _write_text_summary(report):
    m = report['standard_metrics']
    lines = [
        "STANDING EVALUATION REPORT",
        "=" * 60,
        f"generated  {report['generated_at']}",
        "",
        "STANDARD TRACKED OUTPUTS",
        f"  on-time rate            {m.get('on_time_pct', 0):.1f}%",
        f"  makespan                {m.get('makespan_days', 0):.1f} days",
        f"  throughput              {m.get('throughput_orders_per_day', 0):.1f} orders/day",
        f"  mean added tardiness    {m.get('mean_tardiness_h', 0):.1f} h (late orders; effective deadline)",
        f"  max added tardiness     {m.get('max_tardiness_h', 0):.1f} h (effective deadline)",
        f"  labour utilisation      {m.get('labour_utilisation_pct', 0):.1f}% (whole-book)",
        f"  commit / lookahead      {m.get('commit_size')} / {m.get('lookahead_size')} orders",
        "",
    ]

    c = report.get('delay_classifier_curves')
    g = report.get('gnn_metrics')
    if c or g:
        lines.append("DELAY CLASSIFIER (see PROJECT_STATUS.txt Section 0D for label history)")
        if c:
            lines += [
                f"  positive rate (base rate)  {c['positive_rate']:.3f}",
                f"  average precision (PR)     {c['average_precision']:.3f}  (no-skill = positive rate)",
                f"  ROC AUC                    {c['roc_auc']:.3f}  (no-skill = 0.500)",
            ]
        if g:
            lines += [
                f"  accuracy                   {g.get('delay_accuracy', 0):.3f}",
                f"  precision                  {g.get('delay_precision', 0):.3f}",
                f"  recall                     {g.get('delay_recall', 0):.3f}",
                f"  F1                         {g.get('delay_f1', 0):.3f}",
            ]
        lines.append("")

    windows = report.get('windows') or []
    if windows:
        lines.append("PER-WINDOW: ON-TIME AND LOCAL-SEARCH GAIN")
        for w in windows:
            lines.append(
                f"  window {w['window']:>2}: on-time {w.get('on_time_pct', 0):>5.1f}%   "
                f"local search {w.get('local_search_gain_pct', 0):+.2f}%"
            )
        lines.append("")

    if m.get('labour_minutes_by_shift'):
        lines.append("LABOUR BY SHIFT (whole run)")
        for k, v in m['labour_minutes_by_shift'].items():
            lines.append(f"  {k:<10} {v / 60:.0f} h")
        lines.append("")

    if m.get('labour_minutes_by_sourcing_tier'):
        lines.append("LABOUR BY SOURCING TIER (whole run)")
        for k, v in m['labour_minutes_by_sourcing_tier'].items():
            lines.append(f"  {k:<10} {v / 60:.0f} h")
        lines.append("")

    out_txt = os.path.join(OUTPUT_DIR, "evaluation_report.txt")
    with open(out_txt, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines) + "\n")
