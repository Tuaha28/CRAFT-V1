"""Confirm a GNN training run finished properly rather than dying quietly.

Read-only. Run after models/train.py returns:
    .\\venv\\Scripts\\python.exe scratch\\verify_training_run.py

Checks the three artifacts a complete run must leave behind, that they agree
with each other, and that they are NEWER than the stale 3-epoch smoke-test
artifacts. Deliberately does NOT rely on graph_fingerprint: the Stage-1 seed
reproduces the smoke-test seed's operation set exactly, so the fingerprint is
identical across both and cannot detect staleness here.
"""
import hashlib
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CKPT = os.path.join(ROOT, 'outputs', 'checkpoints', 'gnn_multitask_latest.pth')
PREP = CKPT + '.preprocessing.json'
MANIFEST = os.path.join(ROOT, 'outputs', 'split_manifest.json')
LOG = os.path.join(ROOT, 'logs', 'gnn_training.log')

# The 3-epoch smoke-test checkpoint trained against
# garment_fyp_db_seed_v2_20260910_235135 on 2026-09-11 00:03. A real run must
# not leave this hash in place.
STALE_SHA = '136508026c55dc5d1594e6817754d66bd816200fae68e5ba740333d765700598'
EXPECTED_EPOCHS = int(os.environ.get('GNN_EPOCHS', 150))
PROFILE = os.path.join(ROOT, 'outputs', 'integration_audit', 'graph_profile_new_staging.json')
expected_profile = json.load(open(PROFILE, encoding='utf-8'))
EXPECTED_NODES = expected_profile['nodes']['operation']
EXPECTED_TRAIN = expected_profile['split']['train_operations']
EXPECTED_VAL = expected_profile['split']['validation_operations']
EXPECTED_GRAPH = expected_profile['graph_fingerprint']

ok = True


def chk(label, cond, detail=''):
    global ok
    ok = ok and bool(cond)
    print(f"  {label:<52}{'PASS' if cond else '*** FAIL ***':<14}{detail}")


def head(t):
    print('\n' + '=' * 78 + '\n' + t + '\n' + '=' * 78)


def sha256(p):
    with open(p, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


head('1. ARTIFACTS EXIST')
for p in (CKPT, PREP, MANIFEST):
    e = os.path.exists(p)
    chk(os.path.basename(p), e,
        time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(p))) if e else 'MISSING')
if not ok:
    print('\nArtifacts missing -- the run did not reach the end of train_gnn().')
    raise SystemExit(1)

head('2. CHECKPOINT IS NEW, NOT THE 3-EPOCH SMOKE TEST')
actual = sha256(CKPT)
chk('checkpoint sha256 differs from stale smoke test', actual != STALE_SHA, actual[:16] + '...')
chk('checkpoint newer than preprocessing is consistent',
    abs(os.path.getmtime(CKPT) - os.path.getmtime(PREP)) < 120,
    f'{abs(os.path.getmtime(CKPT)-os.path.getmtime(PREP)):.0f}s apart')

head('3. PREPROCESSING ARTIFACT BINDS TO THIS CHECKPOINT')
prep = json.load(open(PREP, encoding='utf-8'))
chk('preprocessing.checkpoint_sha256 == checkpoint',
    prep.get('checkpoint_sha256') == actual, str(prep.get('checkpoint_sha256'))[:16] + '...')
chk('operation_schema.count == 12', prep['operation_schema']['count'] == 12,
    str(prep['operation_schema']['count']))
chk('operation_schema.version == 2', prep['operation_schema']['version'] == 2)
chk('breakdown_scale present and positive',
    prep.get('breakdown_scale', 0) > 0, str(prep.get('breakdown_scale')))
# The real consumer's own gate -- evaluate.py / decision_engine.py call this.
from graph.feature_schema import load_preprocessing
try:
    load_preprocessing(CKPT)
    chk('load_preprocessing() accepts the pair (real consumer)', True)
except Exception as exc:
    chk('load_preprocessing() accepts the pair (real consumer)', False,
        f'{type(exc).__name__}: {str(exc)[:60]}')

