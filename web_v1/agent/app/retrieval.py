import json
import hashlib
import re
from difflib import get_close_matches
from pathlib import Path
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer, ENGLISH_STOP_WORDS
from sklearn.metrics.pairwise import cosine_similarity
from .config import ROOT

class Knowledge:
    def __init__(self):
        raw=(ROOT/'data/chunks.json').read_bytes()
        self.chunks=json.loads(raw)
        if not isinstance(self.chunks,list) or not self.chunks:
            raise ValueError('Knowledge corpus must contain chunks')
        if any(c.get('visibility')!='public' for c in self.chunks):
            raise ValueError('Customer corpus must contain only explicitly public knowledge')
        texts=[x['text'] for x in self.chunks]
        self.word=TfidfVectorizer(ngram_range=(1,2),stop_words='english',sublinear_tf=True)
        self.char=TfidfVectorizer(analyzer='char_wb',ngram_range=(3,5),sublinear_tf=True)
        self.w=self.word.fit_transform(texts)
        self.c=self.char.fit_transform(texts)
        self.embeddings=None
        self.semantic_config=None
        index=ROOT/'data/embeddings.json'
        if index.exists():
            data=json.loads(index.read_text())
            if data.get('corpus_sha256')!=hashlib.sha256(raw).hexdigest():
                raise ValueError('Semantic index is stale. Rebuild with scripts/index_embeddings.py')
            self.embeddings=np.array(data['vectors'],dtype=float)
            if self.embeddings.ndim!=2 or not np.all(np.isfinite(self.embeddings)) or self.embeddings.shape[0]!=len(self.chunks):
                raise ValueError('Semantic index does not match corpus')
            self.semantic_config=data

    def search(self, query, vector=None, limit=5):
        if vector is None:
            generic={'explain','tell','please','need','help','know','information','details','give','needle','craft'}
            words=set(re.findall(r'[a-z0-9]+',query.lower()))-set(ENGLISH_STOP_WORDS)-generic
            vocabulary={w for w in self.word.vocabulary_ if ' ' not in w}
            # TF-IDF drops unknown words. Do not let a lone generic verb make an unrelated query look relevant.
            if words and not any(w in vocabulary or get_close_matches(w,vocabulary,n=1,cutoff=.88) for w in words):
                return []
        lexical=.8*cosine_similarity(self.word.transform([query]),self.w)[0]+.2*cosine_similarity(self.char.transform([query]),self.c)[0]
        scores=lexical
        if vector is not None and self.embeddings is not None:
            semantic=cosine_similarity(np.array([vector]),self.embeddings)[0]
            scores=.55*semantic+.45*lexical
        selected=[]
        ranked=list(np.argsort(scores)[::-1])
        exact=[i for i,c in enumerate(self.chunks) if len(c['text'].split('\n'))>=3 and c['text'].split('\n')[1].strip().casefold()==query.strip().casefold()]
        # A literal FAQ question must rank ahead of related commercial text.
        ranked=exact+[i for i in ranked if i not in exact]
        for i in ranked:
            if lexical[i]<.045 and (vector is None or scores[i]<.25):
                continue
            c=self.chunks[int(i)]
            # Keep the strongest chunks; cite exact source rather than invented page references.
            selected.append({'chunk_id':c['chunk_id'],'section_id':c['id'],'title':c['title'],
                             'page':c['page'],'text':c['text'],'score':round(float(scores[i]),4),
                             'source':c.get('source','Needle_Craft_Customer_Handbook.docx'),'version':c['version']})
            if len(selected)>=limit:
                break
        return selected
