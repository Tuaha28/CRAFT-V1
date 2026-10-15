"""Pure selection boundary for captured TEST routes, never approval authority."""
from __future__ import annotations


def select_exact_routes(routes, selected_keys, approved_rows, factory_id):
    selected = list(selected_keys)
    approved = [row['operation_key'] for row in approved_rows]
    if (not selected or len(selected) != len(set(selected)) or
            len(approved) != len(set(approved)) or set(selected) & set(approved)):
        raise ValueError('CAPTURE_SELECTION_DUPLICATE_OR_APPROVED')
    for row in approved_rows:
        if row.get('resource_configuration', {}).get('factory_id') != factory_id:
            raise ValueError('CAPTURE_APPROVED_CROSS_FACTORY')
    wanted = set(selected) | set(approved)
    known = {row['operation_key'] for _, _, rows, _ in routes for row in rows}
    if not wanted <= known:
        raise ValueError('CAPTURE_SELECTION_OUTSIDE_UNSTARTED_TEST_ROUTES')
    result = []
    for _, _, rows, order in routes:
        chosen = [row for row in rows if row['operation_key'] in wanted]
        if not chosen:
            continue
        if [row['operation_key'] for row in chosen] != [
                row['operation_key'] for row in rows[:len(chosen)]]:
            raise ValueError('CAPTURE_EXTERNAL_PREDECESSOR_MISSING')
        result.append((chosen, order))
    return result
