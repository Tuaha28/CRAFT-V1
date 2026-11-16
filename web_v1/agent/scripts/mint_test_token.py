"""Issue a short-lived local test JWT. The real portal must issue customer tokens."""
import argparse
import sys
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import jwt
from app.config import Settings

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Local test only; never expose a token-issuing endpoint to customers.')
    parser.add_argument('customer_id')
    parser.add_argument('--read-only',action='store_true')
    args=parser.parse_args()
    settings=Settings()
    if settings.auth_mode!='jwt' or settings.jwt_algorithm!='HS256' or len(settings.jwt_key)<32:
        parser.exit(1,'Configure a local HS256 JWT verification secret first. RS256 tokens must come from the portal private-key owner.\n')
    current=int(time.time())
    claims={'sub':args.customer_id,'iss':settings.jwt_issuer,'aud':settings.jwt_audience,'iat':current,'nbf':current,'exp':current+900,
            'scope':'customer:read' if args.read_only else 'customer:read customer:write'}
    print(jwt.encode(claims,settings.jwt_key,algorithm='HS256'))

