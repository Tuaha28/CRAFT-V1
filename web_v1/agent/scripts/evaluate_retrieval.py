import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.retrieval import Knowledge

kb=Knowledge()
queries=json.loads((ROOT/'data/evaluation_queries.json').read_text())
records=[]
for q in queries:
    found=kb.search(q['question'])
    records.append({'question':q['question'],'expected':q['expected_section'],
                    'top1':found[0]['section_id'] if found else None,
                    'hit_at_5':q['expected_section'] in {r['section_id'] for r in found}})
report={'evaluation_type':'lexical retrieval on authored handbook questions, not unseen-query or LLM quality evaluation',
        'questions':len(records),'top1_correct':sum(r['top1']==r['expected'] for r in records),
        'hit_at_5':sum(r['hit_at_5'] for r in records),'results':records}
(ROOT/'docs/retrieval_evaluation.json').write_text(json.dumps(report,indent=2))
print({k:v for k,v in report.items() if k!='results'})
