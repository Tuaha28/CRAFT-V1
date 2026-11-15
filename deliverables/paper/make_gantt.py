"""Figure 2 of the CRAFT paper: Gantt chart of the CRAFT V3 schedule for case
30%-S1 (Kaggle run, craft_v3_schedule.json). One row per worker; each bar is a
productive work segment, coloured by order, so shift pauses appear as gaps and
crews as same-colour bars on several rows at once.

Usage: python make_gantt.py <craft_v3_schedule.json> <out.png> [days]"""
import json
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

DAY = 86400
rows = json.loads(open(sys.argv[1], encoding="utf-8").read())
days = float(sys.argv[3]) if len(sys.argv) > 3 else 14
orders = sorted({r["operation_key"].split("_")[0] for r in rows}, key=int)
palette = plt.get_cmap("tab10")
colour = {o: palette(i % 10) for i, o in enumerate(orders)}

segments = {}
for r in rows:
    order = r["operation_key"].split("_")[0]
    for w in r["resource_configuration"]["worker_ids"]:
        segments.setdefault(str(w), []).extend((a, b, order, r["resource_configuration"]["k"]) for a, b in r["worker_segments"])
workers = sorted(segments, key=lambda w: (min(a for a, *_ in segments[w]), int(w)))

plt.rcParams.update({"font.family": "Times New Roman", "font.size": 8})
fig, ax = plt.subplots(figsize=(7.0, 3.9), dpi=300)
for y, w in enumerate(workers):
    for a, b, order, _k in segments[w]:
        if a / DAY < days:
            ax.broken_barh([(a / DAY, (min(b, days * DAY) - a) / DAY)], (y - 0.38, 0.76), facecolors=colour[order],
                           edgecolors="black", linewidth=0.25)
ax.set_yticks(range(len(workers)))
ax.set_yticklabels([f"W{w}" for w in workers], fontsize=6)
ax.invert_yaxis()
ax.set_xlim(0, days)
ax.set_xticks(range(0, int(days) + 1))
ax.set_xlabel("Days from the start of the plan (10 Sept. 2026)")
ax.set_ylabel("Worker")
ax.grid(axis="x", color="#d0d0d0", linewidth=0.4)
ax.set_axisbelow(True)
used = sorted({o for w in workers for a, b, o, _k in segments[w] if a / DAY < days}, key=int)
ax.legend(handles=[Patch(facecolor=colour[o], edgecolor="black", linewidth=0.25, label=f"Order {o}") for o in used],
          loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=min(5, len(used)), frameon=False, fontsize=7)
fig.tight_layout()
fig.savefig(sys.argv[2], dpi=300)
print("written", sys.argv[2], len(workers), "workers", len(used), "orders shown")
