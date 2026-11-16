import json
import httpx
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app

def test_groq_separates_tools_from_strict_output(tmp_path,monkeypatch):
    for name in ['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy']:
        monkeypatch.delenv(name,raising=False)
    settings=Settings(database=str(tmp_path/'state.sqlite3'),mode='llm',llm_url='https://api.groq.com/openai/v1',llm_model='openai/gpt-oss-20b',llm_key='test',environment='demo',auth_mode='demo',backend_url='',tokens={'one':'CUST-001'})
    calls=[]
    async def fake(self,url,**kwargs):
        payload=kwargs['json']; calls.append(payload)
        if 'tools' in payload:
            assert 'response_format' not in payload
            content='The reference minimum is 500 pieces.'
        else:
            assert payload['response_format']['json_schema']['strict'] is True
            assert 'tool_choice' not in payload
            content=json.dumps({'answer':'The reference minimum is 500 pieces per style and colour.','source_ids':['KB-04'],'needs_handoff':False})
        return httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':content}}]},request=httpx.Request('POST',url))
    monkeypatch.setattr(httpx.AsyncClient,'post',fake)
    with TestClient(create_app(settings)) as client:
        result=client.post('/chat',headers={'Authorization':'Bearer one'},json={'message':'What is the T shirt minimum order quantity?'}).json()
    assert result['mode']=='llm' and '500' in result['answer']
    assert len(calls)==2
