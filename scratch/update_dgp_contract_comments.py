from pathlib import Path
replacements={
 'graph/labels.py':[(
'''        The simulator advances planned and actual availability/precedence on
        separate clocks, and actual overruns cascade through the actual
        timeline. Current audited limitation: machine and employee IDENTITIES
        are selected using actual-clock availability before the planned
        placement is computed, so the stored plan is outcome-conditioned even
        though its subsequent clocks are separate.
''',
'''        The corrected two-phase simulator freezes the complete plan first:
        resource identities, planned timestamps and predecessor readiness use
        planned-only state. Phase B then advances independent actual machine,
        employee and predecessor clocks on those frozen assignments. Full
        staging proof over 176,187 rows showed that changing only the actual
        seed leaves the normalized planned projection identical.
''')],
 'models/train.py':[(
'''    # Computed from the TRAIN split only (never val) and from the labels as
    # they exist AFTER fix 2's masking and fix 3's corrected max(a-p)
    # formula, so it tracks the real balance rather than a stale constant.
''',
'''    # Computed from the TRAIN split only (never val) and from labels after
    # per-head validity masking. The operation-level target is
    # max(actual_end) - max(planned_end), deliberately not max(actual-planned)
    # across lots. The ratio may be below one when positives are the majority;
    # BCEWithLogitsLoss supports that and the saved value remains part of the
    # probability-calibration contract.
''')],
 'data_generation/seed_database_fixed.py':[(
'''      - on_leave (this lot's assigned employee flagged on leave at
        dispatch, i.e. covered by a substitute/less-familiar operator per
        the seeder's own tiered fallback): shifts the MEAN up by a flat
        +10% when true -- a reasoned value inside the ~25% total-friction
        envelope apparel-manufacturing planning literature attributes to
        exactly this kind of factor (among others), not an isolated citation.
''',
'''      - on_leave: shifts the MEAN up by a flat +10% when true. The current
        frozen-plan dataset excludes known-on-leave workers during Phase A, so
        Phase B passes False and this channel contributes zero rows. The
        parameter remains available for an explicit future absence scenario;
        it must not be described as active evidence in the current dataset.
''')]
}
for name,repls in replacements.items():
 p=Path(name); text=p.read_text(encoding='utf-8')
 for old,new in repls:
  if old not in text: raise SystemExit(f'target not found: {name}')
  text=text.replace(old,new)
 p.write_text(text,encoding='utf-8')
 print(name)
