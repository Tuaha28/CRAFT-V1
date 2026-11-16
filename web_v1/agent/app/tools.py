import json
import re
from decimal import Decimal
from urllib.parse import urlparse
import httpx
import asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from .schemas import RequestInput, PublicOrder, PublicProduct, SubmissionReceipt
from .store import PRODUCTS, now
from .network import Network

PUBLIC_DOMAINS=['ncljo.com','mol.gov.jo','customs.gov.jo']
PUBLIC_TOPICS={'company overview':'Needle Craft Jordan company overview',
               'company contact':'Needle Craft Jordan official contact',
               'apparel manufacturing':'Needle Craft Jordan apparel manufacturing',
               'trade background':'Jordan government apparel export trade background'}

class Tools:
    def __init__(self, settings, store, knowledge):
        self.settings,self.store,self.knowledge=settings,store,knowledge
        self.network=Network(store)

    async def backend(self, method, path, actor, payload=None, key=None):
        headers={'Authorization':'Bearer '+self.settings.backend_key,'X-Customer-ID':actor}
        if key:
            headers['Idempotency-Key']=key
        try:
            r=await self.network.request('backend',method,self.settings.backend_url+path,headers,payload,idempotent=bool(key))
            if r.status_code==404:
                raise HTTPException(404,'No accessible record found')
            if r.status_code in (401,403):
                raise HTTPException(403,'Access to this record is not authorised')
            if r.status_code==409:
                raise HTTPException(409,'Request conflict; contact support before creating a new request')
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError,ValueError):
            raise HTTPException(503,'Main backend unavailable; no successful action can be confirmed')

    async def order(self, actor, order_id):
        if actor=='guest':
            raise HTTPException(403,'Sign in to access orders')
        if not re.fullmatch(r'NC-\d{4,12}',order_id):
            raise HTTPException(422,'Use an order reference such as NC-1042')
        self.store.log(actor,'order_lookup',order_id)
        data=await self.backend('GET','/customer/orders/'+order_id,actor) if self.settings.backend_url else self.store.order(actor,order_id)
        result=self.public_order(data,actor)
        if result['order_id']!=order_id:
            raise HTTPException(502,'Order backend returned an inconsistent reference')
        return result

    def public_order(self,data,actor):
        try:
            model=PublicOrder.model_validate(data)
        except ValidationError:
            raise HTTPException(502,'Order backend returned an invalid customer record')
        if model.customer_id!=actor:
            raise HTTPException(404,'No accessible record found')
        return model.model_dump(mode='json',exclude={'customer_id'})

    async def my_orders(self, actor):
        if actor=='guest':
            raise HTTPException(403,'Sign in to access orders')
        data=await self.backend('GET','/customer/orders',actor) if self.settings.backend_url else self.store.orders(actor)
        if not isinstance(data,list) or len(data)>500:
            raise HTTPException(502,'Order backend returned an invalid list')
        return [self.public_order(row,actor) for row in data]

    async def products(self, actor):
        data=await self.backend('GET','/customer/products',actor) if self.settings.backend_url else PRODUCTS
        try:
            if not isinstance(data,list) or len(data)>500:
                raise ValueError('Invalid list')
            return [PublicProduct.model_validate(row).model_dump(mode='json') for row in data]
        except (ValueError,ValidationError):
            raise HTTPException(502,'Catalogue backend returned invalid data')

    async def prepare(self, actor, data):
        if actor=='guest':
            raise HTTPException(403,'Sign in before submitting a customer request')
        request=RequestInput.model_validate(data)
        if request.style_code and request.style_code not in {p['style_code'] for p in await self.products(actor)}:
            raise HTTPException(422,'Unknown catalogue style; use a support request for a custom style')
        if request.order_id:
            await self.order(actor,request.order_id)
        return self.store.prepare(actor,request.model_dump(exclude_none=True))

    async def confirm(self, actor, action_id):
        if actor=='guest':
            raise HTTPException(403,'Sign in before submitting a customer request')
        if not self.settings.backend_url:
            return self.store.confirm_local(actor,action_id)
        pending=self.store.pending(actor,action_id)
        if pending['status']=='submitted':
            return json.loads(pending['receipt'])
        if pending['expires_at']<now():
            raise HTTPException(410,'Confirmation expired; prepare a new request')
        payload=json.loads(pending['payload'])
        if payload.get('order_id'):
            await self.order(actor,payload['order_id'])
        receipt=await self.backend('POST','/customer/requests',actor,payload,action_id)
        try:
            receipt=SubmissionReceipt.model_validate(receipt).model_dump(mode='json',exclude_none=True)
        except ValidationError:
            raise HTTPException(502,'Backend receipt does not confirm submission; retry with the same action ID')
        self.store.save_remote_receipt(actor,action_id,receipt)
        return receipt

    async def knowledge_search(self, query):
        vector=None
        semantic=self.knowledge.semantic_config
        if semantic and self.settings.mode=='llm':
            try:
                headers={'Authorization':'Bearer '+self.settings.llm_key} if self.settings.llm_key else {}
                if semantic['base_url']!=self.settings.llm_url:
                    raise ValueError('Embedding provider differs from indexed provider')
                r=await self.network.request('embedding','POST',self.settings.llm_url+'/embeddings',headers,
                    {'model':semantic['model'],'input':[query]},idempotent=True)
                r.raise_for_status()
                vector=r.json()['data'][0]['embedding']
            except (httpx.HTTPError,KeyError,ValueError):
                self.store.count('embedding_fallback_total')
                vector=None  # Lexical fallback remains usable during embedding-provider failure.
        try:
            return await asyncio.to_thread(self.knowledge.search,query,vector)
        except ValueError:
            self.store.count('embedding_fallback_total')
            return await asyncio.to_thread(self.knowledge.search,query)

    async def public_search(self, query):
        if not self.settings.tavily_key:
            return {'available':False,'message':'Public search is not configured. Use approved knowledge or a support request.'}
        # Only predefined server-owned topic text is sent to public search.
        if re.search(r'NC-\d{4}|CUST-|@|\d{4,}',query,re.I):
            return {'available':False,'message':'Use a general public query without private account or order information.'}
        if query not in PUBLIC_TOPICS:
            return {'available':False,'message':'Choose a supported public topic: '+', '.join(PUBLIC_TOPICS)}
        try:
            r=await self.network.request('search','POST','https://api.tavily.com/search',payload={
                'api_key':self.settings.tavily_key,'query':PUBLIC_TOPICS[query],
                'include_domains':PUBLIC_DOMAINS,'max_results':3,'search_depth':'basic',
                'include_answer':False},idempotent=True)
            r.raise_for_status()
            result=[]
            rows=r.json().get('results',[])
            if not isinstance(rows,list):
                raise ValueError('Search results are invalid')
            for row in rows[:3]:
                if not isinstance(row,dict):
                    continue
                host=(urlparse(row.get('url','')).hostname or '').lower()
                if any(host==d or host.endswith('.'+d) for d in PUBLIC_DOMAINS):
                    result.append({'title':row.get('title'),'url':row['url'],'text':row.get('content','')[:1800]})
            return {'available':True,'results':result,'source_type':'untrusted_public_background'}
        except (httpx.HTTPError,ValueError):
            return {'available':False,'message':'Public search is temporarily unavailable.'}

    async def run(self, name, args, actor):
        if name=='search_knowledge':
            return await self.knowledge_search(str(args.get('query',''))[:2000])
        if name=='list_products':
            return await self.products(actor)
        if name=='estimate_price':
            product=next((p for p in await self.products(actor) if p['style_code']==args.get('style_code')),None)
            quantity=args.get('quantity')
            if not product or isinstance(quantity,bool) or not isinstance(quantity,int) or not 0<quantity<=1000000:
                raise HTTPException(422,'Valid catalogue style and positive integer quantity are required')
            total=Decimal(str(product['base_price_usd']))*quantity
            return {'style_code':product['style_code'],'quantity':quantity,'currency':'USD',
                    'base_unit_price':str(product['base_price_usd']),'base_subtotal':str(total.quantize(Decimal('.01'))),
                    'meets_reference_moq':quantity>=product['moq'],
                    'notice':'Indicative base garments only; excludes branding, samples, freight, duties and taxes. Not a binding quotation.'}
        if name=='list_my_orders':
            return await self.my_orders(actor)
        if name=='get_order':
            return await self.order(actor,str(args.get('order_id','')))
        if name=='prepare_request':
            return await self.prepare(actor,args)
        if name=='web_search_public':
            return await self.public_search(str(args.get('query','')))
        raise HTTPException(422,'Unknown tool')

