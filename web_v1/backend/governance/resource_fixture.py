"""Explicit synthetic TEST resource state; never an observed factory record."""


def initial_state():
    return {'classification': 'SIMULATED_TEST', 'production_admission_enabled': False,
            'source_identity': 'SYNTHETIC_GLOBAL_RESOURCE_V1',
            'reference_time': '2026-09-10T06:00:00+05:00', 'timezone': 'Asia/Karachi',
            'calendar_revision': 'synthetic-shift-v1',
            'eligibility_revision': 'synthetic-single-machine-worker-v1',
            'machine_lines': {'1': 1}, 'worker_ids': ['1'],
            'selected_keys': ['A_residual'],
            'operation_ledger': {
                'A_residual': {'required': 100, 'completed': 40, 'in_flight': 0, 'remaining': 60},
                'B_residual': {'required': 100, 'completed': 40, 'in_flight': 0, 'remaining': 60}},
            'execution_states': {'A_residual': 'PARTIAL_WITH_UNSTARTED_TAIL',
                                 'B_residual': 'PARTIAL_WITH_UNSTARTED_TAIL'},
            'order_identities': {'A_residual': 'A', 'B_residual': 'B'},
            'fixed_commitments': [{
                'factory_id': 1, 'operation_key': 'event:synthetic-maintenance',
                'machine_id': 1, 'line_id': 1, 'start_seconds': 0, 'end_seconds': 300,
                'worker_segments': [['1', 0, 300]],
                'provenance': 'SYNTHETIC_TEST_HOLD_NOT_OBSERVED'}],
            'eligibility': {key: {'machines': [1], 'workers': ['1'], 'lines': [1]}
                            for key in ('A_residual', 'B_residual')},
            'calendar_windows': {key: [[300, 3600]] for key in ('A_residual', 'B_residual')},
            'route_releases': {'A_residual': 300, 'B_residual': 300},
            'route_edges': {'A_residual': {'predecessor': None, 'successor': None},
                            'B_residual': {'predecessor': None, 'successor': None}},
            'completed_keys': []}
