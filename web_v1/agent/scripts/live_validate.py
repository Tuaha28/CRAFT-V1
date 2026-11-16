"""Run explicit live provider checks. This makes real, potentially billable model calls."""
import argparse
import json
import os
import sys
import time
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.config import Settings
from app.retrieval import Knowledge

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api',default='http://localhost:8001')
    parser.add_argument('--cases',type=int,default=8)
    parser.add_argument('--check-web',action='store_true')
    parser.add_argument('--check-orders',action='store_true')
    parser.add_argument('--output',type=Path,default=ROOT/'reports/live_validation.json')
    args=parser.parse_args()
    settings=Settings()
    token=os.getenv('VALIDATION_CUSTOMER_TOKEN','')
    if not token:
        parser.exit(1,'Set VALIDATION_CUSTOMER_TOKEN privately to a short-lived customer JWT or demo access token.\n')
    if not 1<=args.cases<=24:
        parser.exit(1,'Choose 1 to 24 knowledge cases per run to stay within the default API limit.\n')
    headers={'Authorization':'Bearer '+token}
    rows=[]
    with httpx.Client(timeout=120) as client:
        health=client.get(args.api.rstrip('/')+'/health')
        health.raise_for_status()
        if health.json()['answer_mode']!='llm':
            parser.exit(1,'Live validation requires the API to run in llm mode; offline results are not model validation.\n')
        cases=json.loads((ROOT/'data/evaluation_queries.json').read_text())[:96:4][:args.cases]
        for case in cases:
            start=time.monotonic()
            r=client.post(args.api.rstrip('/')+'/chat',headers=headers,json={'message':case['question']})
            if r.status_code==200:
                answer=r.json()
                technical_pass=answer['mode']=='llm' and case['expected_section'] in {s['section_id'] for s in answer['sources']}
                rows.append({'question':case['question'],'technical_pass':technical_pass,'latency_seconds':round(time.monotonic()-start,2),
                             'answer':answer['answer'],'tool_trace':answer['tool_trace'],'human_review':'required'})
            else:
                rows.append({'question':case['question'],'technical_pass':False,'status_code':r.status_code,'human_review':'required'})
        if args.check_orders:
            r=client.post(args.api.rstrip('/')+'/chat',headers=headers,json={'message':'List my accessible orders.'})
            answer=r.json() if r.status_code==200 else {}
            rows.append({'question':'List my accessible orders','technical_pass':answer.get('mode')=='llm' and any(t['tool']=='list_my_orders' and t['status']=='ok' for t in answer.get('tool_trace',[])),
                         'human_review':'Check account ownership and freshness; no private order details are written to this report.'})
        if args.check_web:
            r=client.post(args.api.rstrip('/')+'/chat',headers=headers,json={'message':'Use the public company overview search to check the official company background.'})
            answer=r.json() if r.status_code==200 else {}
            rows.append({'question':'Public company overview search','technical_pass':answer.get('mode')=='llm' and any(t['tool']=='web_search_public' and t['status']=='ok' and t.get('available') is True for t in answer.get('tool_trace',[])),
                         'human_review':'Confirm that search was available and inspect the cited public source.'})
    report={'run_at_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'technical_passes':sum(r['technical_pass'] for r in rows),'checks':len(rows),
            'human_answer_review':'required; technical checks do not establish semantic accuracy', 'results':rows}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    os.chmod(args.output,0o600)
    print(f"Live technical checks passed: {report['technical_passes']}/{report['checks']}. Review the answers before approving model quality.")
    sys.exit(0 if report['technical_passes']==report['checks'] else 1)