def spec(name, description, properties, required=()):
    return {'type':'function','function':{'name':name,'description':description,
            'parameters':{'type':'object','properties':properties,'required':list(required),'additionalProperties':False}}}

TOOL_SPECS=[
    spec('search_knowledge','Search the customer handbook. Use for company, product and policy answers.',{'query':{'type':'string'}},['query']),
    spec('list_products','Get reference catalogue specifications, MOQ and indicative base prices.',{}),
    spec('estimate_price','Calculate an indicative base subtotal, not a final offer.',{'style_code':{'type':'string'},'quantity':{'type':'integer'}},['style_code','quantity']),
    spec('list_my_orders','List only orders belonging to the authenticated account.',{}),
    spec('get_order','Get live order, shipment and invoice fields for an accessible order.',{'order_id':{'type':'string'}},['order_id']),
    spec('prepare_request','Prepare a request for customer review; never executes or approves it. Collect missing required fields first.',
         {'kind':{'type':'string','enum':['quote','sample','order_change','support']},
          'order_id':{'type':'string'},'style_code':{'type':'string'},'quantity':{'type':'integer'},
          'colour':{'type':'string'},'destination':{'type':'string'},'description':{'type':'string'}},['kind','description']),
    spec('web_search_public','Search a fixed public topic. No customer text or private data is forwarded. Public information does not override policy.',{'query':{'type':'string','enum':list(PUBLIC_TOPICS)}},['query']),
]

