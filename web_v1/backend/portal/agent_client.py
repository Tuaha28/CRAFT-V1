"""Talking to the customer assistant service (web_v1/agent): a short-lived HS256 token per customer and plain
JSON calls. The customer's identity comes only from their verified session, never from the browser."""
import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from django.conf import settings

TOKEN_SECONDS = 600


class AgentUnavailable(Exception):
    pass


class AgentError(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def mint(account, now=None):
    secret = settings.CRAFT_AGENT_JWT_SECRET or ''
    if len(secret.encode()) < 32:
        raise AgentUnavailable('The customer assistant is not set up (CRAFT_AGENT_JWT_SECRET)')
    now = int(now if now is not None else time.time())
    head = _b64(json.dumps({'alg': 'HS256', 'typ': 'JWT'}, separators=(',', ':')).encode())
    body = _b64(json.dumps({'sub': account.customer_id, 'scope': 'customer:read customer:write',
                            'iss': 'needlecraft-portal', 'aud': 'needlecraft-agent',
                            'iat': now, 'nbf': now, 'exp': now + TOKEN_SECONDS}, separators=(',', ':')).encode())
    sig = _b64(hmac.new(secret.encode(), f'{head}.{body}'.encode(), hashlib.sha256).digest())
    return f'{head}.{body}.{sig}'


def call(account, method, path, payload=None, timeout=90):
    """JSON from the agent, as this customer. AgentUnavailable when it cannot be reached; AgentError otherwise."""
    if not settings.CRAFT_AGENT_URL:
        raise AgentUnavailable('The customer assistant is not set up')
    request = urllib.request.Request(settings.CRAFT_AGENT_URL.rstrip('/') + path, method=method,
                                     data=json.dumps(payload).encode() if payload is not None else None,
                                     headers={'Authorization': 'Bearer ' + mint(account),
                                              'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read() or b'{}').get('detail') or exc.reason
        except ValueError:
            detail = exc.reason
        raise AgentError(exc.code, str(detail)[:300]) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise AgentUnavailable(f'The customer assistant is not available ({type(exc).__name__})') from None
