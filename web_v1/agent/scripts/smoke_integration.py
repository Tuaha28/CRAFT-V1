"""Exercise real local HTTP between two FastAPI services, with temporary agent state."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]
processes=[]
env=os.environ.copy()
for name in ['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy']:
    env.pop(name,None)
env.update(APP_ENV='demo',AUTH_MODE='demo',REQUIRE_SEMANTIC_INDEX='false',
           CUSTOMER_TOKENS='{"demo-customer-1":"CUST-001","demo-customer-2":"CUST-002","demo-visitor":"guest"}',
           LLM_MODE='offline',BACKEND_URL='http://127.0.0.1:18000',BACKEND_SERVICE_KEY='reference-service-key',EXAMPLE_SERVICE_KEY='reference-service-key')
with tempfile.TemporaryDirectory() as tmp:
    env['DATABASE_PATH']=str(Path(tmp)/'agent.sqlite3')
    try:
        for module,port in [('examples.friend_backend:app',18000),('app.main:app',18001)]:
            processes.append(subprocess.Popen([sys.executable,'-m','uvicorn',module,'--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
        with httpx.Client(trust_env=False,timeout=10) as client:
            for _ in range(150):
                try:
                    if client.get('http://127.0.0.1:18001/health').status_code==200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError('Local services did not start')
            base='http://127.0.0.1:18001'
            h={'Authorization':'Bearer demo-customer-1'}
            result=client.post(base+'/chat',headers=h,json={'message':'Where is NC-1042?'}).json()
            assert '62%' in result['answer']
            prepared=client.post(base+'/requests/prepare',headers=h,json={
                'kind':'quote','style_code':'NC-TEE-180','quantity':1000,'colour':'navy',
                'destination':'Amman, Jordan','description':'Adult shirts without added branding'}).json()
            path=base+'/requests/'+prepared['action_id']+'/confirm'
            receipt=client.post(path,headers=h).json()
            replay=client.post(path,headers=h).json()
            assert receipt==replay and receipt['approval_status']=='pending_review'
            private=client.post(base+'/chat',headers={'Authorization':'Bearer demo-customer-2'},json={'message':'Where is NC-1042?'}).json()
            assert 'No accessible' in private['answer'] and '62%' not in private['answer']
            print('PASS: two-service order lookup, account isolation, confirmed request and idempotent replay')
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

