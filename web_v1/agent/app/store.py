import json
import sqlite3
import uuid
import time
import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from fastapi import HTTPException

def now():
    return datetime.now(timezone.utc).isoformat()

PRODUCTS=[
    {'style_code':'NC-TEE-180','name':'Cotton T shirt','fabric':'100% cotton jersey','gsm':180,'base_price_usd':4.8,'moq':500,'sizes':'XS-XXL'},
    {'style_code':'NC-POLO-220','name':'Cotton blend polo','fabric':'65% cotton 35% polyester pique','gsm':220,'base_price_usd':7.2,'moq':600,'sizes':'XS-XXL'},
    {'style_code':'NC-ACT-150','name':'Polyester training top','fabric':'100% polyester interlock','gsm':150,'base_price_usd':5.6,'moq':500,'sizes':'XS-XXL'},
    {'style_code':'NC-HOOD-300','name':'Cotton blend hoodie','fabric':'80% cotton 20% polyester fleece','gsm':300,'base_price_usd':15.5,'moq':400,'sizes':'S-XXL'},
]

ORDERS=[
    {'order_id':'NC-1042','customer_id':'CUST-001','style_code':'NC-TEE-180','quantity':1500,'colour':'navy','stage':'stitching','progress_percent':62,'estimated_ready_date':'2026-10-22','estimated_arrival_date':'2026-11-02','destination':'Amman, Jordan','carrier':None,'tracking_reference':None,'invoice_number':'INV-2026-1042','invoice_currency':'USD','invoice_amount':7200,'payment_status':'advance_verified','customer_note':'Production is in progress. Delivery dates remain estimates.','updated_at':'2026-10-12T09:00:00+00:00'},
    {'order_id':'NC-1043','customer_id':'CUST-001','style_code':'NC-POLO-220','quantity':1200,'colour':'white','stage':'shipped','progress_percent':100,'estimated_ready_date':'2026-10-08','estimated_arrival_date':'2026-10-18','destination':'Dubai, UAE','carrier':'Reference Freight','tracking_reference':'RF-1043-2026','invoice_number':'INV-2026-1043','invoice_currency':'USD','invoice_amount':8640,'payment_status':'paid_verified','customer_note':'Shipment departed. Arrival date is a logistics estimate.','updated_at':'2026-10-12T08:00:00+00:00'},
    {'order_id':'NC-2042','customer_id':'CUST-002','style_code':'NC-HOOD-300','quantity':800,'colour':'black','stage':'material_preparation','progress_percent':18,'estimated_ready_date':'2026-11-06','estimated_arrival_date':'2026-11-19','destination':'London, UK','carrier':None,'tracking_reference':None,'invoice_number':'INV-2026-2042','invoice_currency':'USD','invoice_amount':12400,'payment_status':'advance_pending','customer_note':'Material preparation is underway.','updated_at':'2026-10-12T08:00:00+00:00'},
]

