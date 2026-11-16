from fastapi.testclient import TestClient
from examples.friend_backend import app

def test_example_backend_enforces_identity_and_replay():
    with TestClient(app) as client:
        assert client.get('/customer/orders/NC-1042',headers={'X-Customer-ID':'CUST-001'}).status_code==401
        headers={'Authorization':'Bearer reference-service-key','X-Customer-ID':'CUST-002'}
        assert client.get('/customer/orders/NC-1042',headers=headers).status_code==404
        headers['X-Customer-ID']='CUST-001'
        assert client.get('/customer/orders/NC-1042',headers=headers).json()['stage']=='stitching'
        import uuid
        headers['Idempotency-Key']=str(uuid.uuid4())
        body={'kind':'support','order_id':'NC-1042','description':'Please review the arrival estimate'}
        first=client.post('/customer/requests',headers=headers,json=body)
        second=client.post('/customer/requests',headers=headers,json=body)
        assert first.status_code==200 and first.json()==second.json()
        body['description']='Please review the changed colour'
        assert client.post('/customer/requests',headers=headers,json=body).status_code==409
