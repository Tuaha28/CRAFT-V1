import asyncio
from contextlib import asynccontextmanager
import httpx

class Network:
    """Bounded retries and durable circuit state. Only idempotent writes are retried."""
    def __init__(self,store):
        self.store=store
        self.client=None

    @asynccontextmanager
    async def connection(self):
        if self.client is not None:
            yield self.client
        else:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20,connect=5),follow_redirects=False) as client:
                yield client

    async def request(self,name,method,url,headers=None,payload=None,idempotent=False):
        if self.store.circuit_open(name):
            self.store.count('provider_'+name+'_blocked_total')
            raise httpx.ConnectError('Dependency circuit temporarily open')
        retries=3 if method=='GET' or idempotent else 1
        async with self.connection() as client:
            for attempt in range(retries):
                self.store.count('provider_'+name+'_requests_total')
                try:
                    if name in ('llm','embedding','search'):
                        response=await client.post(url,headers=headers,json=payload)
                    else:
                        response=await client.request(method,url,headers=headers,json=payload)
                    if len(response.content)>2_000_000:
                        raise ValueError('Dependency response exceeds accepted size')
                    transient=response.status_code in (429,500,502,503,504)
                    if transient and attempt+1<retries:
                        try:
                            delay=float(response.headers.get('Retry-After',.2*(2**attempt)))
                        except ValueError:
                            delay=.2*(2**attempt)
                        if 0<=delay<=1:
                            self.store.count('provider_'+name+'_retries_total')
                            await asyncio.sleep(delay)
                            continue
                    configuration_failure=(name in ('llm','embedding','search') and response.status_code in (401,403,404)) or (name=='backend' and response.status_code==401)
                    self.store.circuit_result(name,not (transient or configuration_failure))
                    if response.is_error:
                        self.store.count('provider_'+name+'_errors_total')
                    return response
                except (httpx.HTTPError,ValueError):
                    if attempt+1<retries:
                        self.store.count('provider_'+name+'_retries_total')
                        await asyncio.sleep(.2*(2**attempt))
                    else:
                        self.store.circuit_result(name,False)
                        self.store.count('provider_'+name+'_errors_total')
                        raise

