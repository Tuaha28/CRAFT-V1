"""Export the validated Factory-2 172-operation CRAFT schedule for people to read.

Reads only frozen evidence (no database, no solver) and writes:
  deliverables/factory2_172_schedule/schedule.csv        one row per operation
  deliverables/factory2_172_schedule/orders.csv          one row per order
  deliverables/factory2_172_schedule/gantt.html          self-contained timeline
"""
import csv
from datetime import datetime, timedelta
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "outputs/crafter/factory2_172_production_v1_workerfix_full/assembled_factory_schedule.json"
STATE = ROOT / "outputs/crafter/evaluation_v2/neutral_factory2_state.json"
OUT = ROOT / "deliverables/factory2_172_schedule"

run = json.loads(RUN.read_text(encoding="utf-8"))
state = json.loads(STATE.read_text(encoding="utf-8"))
validation = run["validation"]
if not (validation["accepted"] and validation["physical_status"] == "VALID"
        and validation["replay_status"] == "PASS" and validation["scope_status"] == "SCOPE_VALID"
        and run["post_cp_modifications"] == 0):
    raise SystemExit("SCHEDULE_NOT_VALIDATED_REFUSING_EXPORT")
origin = datetime.fromisoformat(state["evaluation_origin"])
at = lambda seconds: origin + timedelta(seconds=int(seconds))
ops = {op["operation_key"]: op for op in state["operations"]}
orders = {o["order_id"]: o for o in state["orders"]}
rows = sorted(run["assembled_schedule"], key=lambda r: (r["start_seconds"], r["operation_key"]))
if len(rows) != len(ops) or {r["operation_key"] for r in rows} != set(ops):
    raise SystemExit("SCHEDULE_STATE_IDENTITY_MISMATCH")
OUT.mkdir(parents=True, exist_ok=True)

with (OUT / "schedule.csv").open("w", newline="", encoding="utf-8") as stream:
    w = csv.writer(stream)
    w.writerow(["order_id", "operation_key", "route_step", "operation", "line_id", "start", "end",
                "duration_hours", "setup_minutes", "machines", "workers", "machine_ids", "worker_ids"])
    for r in rows:
        op, cfg = ops[r["operation_key"]], r["resource_configuration"]
        w.writerow([r["order_id"], r["operation_key"], op["sequence"], op["required_capability"],
                    cfg["line_id"], at(r["start_seconds"]).isoformat(sep=" "), at(r["end_seconds"]).isoformat(sep=" "),
                    round((r["end_seconds"] - r["start_seconds"]) / 3600, 2), round(r["setup_seconds"] / 60, 1),
                    len(cfg["machine_ids"]), len(cfg["worker_ids"]),
                    " ".join(map(str, cfg["machine_ids"])), " ".join(cfg["worker_ids"])])

summary = []
for order_id, order in orders.items():
    mine = [r for r in rows if r["order_id"] == order_id]
    start, end = min(r["start_seconds"] for r in mine), max(r["end_seconds"] for r in mine)
    late = max(0, end - order["deadline_seconds"])
    summary.append({"order_id": order_id, "operations": len(mine), "release": at(order["release_seconds"]),
                    "due": at(order["deadline_seconds"]), "start": at(start), "finish": at(end),
                    "late_days": round(late / 86400, 2), "on_time": late == 0,
                    "due_before_schedule_start": order["deadline_seconds"] < 0})
summary.sort(key=lambda s: s["finish"])
with (OUT / "orders.csv").open("w", newline="", encoding="utf-8") as stream:
    w = csv.DictWriter(stream, fieldnames=list(summary[0]))
    w.writeheader()
    for s in summary:
        w.writerow({k: (v.isoformat(sep=" ") if isinstance(v, datetime) else v) for k, v in s.items()})

