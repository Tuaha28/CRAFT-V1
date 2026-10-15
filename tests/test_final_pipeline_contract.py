"""Integration-contract regressions for the top-level production runner."""

from run_final_pipeline import _metric_line


def test_evaluation_output_formatter_accepts_current_structured_contract():
    # evaluate_gnn returns strings, None, dicts and lists as well as scalars.
    # The old unconditional float format crashed on the first such value.
    assert _metric_line('scalar', 0.25).endswith('0.2500')
    assert _metric_line('missing', None).endswith('null')
    assert '"meaning": "plan reproduction"' in _metric_line(
        'semantics', {'meaning': 'plan reproduction'})
    assert _metric_line('label', 'text').endswith('"text"')
