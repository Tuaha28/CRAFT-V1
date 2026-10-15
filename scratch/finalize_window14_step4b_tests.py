"""Run and persist the Step-4B focused regression result."""
from __future__ import annotations
import json, re, subprocess
from pathlib import Path

ROOT=Path('.')
OUT=ROOT/'outputs/crafter/factory2_172_v1/window14_step4b'
CMD=['venv/Scripts/python.exe','-m','pytest','-q','--basetemp','outputs/crafter/factory2_172_v1/window14_step4b/pytest_basetemp','tests/test_repair_objectives.py','tests/test_rolling_horizon_v2_step3a.py','tests/test_rolling_horizon_v2_step3b.py','tests/test_laya_shadow_v1.py','tests/test_boundary_aware_modes.py']
def main():
    result=subprocess.run(CMD,cwd=ROOT,capture_output=True,text=True)
    text=result.stdout+'\n'+result.stderr
    match=re.search(r'(\d+) passed(?:,\s*(\d+) failed)?(?:,\s*(\d+) skipped)?(?:,\s*(\d+) warnings?)?',text)
    warnings=int(match.group(4) or 0) if match else 0
    passed=int(match.group(1)) if match else 0
    failed=int(match.group(2) or 0) if match else (0 if result.returncode==0 else None)
    skipped=int(match.group(3) or 0) if match else 0
    out={'command':' '.join(CMD),'returncode':result.returncode,'passed':passed,'failed':failed,'skipped':skipped,'warnings':warnings,'stdout_tail':text[-4000:],'database_writes':0}
    (OUT/'focused_tests.json').write_text(json.dumps(out,indent=2)+'\n',encoding='utf-8')
    if result.returncode: raise SystemExit(result.returncode)
if __name__=='__main__': main()
