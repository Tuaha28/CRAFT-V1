import os
import json
import httpx
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
st.set_page_config(page_title='Needle Craft Customer Assistant',page_icon='🧵',layout='centered')
st.title('Needle Craft')
st.caption('Customer assistant · Products, orders and service requests')
with st.sidebar:
    st.header('Customer access')
    url=st.text_input('Agent API',os.getenv('AGENT_API_URL','http://localhost:8001')).rstrip('/')
    account=st.selectbox('Customer account',['Customer 1','Customer 2','Visitor'])
    configured_tokens=json.loads(os.getenv('CUSTOMER_TOKENS','{}'))
    subject={'Customer 1':'CUST-001','Customer 2':'CUST-002','Visitor':'guest'}[account]
    preset=next((key for key,value in configured_tokens.items() if value==subject),'')
    token=st.text_input('Access token or portal JWT',preset,type='password')
    st.caption('Reference orders: Customer 1 has NC-1042 and NC-1043; Customer 2 has NC-2042.')
    if st.button('New conversation'):
        st.session_state.clear()
        st.rerun()

headers={'Authorization':'Bearer '+token}
identity=(url,token)
if st.session_state.get('identity')!=identity:
    st.session_state.update(identity=identity,messages=[],session_id=None,pending=[])

def call(method,path,body=None):
    try:
        with httpx.Client(timeout=150) as client:
            r=client.request(method,url+path,headers=headers,json=body)
        if r.status_code>=400:
            try:
                detail=r.json().get('detail','Request failed')
            except ValueError:
                detail='Request failed'
            st.error(str(detail))
            return None
        return r.json()
    except httpx.HTTPError:
        st.error('The assistant API is unavailable. Start the FastAPI service and check the address.')
        return None

for msg in st.session_state.messages:
    with st.chat_message(msg['role']):
        st.write(msg['content'])
if prompt:=st.chat_input('Ask about a garment, your order or a service request'):
    st.session_state.messages.append({'role':'user','content':prompt})
    with st.chat_message('user'):
        st.write(prompt)
    with st.chat_message('assistant'):
        with st.spinner('Checking the available information…'):
            result=call('POST','/chat',{'message':prompt,'session_id':st.session_state.session_id})
        if result:
            st.session_state.session_id=result['session_id']
            st.session_state.messages.append({'role':'assistant','content':result['answer']})
            st.session_state.pending.extend(result['pending_actions'])
            st.write(result['answer'])
            with st.expander('Sources and actions'):
                st.caption('Answer engine: '+result['mode'])
                for src in result['sources']:
                    location=f"Handbook page {src['page']}" if src['page'] else src['source']
                    st.write(f"{src['section_id']} · {src['title']} · {location}")
                st.json(result['tool_trace'])

with st.expander('Prepare a customer request'):
    st.caption('Describe the request, review the details, then confirm submission. Approval follows separately.')
    with st.form('request_form'):
        kind=st.selectbox('Request type',['quote','sample','order_change','support'])
        order_id=st.text_input('Order reference if applicable')
        style=st.selectbox('Style',['','NC-TEE-180','NC-POLO-220','NC-ACT-150','NC-HOOD-300'])
        quantity=st.number_input('Quantity',min_value=0,max_value=1000000,step=100)
        colour=st.text_input('Colour')
        destination=st.text_input('Delivery destination')
        description=st.text_area('Requirements or concern')
        submitted=st.form_submit_button('Review request')
    if submitted:
        body={'kind':kind,'description':description}
        for k,v in {'order_id':order_id.strip().upper(),'style_code':style,'quantity':quantity,'colour':colour,'destination':destination}.items():
            if v:
                body[k]=v
        action=call('POST','/requests/prepare',body)
        if action:
            st.session_state.pending.append(action)

for action in list(st.session_state.pending):
    st.subheader('Review before submission')
    st.json(action['details'])
    st.caption('Confirmation expires: '+action['expires_at'])
    cols=st.columns(2)
    if cols[0].button('Confirm submission',key='confirm-'+action['action_id']):
        receipt=call('POST',f"/requests/{action['action_id']}/confirm")
        if receipt:
            st.success(f"Request {receipt['request_id']} submitted for review. It has not been approved.")
            st.session_state.pending.remove(action)
    if cols[1].button('Dismiss',key='dismiss-'+action['action_id']):
        st.session_state.pending.remove(action)
        st.rerun()

