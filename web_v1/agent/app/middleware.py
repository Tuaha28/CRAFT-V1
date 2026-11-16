import asyncio
import json

class BodyLimitMiddleware:
    def __init__(self,app,limit):
        self.app,self.limit=app,limit

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':
            return await self.app(scope,receive,send)
        messages=[]
        size=0
        try:
            async with asyncio.timeout(10):
                while True:
                    msg=await receive()
                    if msg['type']=='http.disconnect':
                        return
                    size+=len(msg.get('body',b''))
                    if size>self.limit:
                        await self.error(send,413,'Request body exceeds the limit')
                        return
                    messages.append(msg)
                    if not msg.get('more_body',False):
                        break
        except TimeoutError:
            await self.error(send,408,'Request body timed out')
            return
        async def replay():
            if messages:
                return messages.pop(0)
            return await receive()
        await self.app(scope,replay,send)

    async def error(self,send,status,detail):
        await send({'type':'http.response.start','status':status,'headers':[(b'content-type',b'application/json'),(b'x-content-type-options',b'nosniff')]})
        await send({'type':'http.response.body','body':json.dumps({'detail':detail}).encode()})
