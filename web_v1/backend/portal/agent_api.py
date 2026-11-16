"""The backend contract the customer assistant (web_v1/agent) calls: service key + the verified customer id.
Missing and not-owned orders answer the same 404; only customer-facing fields are returned."""
import json
import secrets
from functools import wraps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from . import orders, services
from .catalogue import PRODUCTS
from .models import CustomerAccount

PUBLIC = ('order_id', 'customer_id', 'style_code', 'quantity', 'colour', 'stage', 'progress_percent',
          'estimated_ready_date', 'estimated_arrival_date', 'destination', 'carrier', 'tracking_reference',
          'invoice_number', 'invoice_currency', 'invoice_amount', 'payment_status', 'customer_note', 'updated_at')


def _contract(order):
    return {k: order[k] for k in PUBLIC}


def service(view):
    @csrf_exempt
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        key = settings.CRAFT_AGENT_SERVICE_KEY or ''
        given = request.headers.get('Authorization', '')
        if not key or not secrets.compare_digest(given.encode(), f'Bearer {key}'.encode()):
            return JsonResponse({'detail': 'Service authentication required'}, status=401)
        customer = request.headers.get('X-Customer-ID', '')
        account = None
        if customer.startswith('CUST-') and customer[5:].isdigit():
            account = CustomerAccount.objects.select_related('user', 'dataset').filter(pk=int(customer[5:])).first()
        if account is None or not account.user.is_active:
            return JsonResponse({'detail': 'Unknown customer'}, status=403)
        return view(request, account, *args, **kwargs)
    return wrapped


@require_http_methods(['GET'])
@service
def products(request, account):
    return JsonResponse(PRODUCTS, safe=False)


@require_http_methods(['GET'])
@service
def order_list(request, account):
    return JsonResponse([_contract(o) for o in orders.customer_orders(account)], safe=False)


@require_http_methods(['GET'])
@service
def order_detail(request, account, ref):
    found = orders.customer_order(account, ref)
    if found is None:
        return JsonResponse({'detail': 'No accessible record found'}, status=404)
    return JsonResponse(_contract(found))


@require_http_methods(['POST'])
@service
def create_request(request, account):
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'detail': 'Invalid JSON'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'detail': 'Invalid JSON'}, status=400)
    try:
        req, created = services.submit(account, data, request.headers.get('Idempotency-Key'))
    except ValidationError as exc:
        return JsonResponse({'detail': '; '.join(exc.messages)}, status=400)
    except services.NotFound:
        return JsonResponse({'detail': 'No accessible record found'}, status=404)
    except services.Conflict as exc:
        return JsonResponse({'detail': str(exc)}, status=409)
    return JsonResponse(services.receipt(req), status=201 if created else 200)
