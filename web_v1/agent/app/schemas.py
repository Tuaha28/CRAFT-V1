from typing import Literal
from decimal import Decimal
from datetime import datetime, date
from pydantic import BaseModel, Field, ConfigDict, model_validator, field_validator

class ChatInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None

    @field_validator('message')
    @classmethod
    def nonblank(cls,value):
        if not value.strip():
            raise ValueError('Message cannot be blank')
        return value.strip()

class RequestInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind: Literal['quote','sample','order_change','support']
    order_id: str | None = Field(default=None, pattern=r'^NC-\d{4,12}$')
    style_code: str | None = Field(default=None, max_length=40)
    quantity: int | None = Field(default=None, gt=0, le=1000000,strict=True)
    colour: str | None = Field(default=None, max_length=100)
    destination: str | None = Field(default=None, max_length=200)
    description: str = Field(min_length=10, max_length=2000)

    @model_validator(mode='after')
    def validate_requirements(self):
        self.description=self.description.strip()
        if len(self.description)<10:
            raise ValueError('Description needs at least 10 nonblank characters')
        for key in ('style_code','colour','destination'):
            value=getattr(self,key)
            if value is not None and not value.strip():
                raise ValueError(key+' cannot be blank')
        if self.kind=='quote' and not all([self.style_code,self.quantity,self.colour,self.destination]):
            raise ValueError('Quote needs style_code, quantity, colour and destination')
        if self.kind=='sample' and not self.style_code:
            raise ValueError('Sample needs style_code')
        if self.kind=='order_change' and not self.order_id:
            raise ValueError('Order change needs order_id')
        return self

class PublicProduct(BaseModel):
    model_config=ConfigDict(extra='ignore')
    style_code: str = Field(min_length=1,max_length=40)
    name: str = Field(min_length=1,max_length=100)
    fabric: str = Field(max_length=500)
    gsm: int = Field(gt=0,le=2000)
    base_price_usd: Decimal = Field(ge=0,allow_inf_nan=False)
    moq: int = Field(gt=0,le=1000000)
    sizes: str = Field(max_length=100)

class PublicOrder(BaseModel):
    model_config=ConfigDict(extra='ignore')
    order_id: str = Field(pattern=r'^NC-\d{4,12}$')
    customer_id: str = Field(min_length=1,max_length=100)
    style_code: str = Field(max_length=40)
    quantity: int = Field(gt=0)
    colour: str = Field(max_length=100)
    stage: str = Field(min_length=1,max_length=80)
    progress_percent: int = Field(ge=0,le=100)
    estimated_ready_date: date | None = None
    estimated_arrival_date: date | None = None
    destination: str | None = Field(default=None,max_length=200)
    carrier: str | None = Field(default=None,max_length=100)
    tracking_reference: str | None = Field(default=None,max_length=100)
    invoice_number: str | None = Field(default=None,max_length=100)
    invoice_currency: str | None = Field(default=None,max_length=10)
    invoice_amount: Decimal | None = Field(default=None,ge=0,allow_inf_nan=False)
    payment_status: str | None = Field(default=None,max_length=100)
    customer_note: str | None = Field(default=None,max_length=1000)
    updated_at: datetime

class SubmissionReceipt(BaseModel):
    model_config=ConfigDict(extra='ignore')
    request_id: str = Field(pattern=r'^REQ-[A-Za-z0-9_-]{1,100}$')
    status: Literal['submitted']
    approval_status: Literal['pending_review']
    created_at: datetime | None = None

class GroundedAnswer(BaseModel):
    model_config=ConfigDict(extra='forbid')
    answer: str = Field(min_length=1,max_length=6000)
    source_ids: list[str] = Field(max_length=20)
    needs_handoff: bool

