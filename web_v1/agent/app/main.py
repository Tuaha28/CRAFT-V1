import asyncio
import logging
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from .config import Settings
from .store import Store
from .retrieval import Knowledge
from .tools import Tools
from .agent import Agent
from .schemas import ChatInput, RequestInput
from .auth import verify
from .middleware import BodyLimitMiddleware

logger=logging.getLogger('needlecraft')
if not logger.handlers:
    class SafeFormatter(logging.Formatter):
        def format(self,record):
            return json.dumps({'level':record.levelname,'event':record.getMessage()})
    handler=logging.StreamHandler()
    handler.setFormatter(SafeFormatter())
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate=False
security=HTTPBearer(auto_error=False)

def create_app(settings=None):
    settings=settings or Settings()
    settings.validate()
    store=Store(settings.database,seed_demo=settings.environment=='demo')
    knowledge=Knowledge()
    if settings.environment=='production' and not settings.allow_reference and any(c.get('approval')!='approved' for c in knowledge.chunks):
        raise ValueError('Knowledge needs approval, or explicitly ALLOW_REFERENCE_DATA=true for the FYP reference model')
    if settings.require_semantic and knowledge.embeddings is None:
        raise ValueError('Build a semantic index before starting this configuration')
    tools=Tools(settings,store,knowledge)
    agent=Agent(settings,store,tools)

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=httpx.Timeout(20,connect=5),limits=httpx.Limits(max_connections=30,max_keepalive_connections=15),follow_redirects=False) as client:
            tools.network.client=client
            yield
            tools.network.client=None

    app=FastAPI(title='Needle Craft Customer Assistant',version='0.2.1',lifespan=lifespan,
                description='Grounded customer assistance with verified identity and confirmed requests.',
                docs_url='/docs' if settings.environment=='demo' else None,
                openapi_url='/openapi.json' if settings.environment=='demo' else None)
    app.state.store,app.state.tools,app.state.agent=store,tools,agent
    app.add_middleware(BodyLimitMiddleware,limit=settings.max_body_bytes)
    app.add_middleware(CORSMiddleware,allow_origins=settings.allowed_origins,
                       allow_methods=['GET','POST'],allow_headers=['Authorization','Content-Type'],allow_credentials=False)

    @app.middleware('http')
    async def request_id(request: Request, call_next):
        rid=str(uuid.uuid4())
        start=time.monotonic()
        try:
            response=await call_next(request)
        except sqlite3.Error:
            response=JSONResponse(status_code=503,content={'detail':'Assistant state is temporarily unavailable'})
        except Exception:
            logger.error('request=%s internal_error',rid)
            response=JSONResponse(status_code=500,content={'detail':'An internal error occurred','request_id':rid})
        response.headers['X-Request-ID']=rid
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Cache-Control']='no-store'
        route=getattr(request.scope.get('route'),'path','unmatched')
        duration=time.monotonic()-start
        logger.info('request=%s method=%s route=%s status=%s duration_ms=%.1f',rid,request.method,route,response.status_code,duration*1000)
        if route not in ('/health','/readyz','/metrics'):
            try:
                store.count('http_requests_total')
                store.count('http_duration_seconds_sum',duration)
                if response.status_code>=500:
                    store.count('http_errors_total')
            except sqlite3.Error:
                pass
        return response

    async def identity(credentials: HTTPAuthorizationCredentials | None=Depends(security)):
        identity=verify(settings,credentials.credentials if credentials else '')
        await asyncio.to_thread(store.limit,identity.subject,settings.rate_per_minute)
        return identity

    async def reader(owner=Depends(identity)):
        owner.require('customer:read')
        return owner.subject

    async def writer(owner=Depends(identity)):
        owner.require('customer:write')
        return owner.subject

    @app.get('/health')
    async def health():
        return {'status':'ok','version':'0.2.1','answer_mode':settings.mode,
                'order_source':'main_backend' if settings.backend_url else 'reference_database',
                'retrieval':'semantic_hybrid' if knowledge.embeddings is not None and settings.mode=='llm' else 'lexical_hybrid',
                'public_search_configured':bool(settings.tavily_key)}

    @app.get('/readyz')
    async def ready():
        try:
            with store.db() as db:
                db.execute('SELECT 1').fetchone()
            if (settings.mode=='llm' and store.circuit_open('llm')) or (settings.backend_url and store.circuit_open('backend')):
                raise HTTPException(503,'A required dependency is temporarily unavailable')
        except sqlite3.Error:
            raise HTTPException(503,'State database unavailable')
        return {'status':'ready','live_validation':'Run scripts/live_validate.py against configured providers'}

    @app.get('/metrics',response_class=PlainTextResponse)
    async def metrics(credentials: HTTPAuthorizationCredentials | None=Depends(security)):
        if not settings.metrics_token or not credentials or not secrets.compare_digest(credentials.credentials,settings.metrics_token):
            raise HTTPException(401,'Monitoring authentication required')
        values=await asyncio.to_thread(store.metric_values)
        return ''.join('needlecraft_'+key+' '+str(value)+'\n' for key,value in sorted(values.items()))

    @app.get('/products')
    async def products(owner=Depends(reader)):
        return await tools.products(owner)

    @app.get('/orders')
    async def orders(owner=Depends(reader)):
        return await tools.my_orders(owner)

    @app.post('/chat')
    async def chat(data: ChatInput,identity_data=Depends(identity)):
        identity_data.require('customer:read')
        owner=identity_data.subject
        sid=await asyncio.to_thread(store.session,owner,data.session_id)
        lease=await asyncio.to_thread(store.acquire_chat,owner,sid,settings.global_concurrency,settings.max_chat_seconds+30)
        try:
            await asyncio.to_thread(store.limit,owner,settings.daily_chats,86400,'chat_daily')
            return await asyncio.wait_for(agent.chat(owner,data.message,sid,allow_write='customer:write' in identity_data.scopes),timeout=settings.max_chat_seconds)
        except TimeoutError:
            store.count('chat_timeout_total')
            raise HTTPException(504,'Assistant deadline reached; retry or contact support')
        finally:
            await asyncio.to_thread(store.release_chat,lease)

    @app.post('/requests/prepare')
    async def prepare(data: RequestInput,owner=Depends(writer)):
        return await tools.prepare(owner,data.model_dump(exclude_none=True))

    @app.post('/requests/{action_id}/confirm')
    async def confirm(action_id: uuid.UUID,owner=Depends(writer)):
        return await tools.confirm(owner,str(action_id))

    return app

app=create_app()