head('4. SPLIT MANIFEST AGREES WITH THE CHECKPOINT')
m = json.load(open(MANIFEST, encoding='utf-8'))
chk('manifest.checkpoint_sha256 == checkpoint',
    m.get('checkpoint_sha256') == actual, str(m.get('checkpoint_sha256'))[:16] + '...')
chk(f'num_operation_nodes == {EXPECTED_NODES}', m.get('num_operation_nodes') == EXPECTED_NODES, str(m.get('num_operation_nodes')))
chk(f'num_train_operations == {EXPECTED_TRAIN}', m.get('num_train_operations') == EXPECTED_TRAIN, str(m.get('num_train_operations')))
chk(f'num_val_operations == {EXPECTED_VAL}', m.get('num_val_operations') == EXPECTED_VAL, str(m.get('num_val_operations')))
chk('train + val == total',
    (m.get('num_train_operations', 0) + m.get('num_val_operations', 0)) == m.get('num_operation_nodes'))
chk('split_seed == 42', m.get('split_seed') == 42, str(m.get('split_seed')))
chk('val_fraction == 0.2', m.get('val_fraction') == 0.2, str(m.get('val_fraction')))
chk('val order ids recorded', len(m.get('production_order_ids', [])) == m.get('num_val_orders'),
    f"{m.get('num_val_orders')} orders")
chk('delay_pos_weight recorded', m.get('delay_pos_weight') is not None, str(m.get('delay_pos_weight')))
chk('breakdown_scale matches preprocessing',
    m.get('breakdown_scale') == prep.get('breakdown_scale'), str(m.get('breakdown_scale')))
chk('priority_cutpoints frozen', bool(m.get('priority_cutpoints')),
    str(m.get('priority_cutpoints_source')))
chk('manifest graph fingerprint == accepted corrected graph',
    m.get('graph_fingerprint') == EXPECTED_GRAPH, str(m.get('graph_fingerprint'))[:24] + '...')

head('5. LOG SHOWS A COMPLETE RUN')
tail = open(LOG, encoding='utf-8', errors='replace').read()[-400000:]
lines = [l for l in tail.splitlines() if 'gnn_trainer' in l]
epochs = [int(mm.group(1)) for l in lines
          for mm in [re.search(r'Epoch (\d+)/(\d+) mem_rss_mb=', l)] if mm]
tot = [int(mm.group(2)) for l in lines
       for mm in [re.search(r'Epoch (\d+)/(\d+) mem_rss_mb=', l)] if mm]
run = []
for e in epochs:                      # keep only the final contiguous run
    if e == 1:
        run = []
    run.append(e)
chk(f'final run reached epoch {EXPECTED_EPOCHS}',
    bool(run) and max(run) == EXPECTED_EPOCHS, f'max epoch {max(run) if run else None}')
chk('no epochs skipped', run == list(range(1, len(run) + 1)), f'{len(run)} epoch lines')
chk('configured total was as expected',
    bool(tot) and tot[-1] == EXPECTED_EPOCHS, f'Epoch N/{tot[-1] if tot else "?"}')
chk('"Training Complete!" present', 'Training Complete!' in tail)
chk('"Split manifest written" present', 'Split manifest written to' in tail)

mem = [float(mm.group(1)) for l in lines
       for mm in [re.search(r'mem_rss_mb=([\d.]+)', l)] if mm][-len(run):] if run else []
if mem:
    growth = mem[-1] - mem[0]
    print(f'    RSS first/last epoch: {mem[0]:.1f} -> {mem[-1]:.1f} MB  (growth {growth:+.1f} MB)')
    chk('memory flat (no leak signature)', growth < 300, f'{growth:+.1f} MB over {len(mem)} epochs')

print('\n' + '=' * 78)
print('TRAINING RUN:', 'COMPLETE AND CONSISTENT' if ok else '*** INCOMPLETE / INCONSISTENT ***')
print('=' * 78)
raise SystemExit(0 if ok else 1)
