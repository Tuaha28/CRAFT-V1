"""
Cheap, direct check (per instruction: five minutes of looking at the data,
not a deep investigation) of whether window 2 (orders [300:600) of the
1,029-order snapshot "20260825") is structurally distinct from windows 1
and 3 ([0:300), [600:900)) in ways that could plausibly explain
ALNS_REPAIR_SCORING_PROPOSAL.md Section 13's finding (corrected-formula
differentiation: window1=0.4%, window2=85.0%, window3=0.6%) -- this is
the SECOND independent test to single out window 2 specifically (after
the still-open move-trace anomaly), so worth checking directly before
treating it as an unexplained curiosity a second time.

Three real, checkable properties, each cheap:
  (a) garment-type mix (Products.category) per window
  (b) real release_date / order_deadline range and deadline-pressure
      (slack = deadline - release) per window
  (c) concentration of high-delay-risk operations: full delay_prob
      distribution (not just the sampled subset) per window, including
      what fraction of REAL operations exceed common risk thresholds

Read-only. Does not modify any ga/*, database/*, or model files.
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import statistics
from collections import Counter

import database.ga_snapshot as ga_snapshot
SNAPSHOT_NAME = "20260825"
ga_snapshot.activate_snapshot(SNAPSHOT_NAME)

import pandas as pd
import torch

from database.db import get_engine
from graph.graph_builder import FactoryGraphBuilder
from models.gnn import MultiTaskFactoryGNN
from ga.scheduler import make_key
from utils.config import GNN_HIDDEN_CHANNELS, OUTPUT_DIR

COMMIT_SIZE = 300
WINDOW_STARTS = [0, 300, 600]

builder = FactoryGraphBuilder()
graph, _ = builder.build_graph()

model = MultiTaskFactoryGNN(metadata=graph.metadata(), hidden_channels=GNN_HIDDEN_CHANNELS)
model.load_state_dict(torch.load(os.path.join(OUTPUT_DIR, "checkpoints", "gnn_multitask_latest.pth")))
model.eval()
with torch.no_grad():
    out = model(graph.x_dict, graph.edge_index_dict, graph['operation'].type_idx)
delay = torch.sigmoid(out['delay']).squeeze(-1)
preds_all = {k: delay[i].item() for i, k in enumerate(builder.op_map.keys())}

order_release = builder.df_routings.groupby('production_order_id')['release_date'].min()
all_orders_sorted = order_release.reset_index().sort_values(['release_date', 'production_order_id'])
all_ids = all_orders_sorted['production_order_id'].tolist()
print(f"total distinct orders: {len(all_ids)}")

# Real order-level metadata: category (garment type), deadline, release date.
order_meta = pd.read_sql(
    """
    SELECT po.production_order_id, po.scheduled_start_date AS release_date,
           pu.delivery_deadline AS order_deadline, pr.category
    FROM Production_Orders po
    JOIN Purchase_Orders pu ON pu.po_id = po.po_id
    JOIN Products pr ON pr.product_id = po.product_id
    WHERE po.status IN ('Pending', 'Processing', 'Delayed')
    """,
    get_engine(),
).drop_duplicates(subset=['production_order_id']).set_index('production_order_id')

for wi, wstart in enumerate(WINDOW_STARTS, start=1):
    ids = all_ids[wstart:wstart + COMMIT_SIZE]
    idset = set(ids)
    meta = order_meta.loc[order_meta.index.intersection(idset)]

    print()
    print("=" * 70)
    print(f"WINDOW {wi}  (orders [{wstart}:{wstart+COMMIT_SIZE}), n={len(ids)})")
    print("=" * 70)

    # (a) garment-type / category mix
    cat_counts = Counter(meta['category'].fillna('UNKNOWN'))
    total_cat = sum(cat_counts.values())
    print("(a) category mix:")
    for cat, cnt in cat_counts.most_common():
        print(f"    {cat}: {cnt} ({100.0*cnt/total_cat:.1f}%)")

    # (b) real date range + deadline pressure
    rel = pd.to_datetime(meta['release_date'])
    dl = pd.to_datetime(meta['order_deadline'])
    slack_days = (dl - rel).dt.total_seconds() / 86400.0
    print("(b) release_date range:", rel.min(), "to", rel.max())
    print("    order_deadline range:", dl.min(), "to", dl.max())
    print(f"    slack (deadline-release) days: mean={slack_days.mean():.1f} "
          f"median={slack_days.median():.1f} min={slack_days.min():.1f} max={slack_days.max():.1f}")

    # (c) delay_prob concentration over ALL real operations in this window
    sub = builder.df_routings[builder.df_routings['production_order_id'].isin(idset)]
    keys = [make_key(r['production_order_id'], r['operation_id']) for _, r in sub.iterrows()]
    probs = [preds_all[k] for k in keys if k in preds_all]
    print(f"(c) delay_prob over {len(probs)} real operations:")
    print(f"    mean={statistics.mean(probs):.4f} median={statistics.median(probs):.4f} "
          f"stdev={statistics.pstdev(probs):.4f} min={min(probs):.4f} max={max(probs):.4f}")
    for thresh in (0.3, 0.4, 0.5, 0.6):
        frac = sum(1 for p in probs if p > thresh) / len(probs)
        print(f"    fraction with delay_prob > {thresh}: {frac*100:.1f}%")

print("\nDONE")
