"""Runnable example of the main application's customer API contract."""
import json
import os
import secrets
import uuid
from fastapi import FastAPI, Header, HTTPException, Depends
from app.config import ROOT
from app.schemas import RequestInput
from app.store import Store, PRODUCTS, now

app=FastAPI(title='Scheduling backend customer API example')
store=Store(str(ROOT/'data/friend.sqlite3'))
service_key=os.getenv('EXAMPLE_SERVICE_KEY','reference-service-key')
with store.db() as db:
    db.execute('CREATE TABLE IF NOT EXISTS integrated_requests(customer_id TEXT,idem_key TEXT,payload TEXT,receipt TEXT,PRIMARY KEY(customer_id,idem_key))')

def customer(authorization: str=Header(default=''),x_customer_id: str=Header(default='')):
    if not secrets.compare_digest(authorization,'Bearer '+service_key):
        raise HTTPException(401,'Valid service authentication required')
    if x_customer_id not in ['CUST-001','CUST-002','guest']:
        raise HTTPException(403,'Unknown customer context')
    return x_customer_id

@app.get('/customer/products')
def catalogue(actor=Depends(customer)):
    return PRODUCTS

@app.get('/customer/orders')
def orders(actor=Depends(customer)):
    if actor=='guest':
        raise HTTPException(403,'Sign in required')
    return store.orders(actor)

@app.get('/customer/orders/{order_id}')
def order(order_id: str,actor=Depends(customer)):
    if actor=='guest':
        raise HTTPException(403,'Sign in required')
    return store.order(actor,order_id)

@app.post('/customer/requests')
def submit(data: RequestInput,actor=Depends(customer),idempotency_key: str=Header(min_length=1,max_length=100)):
    if actor=='guest':
        raise HTTPException(403,'Sign in required')
    if data.order_id:
        store.order(actor,data.order_id)
    if data.style_code and data.style_code not in {p['style_code'] for p in PRODUCTS}:
        raise HTTPException(422,'Unknown style')
    payload=json.dumps(data.model_dump(exclude_none=True),sort_keys=True)
    with store.db() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT payload,receipt FROM integrated_requests WHERE customer_id=? AND idem_key=?',(actor,idempotency_key)).fetchone()
        if row:
            if row['payload']!=payload:
                raise HTTPException(409,'Idempotency key was used for different details')
            return json.loads(row['receipt'])
        receipt={'request_id':'REQ-'+uuid.uuid4().hex[:10].upper(),'status':'submitted','approval_status':'pending_review','created_at':now()}
        db.execute('INSERT INTO integrated_requests VALUES(?,?,?,?)',(actor,idempotency_key,payload,json.dumps(receipt)))
        return receipt