# Self-contained Gantt: one lane per order, bars per operation, hover for detail.
span = max(r["end_seconds"] for r in rows) - min(r["start_seconds"] for r in rows)
t0 = min(r["start_seconds"] for r in rows)
lanes = [s["order_id"] for s in summary]
bars = []
for r in rows:
    op, cfg = ops[r["operation_key"]], r["resource_configuration"]
    left = 100 * (r["start_seconds"] - t0) / span
    width = max(0.15, 100 * (r["end_seconds"] - r["start_seconds"]) / span)
    tip = (f"{r['operation_key']} · step {op['sequence']} · {op['required_capability']}\n"
           f"{at(r['start_seconds']):%d %b %H:%M} → {at(r['end_seconds']):%d %b %H:%M}\n"
           f"line {cfg['line_id']} · {len(cfg['machine_ids'])} machines · {len(cfg['worker_ids'])} workers")
    bars.append((lanes.index(r["order_id"]), left, width, tip))
days = int(span // 86400) + 1
ticks = "".join(f'<span class="tick" style="left:{100 * d * 86400 / span:.3f}%">{at(t0 + d * 86400):%d %b}</span>'
                for d in range(0, days, max(1, days // 12)))
lane_html = "".join(
    f'<div class="lane"><div class="label">Order {html.escape(s["order_id"])}'
    f'<small>{"on time" if s["on_time"] else f"{s[chr(108)+"ate_days"]} d late"}</small></div><div class="track">'
    + "".join(f'<div class="bar" style="left:{l:.3f}%;width:{w:.3f}%" title="{html.escape(t)}"></div>'
              for lane, l, w, t in bars if lane == i)
    + "</div></div>" for i, s in enumerate(summary))
on_time = sum(s["on_time"] for s in summary)
page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Factory 2 Schedule</title><style>
:root{{--bg:#f7f7f5;--panel:#fff;--text:#1d2125;--muted:#5f6670;--line:#dde0e4;--bar:#2f6f9f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#15181b;--panel:#1d2125;--text:#e8eaed;--muted:#9aa1aa;--line:#30353b;--bar:#5b9bd0}}}}
body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,sans-serif}}
main{{max-width:1400px;margin:0 auto;padding:24px 16px}}h1{{font-size:22px;margin:0 0 4px}}p{{color:var(--muted);margin:0 0 16px}}
.kpis{{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:16px}}.kpi{{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:10px 14px}}
.kpi b{{display:block;font-size:18px}}.chart{{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px;overflow-x:auto}}
.axis,.lane{{display:grid;grid-template-columns:140px minmax(700px,1fr)}}.axis div{{position:relative;height:22px}}
.tick{{position:absolute;font-size:11px;color:var(--muted);white-space:nowrap}}.lane{{border-top:1px solid var(--line)}}
.label{{padding:6px 8px 6px 0;font-weight:600}}.label small{{display:block;font-weight:400;color:var(--muted)}}
.track{{position:relative;height:38px}}.bar{{position:absolute;top:9px;height:20px;background:var(--bar);border-radius:3px;opacity:.9}}
.bar:hover{{opacity:1;outline:2px solid var(--text)}}</style></head><body><main>
<h1>Factory 2 — validated CRAFT schedule</h1>
<p>172 operations, 20 orders, frozen benchmark from <code>garment_fyp_db</code>. Exact replay PASS, physical VALID, scope VALID. Times from {origin:%d %b %Y %H:%M}. Hover a bar for details.</p>
<div class="kpis"><div class="kpi"><b>{span / 86400:.1f} days</b>schedule span</div><div class="kpi"><b>{on_time} / {len(summary)}</b>orders on time</div>
<div class="kpi"><b>{sum(s["due_before_schedule_start"] for s in summary)} / {len(summary)}</b>orders already past due at start</div><div class="kpi"><b>{len(rows)}</b>operations</div></div>
<div class="chart"><div class="axis"><div></div><div>{ticks}</div></div>{lane_html}</div></main></body></html>"""
(OUT / "gantt.html").write_text(page, encoding="utf-8")
print(json.dumps({"operations": len(rows), "orders": len(summary), "on_time": on_time,
                  "span_days": round(span / 86400, 2),
                  "due_before_start": sum(s["due_before_schedule_start"] for s in summary),
                  "output": str(OUT)}))
