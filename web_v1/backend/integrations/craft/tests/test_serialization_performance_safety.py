from datetime import datetime, timedelta
from decimal import Decimal
import json
from ..contracts import json_value, ordered_source, canonical_json


def test_sorting_preserves_original_canonical_contract():
    source = {'orders': [{1: Decimal('2.50')}, {1: 3}], 'operations': [],
              'lots': [{'at': datetime(2026, 9, 10), 'segments': [(3, 2), (1, 4)]}],
              'wip': [{'delta': timedelta(seconds=25)}], 'resources': {'workers': [{'id': 2}, {'id': 1}]}}
    original = json_value(source)
    for table in ('orders', 'operations', 'lots', 'wip'):
        original[table] = sorted(original[table], key=canonical_json)
    original['resources'] = {key: sorted(rows, key=canonical_json) for key, rows in sorted(original['resources'].items())}
    assert ordered_source(source) == original
    assert isinstance(source['orders'][0][1], Decimal)
    assert ordered_source(source)['lots'][0]['segments'] == [[3, 2], [1, 4]]
