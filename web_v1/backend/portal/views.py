"""Portal API for the React pages: customers see their own orders, catalogue, requests and the assistant;
staff answer requests and admins create customer accounts."""
import secrets
import uuid
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from rest_framework import serializers
from rest_framework.decorators import api_view
from rest_framework.response import Response
from planning.models import Dataset
from planning.services import event
from . import agent_client, orders, services
from .catalogue import PRODUCTS
from .models import CustomerAccount, CustomerRequest

ASSISTANT_KEYS = ('answer', 'session_id', 'sources', 'pending_actions', 'mode')


def _account(request):
    account = getattr(request.user, 'customer_account', None)
    if account is None or not request.user.is_active:
        raise PermissionDenied('This page is for customers')
    return account


def _guard(view):
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except PermissionDenied as exc:
            return Response({'error': str(exc) or 'Not allowed'}, status=403)
        except ValidationError as exc:
            return Response({'error': '; '.join(exc.messages)}, status=400)
        except services.NotFound as exc:
            return Response({'error': str(exc)}, status=404)
        except services.Conflict as exc:
            return Response({'error': str(exc)}, status=409)
        except agent_client.AgentUnavailable as exc:
            return Response({'error': str(exc)}, status=503)
        except agent_client.AgentError as exc:
            return Response({'error': exc.detail}, status=exc.status if 400 <= exc.status < 500 else 502)
    wrapped.__name__ = view.__name__
    wrapped.__doc__ = view.__doc__
    return wrapped


@api_view(['GET'])
@_guard
def me(request):
    account = _account(request)
    return Response({'customer_id': account.customer_id, 'client_name': account.client_name,
                     'username': request.user.get_username(), 'data': account.dataset.label})


@api_view(['GET'])
@_guard
def order_list(request):
    return Response(orders.customer_orders(_account(request)))


@api_view(['GET'])
@_guard
def order_detail(request, ref):
    found = orders.customer_order(_account(request), ref)
    if found is None:
        raise services.NotFound('No accessible order found')
    return Response(found)


@api_view(['GET'])
@_guard
def products(request):
    _account(request)
    return Response(PRODUCTS)


class RequestIn(serializers.Serializer):
    kind = serializers.CharField(max_length=20)
    description = serializers.CharField(max_length=2000)
    order_id = serializers.CharField(max_length=20, required=False, allow_blank=True)
    style_code = serializers.CharField(max_length=40, required=False, allow_blank=True)
    quantity = serializers.IntegerField(required=False, allow_null=True)
    colour = serializers.CharField(max_length=100, required=False, allow_blank=True)
    destination = serializers.CharField(max_length=200, required=False, allow_blank=True)
    key = serializers.CharField(max_length=100, required=False, allow_blank=True)


@api_view(['GET', 'POST'])
@_guard
def my_requests(request):
    account = _account(request)
    if request.method == 'POST':
        data = RequestIn(data=request.data)
        data.is_valid(raise_exception=True)
        values = dict(data.validated_data)
        key = values.pop('key', '') or str(uuid.uuid4())
        req, created = services.submit(account, values, key)
        return Response(services.public_request(req), status=201 if created else 200)
    return Response([services.public_request(r) for r in account.requests.order_by('-created_at')[:200]])


class ChatIn(serializers.Serializer):
    message = serializers.CharField(max_length=4000)
    session_id = serializers.CharField(max_length=100, required=False, allow_null=True, allow_blank=True)


@api_view(['POST'])
@_guard
def chat(request):
    account = _account(request)
    data = ChatIn(data=request.data)
    data.is_valid(raise_exception=True)
    reply = agent_client.call(account, 'POST', '/chat', {'message': data.validated_data['message'].strip(),
                                                         'session_id': data.validated_data.get('session_id') or None})
    return Response({k: reply.get(k) for k in ASSISTANT_KEYS})


@api_view(['POST'])
@_guard
def confirm_action(request, action_id):
    account = _account(request)
    return Response(agent_client.call(account, 'POST', f'/requests/{action_id}/confirm'))


# ---------- staff ----------

@api_view(['GET'])
@_guard
def staff_requests(request):
    if not request.user.has_perm('portal.handle_request'):
        raise PermissionDenied('You cannot answer customer requests')
    found = CustomerRequest.objects.select_related('account__user', 'decided_by').order_by('status', '-created_at')
    status = request.query_params.get('status')
    if status:
        found = found.filter(status=status)
    return Response([services.public_request(r, staff=True) for r in found[:300]])


class DecisionIn(serializers.Serializer):
    decision = serializers.ChoiceField(choices=['ACCEPTED', 'DECLINED'])
    note = serializers.CharField(max_length=2000, required=False, allow_blank=True)


@api_view(['POST'])
@_guard
def staff_decide(request, pk):
    data = DecisionIn(data=request.data)
    data.is_valid(raise_exception=True)
    req = services.decide(request.user, pk, data.validated_data['decision'], data.validated_data.get('note', ''))
    return Response(services.public_request(req, staff=True))


class AccountIn(serializers.Serializer):
    username = serializers.RegexField(r'^[A-Za-z0-9_.-]{3,40}$')
    client_name = serializers.CharField(max_length=150)
    dataset_id = serializers.IntegerField()
    email = serializers.EmailField(required=False, allow_blank=True)


def _need_admin(user):
    if not user.has_perm('floor.manage_users'):
        raise PermissionDenied('Only an administrator manages customer accounts')


@api_view(['GET', 'POST'])
@_guard
def staff_accounts(request):
    _need_admin(request.user)
    if request.method == 'POST':
        data = AccountIn(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        dataset = Dataset.objects.filter(pk=v['dataset_id'], active=True).first()
        if dataset is None:
            raise ValidationError('Choose the factory data (30, 60 or 90% load)')
        if User.objects.filter(username__iexact=v['username']).exists():
            raise ValidationError('This user name is taken')
        client_name = v['client_name'].strip()
        if not orders.client_exists(dataset, client_name):
            raise ValidationError(f'No orders for "{client_name}" in {dataset.label}')
        password = secrets.token_urlsafe(12)
        with transaction.atomic():
            user = User.objects.create_user(v['username'], password=password, email=(v.get('email') or '').strip())
            account = CustomerAccount.objects.create(user=user, client_name=client_name, dataset=dataset,
                                                     created_by=request.user)
        event(None, request.user, 'CUSTOMER_ACCOUNT_CREATED', f'{user.username} added for {client_name}')
        return Response({**_public_account(account), 'password': password}, status=201)
    return Response([_public_account(a) for a in CustomerAccount.objects.select_related('user', 'dataset')
                     .order_by('client_name')])


def _public_account(a):
    return {'id': a.pk, 'customer_id': a.customer_id, 'username': a.user.get_username(), 'client_name': a.client_name,
            'data': a.dataset.label, 'active': a.user.is_active, 'created_at': a.created_at}


@api_view(['GET'])
@_guard
def staff_clients(request):
    _need_admin(request.user)
    try:
        dataset = Dataset.objects.get(pk=int(request.query_params['dataset']), active=True)
    except (KeyError, ValueError, Dataset.DoesNotExist):
        raise ValidationError('Choose the factory data') from None
    return Response(orders.clients(dataset, request.query_params.get('q', '').strip()[:60]))
