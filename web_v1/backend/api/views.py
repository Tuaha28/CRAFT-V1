from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .serializers import (FactorySerializer, LineSerializer, OrderSerializer, MachineSerializer, WorkerSerializer,
    OperationSerializer, DisruptionSerializer, CraftRunSerializer)

class FilteredList(APIView):
    serializer_class = None
    data_attr = None

    def get_data(self):
        return getattr(services, self.data_attr)

    def get(self, request):
        rows = services.filtered_rows(services.snapshot(self.get_data()), request.query_params)
        serializer = self.serializer_class(rows, many=True)
        page = self.paginate(request, serializer.data)
        return page

    def paginate(self, request, rows):
        from .pagination import StandardPagination
        paginator = StandardPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response(page) if page is not None else Response(rows)

class FactoryList(FilteredList):
    serializer_class, data_attr = FactorySerializer, "FACTORIES"

class LineList(FilteredList):
    serializer_class, data_attr = LineSerializer, "LINES"

class OrderList(FilteredList):
    serializer_class, data_attr = OrderSerializer, "ORDERS"

class OrderDetail(APIView):
    def get(self, request, pk):
        order = next((row for row in services.ORDERS if row["id"] == pk), None)
        if not order:
            return Response({"error": {"code": "not_found", "message": "Order not found.", "details": {"id": pk}}}, status=404)
        result = OrderSerializer(order).data
        result["routing"] = OperationSerializer([op for op in services.OPERATIONS if op["order_id"] == pk], many=True).data
        result["disruptions"] = DisruptionSerializer([event for event in services.DISRUPTIONS if event.get("order_id") == pk], many=True).data
        return Response(result)

class MachineList(FilteredList):
    serializer_class, data_attr = MachineSerializer, "MACHINES"

class WorkerList(FilteredList):
    serializer_class, data_attr = WorkerSerializer, "WORKERS"

class ScheduleList(FilteredList):
    serializer_class, data_attr = OperationSerializer, "OPERATIONS"

    def get_data(self):
        rows = services.snapshot(services.OPERATIONS)
        query = self.request.query_params
        if query.get("start"):
            point = parse_datetime(query["start"])
            if point:
                rows = [row for row in rows if row["end"] >= point]
        if query.get("end"):
            point = parse_datetime(query["end"])
            if point:
                rows = [row for row in rows if row["start"] <= point]
        return rows

class DisruptionList(FilteredList):
    serializer_class, data_attr = DisruptionSerializer, "DISRUPTIONS"

class CraftRunList(FilteredList):
    serializer_class, data_attr = CraftRunSerializer, "CRAFT_RUNS"

class CraftRunDetail(APIView):
    def get(self, request, pk):
        run = next((row for row in services.CRAFT_RUNS if row["id"] == pk), None)
        if not run:
            return Response({"error": {"code": "not_found", "message": "CRAFT run not found.", "details": {"id": pk}}}, status=404)
        return Response(CraftRunSerializer(run).data)

@api_view(["GET"])
def health(request):
    return Response({"status": "ok", "service": "CRAFT Demo API", "mode": "DEMO / MOCK DATA", "database": "not connected", "scheduler": "not connected"})

@api_view(["GET"])
def dashboard(request):
    return Response(services.dashboard_summary(request.query_params.get("factory_id")))

@api_view(["GET"])
def analytics(request):
    factory = request.query_params.get("factory_id")
    return Response({"factory_id": int(factory) if factory and factory.isdigit() else None, "period": request.query_params.get("period", "7d"), "delivery": [{"period": "W-5", "on_time": 84, "tardiness": 410}, {"period": "W-4", "on_time": 87, "tardiness": 365}, {"period": "W-3", "on_time": 82, "tardiness": 452}, {"period": "W-2", "on_time": 89, "tardiness": 311}, {"period": "W-1", "on_time": 91, "tardiness": 264}, {"period": "Current", "on_time": 88, "tardiness": 298}], "utilization": [{"name": "Machine", "value": 76}, {"name": "Workforce", "value": 83}], "stability": [{"week": "W-5", "accepted": 92, "escalated": 8}, {"week": "W-4", "accepted": 94, "escalated": 6}, {"week": "W-3", "accepted": 90, "escalated": 10}, {"week": "W-2", "accepted": 96, "escalated": 4}, {"week": "W-1", "accepted": 93, "escalated": 7}, {"week": "Current", "accepted": 91, "escalated": 9}], "summary": {"on_time_delivery": 88.4, "mean_tardiness_hours": 6.3, "schedule_stability": 93.1, "repairs_accepted": 27, "repairs_escalated": 3}})

@api_view(["GET"])
def audit(request):
    rows = services.filtered_rows(services.snapshot(services.AUDIT), request.query_params, keys=("factory_id", "accepted"))
    from .pagination import StandardPagination
    pager = StandardPagination(); page = pager.paginate_queryset(rows, request)
    return pager.get_paginated_response(page) if page is not None else Response(rows)

class DemoDisruption(APIView):
    def post(self, request):
        from rest_framework import serializers
        class Input(serializers.Serializer):
            type = serializers.ChoiceField(choices=["Machine breakdown", "Worker absence", "Rush order", "Priority override", "Line disruption"])
            severity = serializers.ChoiceField(choices=["Info", "Warning", "Critical"])
            entity = serializers.CharField(max_length=120)
            factory_id = serializers.IntegerField(min_value=1, max_value=6)
            line_id = serializers.CharField(required=False, allow_blank=True)
            order_id = serializers.CharField(required=False, allow_blank=True)
            description = serializers.CharField(max_length=300)
        form = Input(data=request.data); form.is_valid(raise_exception=True)
        return Response({"demo": True, "notice": "Simulation only. No live factory state or schedule changed.", "event": DisruptionSerializer(services.add_demo_disruption(form.validated_data)).data}, status=status.HTTP_201_CREATED)

class DemoPriority(APIView):
    def post(self, request, pk):
        from rest_framework import serializers
        class Input(serializers.Serializer):
            priority = serializers.ChoiceField(choices=["Normal", "High", "Urgent"])
        form = Input(data=request.data); form.is_valid(raise_exception=True)
        order = services.set_demo_priority(pk, form.validated_data["priority"])
        if order is None:
            return Response({"error": {"code": "not_found", "message": "Order not found.", "details": {"id": pk}}}, status=404)
        return Response({"demo": True, "notice": "Priority changed in in-memory demo fixtures only.", "order": OrderSerializer(order).data})

class DemoCraftRun(APIView):
    def post(self, request):
        from rest_framework import serializers
        class Input(serializers.Serializer):
            factory_id = serializers.IntegerField(min_value=1, max_value=6)
            trigger = serializers.CharField(max_length=160)
        form = Input(data=request.data); form.is_valid(raise_exception=True)
        return Response({"demo": True, "notice": "Simulation only. No CRAFT scheduler was invoked and no schedule changed.", "run": CraftRunSerializer(services.add_demo_run(**form.validated_data)).data}, status=201)
