"""Append-safe Option B JSONL logger. Optional caller-owned utility."""
import json
from pathlib import Path
def append(path, record):
    p=Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a",encoding="utf-8") as out: out.write(json.dumps(record,sort_keys=True,default=str)+"\n")
