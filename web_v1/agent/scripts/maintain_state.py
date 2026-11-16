import argparse
import os
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.config import Settings
from app.store import Store

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('operation',choices=['backup','cleanup'])
    parser.add_argument('--destination',type=Path)
    args=parser.parse_args()
    settings=Settings()
    store=Store(settings.database,seed_demo=False)
    if args.operation=='backup':
        if not args.destination or args.destination.exists():
            parser.exit(1,'Provide a new backup destination with --destination.\n')
        args.destination.parent.mkdir(parents=True,exist_ok=True)
        store.backup(args.destination)
        os.chmod(args.destination,0o600)
        print('Backup completed. Store it privately outside the application host and test restoration.')
    else:
        store.cleanup(settings.conversation_days,settings.audit_days)
        print('Retention cleanup completed: conversation inactivity 30 days; local request and audit history 90 days.')

