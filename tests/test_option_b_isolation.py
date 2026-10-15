import ast
from pathlib import Path
from ga.scheduler import GAScheduler

def test_option_b_off_has_no_required_option_b_import():
    tree=ast.parse(Path('ga/scheduler.py').read_text())
    assert not any((isinstance(n, ast.Import) and any(x.name.startswith('option_b') for x in n.names)) or (isinstance(n, ast.ImportFrom) and (n.module or '').startswith('option_b')) for n in ast.walk(tree))
    assert GAScheduler.__init__.__defaults__[-1] is False
