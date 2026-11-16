"""Build the optional semantic index using an OpenAI-compatible embedding endpoint."""
import hashlib
import json
import os
import sys
from pathlib import Path
import httpx
from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parents[1]
load_dotenv(ROOT/'.env')
raw=(ROOT/'data/chunks.json').read_bytes()
chunks=json.loads(raw)
model=os.getenv('EMBEDDING_MODEL','')
if not model:
    sys.exit('Set EMBEDDING_MODEL to an available embedding model before indexing.')
url=os.getenv('LLM_BASE_URL','https://api.openai.com/v1').rstrip('/')
key=os.getenv('LLM_API_KEY','')
headers={'Authorization':'Bearer '+key} if key else {}
vectors=[]
with httpx.Client(timeout=90) as client:
    for start in range(0,len(chunks),24):
        r=client.post(url+'/embeddings',headers=headers,json={'model':model,'input':[x['text'] for x in chunks[start:start+24]]})
        r.raise_for_status()
        data=sorted(r.json()['data'],key=lambda item:item['index'])
        if len(data)!=len(chunks[start:start+24]):
            sys.exit('Embedding response count mismatch; index was not written.')
        vectors.extend(x['embedding'] for x in data)
index={'corpus_sha256':hashlib.sha256(raw).hexdigest(),'model':model,'base_url':url,'vectors':vectors}
tmp=ROOT/'data/embeddings.tmp'
tmp.write_text(json.dumps(index),encoding='utf-8')
tmp.replace(ROOT/'data/embeddings.json')
print(f'Indexed {len(vectors)} chunks. Restart FastAPI to load the semantic index.')

