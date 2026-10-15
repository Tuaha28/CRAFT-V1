"""Evidence assessment, not a continuation planner or scheduling authorization."""
from .contracts import digest
from .residual_continuation import classify_residual_operation


def assess_residual_tails(snapshot):
    operations = []
    for op in sorted(snapshot['operations'], key=lambda item: item['operation_key']):
        if op['execution_state'] != 'PARTIALLY_EXECUTED':
            continue
        classification = classify_residual_operation(op)
        missing = classification['reasons']
        operations.append({
            'operation_key': op['operation_key'], 'classification': classification['classification'],
            'completed_quantity': op['quantity']['completed'],
            'in_flight_quantity': op['quantity']['in_flight'],
            'residual_not_started_quantity': op['quantity']['not_started'],
            'remaining_quantity': op['quantity']['remaining'],
            'residual_lot_ids': op['residual_lot_ids'],
            'known_in_flight_resource_associations': op['resource_holds'],
            'residual_lot_assignments_not_commitments': [
                {key: lot.get(key) for key in ('lot_id', 'state', 'quantity', 'line_id', 'machine_id', 'worker_id')}
                for lot in op['lots'] if lot['state'] != 'COMPLETED'],
            'residual_treatment': 'NON_MOVABLE_BOUNDARY_PENDING_PROVEN_CONTINUATION',
            'execution_boundary_end': op.get('completion_boundary'), 'optimizable': classification['optimizable'],
            'missing_evidence': missing,
            'successor': op['successor'],
            'successor_release_effect': 'BLOCKED_UNTIL_PHYSICAL_COMPLETION_BOUNDARY_PROVEN',
            'reason': 'Calendar walking requires known work and fixed resources; generated future actuals and planned rows are not validated residual commitments.',
        })
    result = {'snapshot_hash': snapshot['snapshot_hash'], 'partial_operations_total': len(operations),
              'resolved_committed_boundary': sum(o['classification'] == 'RESOLVED_COMMITTED_BOUNDARY' for o in operations),
              'resolved_splittable_residual': sum(o['classification'] == 'RESOLVED_SPLITTABLE_RESIDUAL' for o in operations),
              'boundary_unresolved': sum(o['classification'] == 'BOUNDARY_UNRESOLVED' for o in operations), 'operations': operations}
    result['assessment_hash'] = digest(result)
    return result
