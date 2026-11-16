"""Create a local environment without printing secrets or overwriting existing configuration."""
import argparse
import os
import secrets
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def create(mode,destination):
    template=ROOT/('.env.production.example' if mode=='production' else '.env.example')
    values={'JWT_VERIFICATION_KEY':secrets.token_hex(32),'METRICS_TOKEN':secrets.token_hex(32),'BACKEND_SERVICE_KEY':secrets.token_hex(32)}
    lines=[]
    for line in template.read_text().splitlines():
        name=line.split('=',1)[0]
        if name in values and (mode=='production' or name=='METRICS_TOKEN'):
            line=name+'='+values[name]
        lines.append(line)
    # Exclusive create protects an existing .env and credentials.
    fd=os.open(str(destination),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as file:
        file.write('\n'.join(lines)+'\n')

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode',choices=['demo','production'],default='demo')
    args=parser.parse_args()
    try:
        create(args.mode,ROOT/'.env')
    except FileExistsError:
        parser.exit(1,'An .env already exists. Edit it directly; it was not overwritten.\n')
    print('Created .env. For production set your model and embedding configuration, match the portal JWT settings, and share only the backend service credential through your private configuration channel. Secrets were not printed.')

