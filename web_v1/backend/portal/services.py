"""Customer requests: created once per idempotency key, reviewed by staff. Nothing here edits a plan."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from planning.services import event
from . import orders
from .models import CustomerRequest

FIELDS = ('kind', 'description', 'order_id', 'style_code', 'quantity', 'colour', 'destination')
KINDS = {k for k, _ in CustomerRequest.KINDS}


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def _clean(data):
    payload = {k: data.get(k) for k in FIELDS if data.get(k) not in (None, '')}
    if payload.get('kind') not in KINDS:
        raise ValidationError('Choose quote, sample, order change or support')
    description = str(payload.get('description') or '').strip()
    if len(description) < 10 or len(description) > 2000:
        raise ValidationError('Describe the request in 10 to 2000 characters')
    payload['description'] = description
    if payload['kind'] == 'order_change' and not payload.get('order_id'):
        raise ValidationError('An order change needs the order')
    if payload['kind'] == 'sample' and not payload.get('style_code'):
        raise ValidationError('A sample needs the style')
    if 'quantity' in payload:
        try:
            payload['quantity'] = int(payload['quantity'])
        except (TypeError, ValueError):
            raise ValidationError('Quantity must be a whole number') from None
        if not 0 < payload['quantity'] <= 1_000_000:
            raise ValidationError('Quantity must be between 1 and 1,000,000')
    for key in ('order_id', 'style_code', 'colour', 'destination'):
        if key in payload:
            payload[key] = str(payload[key]).strip()[:200]
    return payload


def receipt(req):
    return {'request_id': req.request_id, 'status': 'submitted', 'approval_status': 'pending_review',
            'created_at': req.created_at.isoformat()}


def submit(account, data, key):
    """(request, created). Same key + same payload returns the first request; a different payload is a Conflict."""
    key = str(key or '').strip()
    if not key or len(key) > 100:
        raise ValidationError('An idempotency key is required')
    payload = _clean(data)
    existing = CustomerRequest.objects.filter(account=account, idempotency_key=key).first()
    if existing is not None:
        if existing.payload != payload:
            raise Conflict('This request key was already used for a different request')
        return existing, False
    if payload.get('order_id') and orders.customer_order(account, payload['order_id']) is None:
        raise NotFound('No accessible order found')
    try:
        with transaction.atomic():
            req = CustomerRequest.objects.create(
                account=account, idempotency_key=key, payload=payload, kind=payload['kind'],
                description=payload['description'], order_id=payload.get('order_id', ''),
                style_code=payload.get('style_code', ''), quantity=payload.get('quantity'),
                colour=payload.get('colour', ''), destination=payload.get('destination', ''))
    except IntegrityError:  # the same key arrived twice at once
        return submit(account, data, key)
    event(None, None, 'CUSTOMER_REQUEST', f'{account.client_name} sent a {req.get_kind_display().lower()} request '
                                          f'({req.request_id})')
    return req, True


def decide(user, request_pk, decision, note=''):
    if not user.has_perm('portal.handle_request'):
        raise PermissionDenied('You cannot answer customer requests')
    if decision not in ('ACCEPTED', 'DECLINED'):
        raise ValidationError('Accept or decline the request')
    req = CustomerRequest.objects.select_related('account').filter(pk=request_pk).first()
    if req is None:
        raise NotFound('Request not found')
    if req.status != 'PENDING':
        raise ValidationError('This request was already answered')
    req.status, req.staff_note, req.decided_by, req.decided_at = decision, (note or '').strip()[:2000], user, timezone.now()
    req.save(update_fields=['status', 'staff_note', 'decided_by', 'decided_at'])
    event(None, user, 'CUSTOMER_REQUEST_' + decision, f'{req.request_id} from {req.account.client_name} '
                                                       f'{decision.lower()}')
    return req


def public_request(req, staff=False):
    data = {'id': req.pk, 'request_id': req.request_id, 'kind': req.kind, 'kind_label': req.get_kind_display(),
            'description': req.description, 'order_id': req.order_id or None, 'style_code': req.style_code or None,
            'quantity': req.quantity, 'colour': req.colour or None, 'destination': req.destination or None,
            'status': req.status, 'staff_note': req.staff_note, 'created_at': req.created_at,
            'decided_at': req.decided_at}
    if staff:
        data.update(client_name=req.account.client_name, customer=req.account.user.get_username(),
                    decided_by=req.decided_by.get_username() if req.decided_by else None)
    return data
