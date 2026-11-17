from datetime import date
from rest_framework.decorators import api_view
from rest_framework.response import Response
from planning.models import Dataset
from . import queries
from .db import ErpUnavailable, cached, live_database, read_only

CACHE_SECONDS = 300


def needs(*perms):
    """403 unless the user holds one of these permissions (least privilege per page)."""
    def wrap(view):
        def inner(request, *args, **kwargs):
            if not any(request.user.has_perm(p) for p in perms):
                return Response({'error': 'Your role cannot see this page'}, status=403)
            return view(request, *args, **kwargs)
        inner.__name__, inner.__doc__ = view.__name__, view.__doc__
        return inner
    return wrap


def _run(request, dataset_id, factory_id, name, compute, cache=True):
    """Resolve the capture's database and reference date, then run one read model."""
    dataset = Dataset.objects.filter(pk=dataset_id).first()
    if dataset is None or not any(f['factory_id'] == factory_id for f in dataset.factories):
        return Response({'error': 'Capture or factory not found'}, status=404)
    ref = date.fromisoformat(dataset.reference_time[:10]) if dataset.reference_time else date.today()

    def work():
        with read_only(live_database(dataset)) as cursor:
            return compute(cursor, factory_id, ref)
    key = (live_database(dataset), factory_id, name, tuple(sorted(request.query_params.items())))
    try:
        data = cached(key, CACHE_SECONDS, work) if cache else work()
    except ErpUnavailable as exc:
        return Response({'error': str(exc)}, status=503)
    if data is None:
        return Response({'error': 'Not found'}, status=404)
    return Response({'database': dataset.database, 'reference_date': ref.isoformat(), **(data if isinstance(data, dict) else {'items': data})})


def _q(request, name, default=''):
    return request.query_params.get(name, default)


@api_view(['GET'])
@needs('floor.view_overview')
def summary(request, dataset_id, factory_id):
    return _run(request, dataset_id, factory_id, 'summary', queries.summary)


@api_view(['GET'])
@needs('floor.view_orders')
def orders(request, dataset_id, factory_id):
    return _run(request, dataset_id, factory_id, 'orders', lambda c, f, ref: queries.orders(
        c, f, ref, q=_q(request, 'q'), status=_q(request, 'status'), page=_q(request, 'page', 1),
        size=_q(request, 'size', 50), sort=_q(request, 'sort', 'deadline')))


@api_view(['GET'])
@needs('floor.view_orders')
def order_detail(request, dataset_id, factory_id, order_id):
    return _run(request, dataset_id, factory_id, f'order-{order_id}', lambda c, f, ref: queries.order_detail(c, f, order_id))


@api_view(['GET'])
@needs('floor.view_orders')
def styles(request, dataset_id, factory_id):
    return _run(request, dataset_id, factory_id, 'styles', lambda c, f, ref: queries.styles(
        c, f, q=_q(request, 'q'), category=_q(request, 'category'), page=_q(request, 'page', 1), size=_q(request, 'size', 50)))


@api_view(['GET'])
@needs('floor.view_orders')
def style_detail(request, dataset_id, factory_id, product_id):
    return _run(request, dataset_id, factory_id, f'style-{product_id}', lambda c, f, ref: queries.style_detail(c, product_id))


@api_view(['GET'])
@needs('floor.view_overview', 'floor.report_issue')
def materials(request, dataset_id, factory_id):
    return _run(request, dataset_id, factory_id, 'materials', lambda c, f, ref: queries.materials(c, f))


@api_view(['GET'])
@needs('floor.view_workers')
def workforce(request, dataset_id, factory_id):
    return _run(request, dataset_id, factory_id, 'workforce', lambda c, f, ref: queries.workforce(
        c, f, ref, q=_q(request, 'q'), role=_q(request, 'role'), page=_q(request, 'page', 1), size=_q(request, 'size', 50)))


@api_view(['GET'])
@needs('floor.view_machines', 'floor.report_issue')
def machines(request, dataset_id, factory_id):
    return _run(request, dataset_id, factory_id, 'machines', lambda c, f, ref: queries.machines(
        c, f, status=_q(request, 'status'), line=_q(request, 'line'), q=_q(request, 'q'),
        page=_q(request, 'page', 1), size=_q(request, 'size', 50)))


@api_view(['GET'])
@needs('floor.view_overview', 'floor.record_defect')
def quality(request, dataset_id, factory_id):
    return _run(request, dataset_id, factory_id, 'quality', lambda c, f, ref: queries.quality(c, f))


@api_view(['GET'])
@needs('floor.view_orders')
def search(request, dataset_id, factory_id):
    q = _q(request, 'q').strip()
    if len(q) < 2:
        response = Response({'orders': [], 'people': [], 'machines': [], 'styles': []})
    else:
        response = _run(request, dataset_id, factory_id, 'search', lambda c, f, ref: queries.search(c, f, q[:60]))
    if response.status_code == 200 and isinstance(response.data, dict):  # after the cache: filtered per user
        if not request.user.has_perm('floor.view_workers'):
            response.data.pop('people', None)
        if not request.user.has_perm('floor.view_machines'):
            response.data.pop('machines', None)
    return response