class Store:
    def __init__(self, path, seed_demo=True):
        self.path=path
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS orders(order_id TEXT PRIMARY KEY,customer_id TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,customer_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT);
            CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY,customer_id TEXT,payload TEXT,expires_at TEXT,status TEXT,receipt TEXT);
            CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,customer_id TEXT,payload TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,customer_id TEXT,event TEXT,reference TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS limits(key TEXT PRIMARY KEY,starts_at REAL NOT NULL,count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS leases(key TEXT PRIMARY KEY,owner TEXT NOT NULL,expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS counters(key TEXT PRIMARY KEY,value REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS circuits(name TEXT PRIMARY KEY,failures INTEGER NOT NULL,open_until REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS schema_versions(version INTEGER PRIMARY KEY);
            CREATE INDEX IF NOT EXISTS pending_owner_status ON pending(customer_id,status,expires_at);
            ''')
            # Versioned, idempotent SQLite migrations preserve the original prototype state.
            for table,column in [('sessions','updated_at'),('messages','created_at'),('pending','created_at')]:
                columns={r['name'] for r in db.execute('PRAGMA table_info('+table+')')}
                if column not in columns:
                    db.execute('ALTER TABLE '+table+' ADD COLUMN '+column+' TEXT')
                    db.execute('UPDATE '+table+' SET '+column+'=?',(now(),))
            db.execute('INSERT OR IGNORE INTO schema_versions VALUES(2)')
            for order in ORDERS if seed_demo else []:
                db.execute('INSERT OR IGNORE INTO orders VALUES(?,?,?)',(order['order_id'],order['customer_id'],json.dumps(order)))

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path, timeout=10)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def order(self, actor, order_id):
        with self.db() as db:
            r=db.execute('SELECT payload FROM orders WHERE order_id=? AND customer_id=?',(order_id,actor)).fetchone()
        if not r:
            raise HTTPException(404,'No accessible order found')
        return json.loads(r['payload'])

    def orders(self, actor):
        with self.db() as db:
            return [json.loads(r['payload']) for r in db.execute('SELECT payload FROM orders WHERE customer_id=?',(actor,))]

    def session(self, actor, session_id=None):
        with self.db() as db:
            if session_id:
                r=db.execute('SELECT customer_id FROM sessions WHERE id=?',(session_id,)).fetchone()
                if not r or r['customer_id']!=actor:
                    raise HTTPException(404,'No accessible conversation found')
            else:
                session_id=str(uuid.uuid4())
                db.execute('INSERT INTO sessions(id,customer_id,updated_at) VALUES(?,?,?)',(session_id,actor,now()))
        return session_id

    def history(self, session_id):
        with self.db() as db:
            rows=db.execute('SELECT role,content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 12',(session_id,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def message(self, session_id, role, content):
        with self.db() as db:
            db.execute('INSERT INTO messages(session_id,role,content,created_at) VALUES(?,?,?,?)',(session_id,role,content,now()))
            db.execute('UPDATE sessions SET updated_at=? WHERE id=?',(now(),session_id))

    def log(self, actor, event, reference):
        with self.db() as db:
            db.execute('INSERT INTO audit(customer_id,event,reference,created_at) VALUES(?,?,?,?)',(actor,event,reference,now()))

    def prepare(self, actor, payload):
        action_id=str(uuid.uuid4())
        expires=(datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat()
        canonical=json.dumps(payload,sort_keys=True)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT id,expires_at FROM pending WHERE customer_id=? AND payload=? AND status=? AND expires_at>?',(actor,canonical,'pending',now())).fetchone()
            if existing:
                return {'action_id':existing['id'],'details':payload,'expires_at':existing['expires_at'],'status':'awaiting_confirmation'}
            db.execute('INSERT INTO pending(id,customer_id,payload,expires_at,status,receipt,created_at) VALUES(?,?,?,?,?,?,?)',(action_id,actor,canonical,expires,'pending',None,now()))
        self.log(actor,'request_prepared',action_id)
        return {'action_id':action_id,'details':payload,'expires_at':expires,'status':'awaiting_confirmation'}

    def pending(self, actor, action_id):
        with self.db() as db:
            r=db.execute('SELECT * FROM pending WHERE id=? AND customer_id=?',(action_id,actor)).fetchone()
        if not r:
            raise HTTPException(404,'No accessible request found')
        return dict(r)

    def confirm_local(self, actor, action_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            r=db.execute('SELECT * FROM pending WHERE id=? AND customer_id=?',(action_id,actor)).fetchone()
            if not r:
                raise HTTPException(404,'No accessible request found')
            if r['status']=='submitted':
                return json.loads(r['receipt'])
            if r['expires_at'] < now():
                raise HTTPException(410,'Confirmation expired; prepare a new request')
            request_id='REQ-'+uuid.uuid4().hex[:10].upper()
            receipt={'request_id':request_id,'status':'submitted','approval_status':'pending_review','created_at':now()}
            db.execute('INSERT INTO requests VALUES(?,?,?,?)',(request_id,actor,r['payload'],receipt['created_at']))
            db.execute('UPDATE pending SET status=?,receipt=? WHERE id=?',('submitted',json.dumps(receipt),action_id))
            db.execute('INSERT INTO audit(customer_id,event,reference,created_at) VALUES(?,?,?,?)',(actor,'request_submitted',request_id,now()))
        return receipt

    def save_remote_receipt(self, actor, action_id, receipt):
        with self.db() as db:
            db.execute('UPDATE pending SET status=?,receipt=? WHERE id=? AND customer_id=?',('submitted',json.dumps(receipt),action_id,actor))
        self.log(actor,'request_submitted',action_id)

    def limit(self,actor,limit,period=60,kind='api'):
        key=kind+':'+hashlib.sha256(actor.encode()).hexdigest()
        current=time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM limits WHERE key=?',(key,)).fetchone()
            if row and row['starts_at']+period>current:
                if row['count']>=limit:
                    retry=max(1,int(row['starts_at']+period-current)+1)
                    raise HTTPException(429,'Resource limit reached; retry later',headers={'Retry-After':str(retry)})
                db.execute('UPDATE limits SET count=count+1 WHERE key=?',(key,))
            else:
                db.execute('INSERT INTO limits VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET starts_at=excluded.starts_at,count=1',(key,current,1))

    def acquire_chat(self,actor,session_id,capacity,ttl):
        current=time.time()
        owner=str(uuid.uuid4())
        actor_hash=hashlib.sha256(actor.encode()).hexdigest()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM leases WHERE expires_at<=?',(current,))
            if db.execute('SELECT 1 FROM leases WHERE key=?',('session:'+session_id,)).fetchone():
                raise HTTPException(409,'A message is already processing in this conversation')
            if db.execute("SELECT count(*) FROM leases WHERE key LIKE 'worker:%'").fetchone()[0]>=capacity:
                raise HTTPException(503,'Assistant capacity is temporarily busy',headers={'Retry-After':'5'})
            if db.execute('SELECT 1 FROM leases WHERE key LIKE ?',('worker:'+actor_hash+':%',)).fetchone():
                raise HTTPException(429,'A message is already processing for your account',headers={'Retry-After':'5'})
            db.execute('INSERT INTO leases VALUES(?,?,?)',('session:'+session_id,owner,current+ttl))
            db.execute('INSERT INTO leases VALUES(?,?,?)',('worker:'+actor_hash+':'+owner,owner,current+ttl))
        return owner

    def release_chat(self,owner):
        with self.db() as db:
            db.execute('DELETE FROM leases WHERE owner=?',(owner,))

    def count(self,key,amount=1):
        with self.db() as db:
            db.execute('INSERT INTO counters VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value',(key,amount))

    def metric_values(self):
        with self.db() as db:
            return {r['key']:r['value'] for r in db.execute('SELECT * FROM counters')}

    def circuit_open(self,name):
        with self.db() as db:
            row=db.execute('SELECT open_until FROM circuits WHERE name=?',(name,)).fetchone()
        return bool(row and row['open_until']>time.time())

    def circuit_result(self,name,success):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT failures FROM circuits WHERE name=?',(name,)).fetchone()
            failures=0 if success else (row['failures'] if row else 0)+1
            opened=time.time()+20 if failures>=3 else 0
            db.execute('INSERT INTO circuits VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET failures=excluded.failures,open_until=excluded.open_until',(name,failures,opened))

    def cleanup(self,conversation_days=30,audit_days=90):
        cutoff=(datetime.now(timezone.utc)-timedelta(days=conversation_days)).isoformat()
        audit_cutoff=(datetime.now(timezone.utc)-timedelta(days=audit_days)).isoformat()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # Active conversations are excluded even if their prior activity is old.
            db.execute("DELETE FROM messages WHERE session_id IN (SELECT id FROM sessions WHERE updated_at<? AND ('session:'||id) NOT IN (SELECT key FROM leases WHERE expires_at>?))",(cutoff,time.time()))
            db.execute("DELETE FROM sessions WHERE updated_at<? AND ('session:'||id) NOT IN (SELECT key FROM leases WHERE expires_at>? )",(cutoff,time.time()))
            db.execute('DELETE FROM audit WHERE created_at<?',(audit_cutoff,))
            db.execute('DELETE FROM pending WHERE created_at<?',(audit_cutoff,))
            db.execute('DELETE FROM requests WHERE created_at<?',(audit_cutoff,))
            db.execute('DELETE FROM limits WHERE starts_at<?',(time.time()-86400,))
            db.execute('DELETE FROM leases WHERE expires_at<=?',(time.time(),))

    def backup(self,destination):
        with self.db() as source:
            with sqlite3.connect(str(destination)) as target:
                source.backup(target)

