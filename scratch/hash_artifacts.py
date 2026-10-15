import hashlib,json,sys
from pathlib import Path
p=Path(sys.argv[1]); files={}
for f in sorted(p.rglob('*')):
    if f.is_file() and f.name!='artifact_hash_manifest.json': files[str(f.relative_to(p)).replace('\\','/')]=hashlib.sha256(f.read_bytes()).hexdigest()
(p/'artifact_hash_manifest.json').write_text(json.dumps({'files':files,'artifact_count':len(files),'status':'CRAFT_P2_SEARCH_LIMIT','database_writes':0},indent=2)+'\n')
