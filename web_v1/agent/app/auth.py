import re
import secrets
import jwt
from fastapi import HTTPException

class Identity:
    def __init__(self,subject,scopes):
        self.subject=subject
        self.scopes=frozenset(scopes)

    def require(self,scope):
        if scope not in self.scopes:
            raise HTTPException(403,'Your account does not have permission for this action')

def verify(settings,token):
    if not token or len(token)>8192:
        raise HTTPException(401,'Valid bearer token required')
    if settings.auth_mode=='demo':
        for credential,actor in settings.tokens.items():
            if secrets.compare_digest(credential,token):
                return Identity(actor,['customer:read'] if actor=='guest' else ['customer:read','customer:write'])
    else:
        try:
            claims=jwt.decode(token,settings.jwt_key,algorithms=[settings.jwt_algorithm],
                issuer=settings.jwt_issuer,audience=settings.jwt_audience,
                options={'require':['sub','exp','iat','nbf','iss','aud']},leeway=5)
            subject=claims['sub']
            if not isinstance(subject,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',subject):
                raise ValueError('Invalid subject')
            raw=claims.get('scope','')
            if not isinstance(raw,str):
                raise ValueError('Invalid scope')
            return Identity(subject,raw.split())
        except (jwt.PyJWTError,ValueError,TypeError):
            pass
    raise HTTPException(401,'Invalid or expired bearer token')
