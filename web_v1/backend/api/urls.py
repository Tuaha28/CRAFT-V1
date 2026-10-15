from django.urls import path
from .views import (health, dashboard, analytics, audit, FactoryList, LineList, OrderList, OrderDetail,
    MachineList, WorkerList, ScheduleList, DisruptionList, CraftRunList, CraftRunDetail,
    DemoDisruption, DemoPriority, DemoCraftRun)

urlpatterns = [
    path("health", health),
    path("dashboard/summary", dashboard),
    path("factories", FactoryList.as_view()),
    path("lines", LineList.as_view()),
    path("orders", OrderList.as_view()),
    path("orders/<str:pk>", OrderDetail.as_view()),
    path("resources/machines", MachineList.as_view()),
    path("resources/workers", WorkerList.as_view()),
    path("schedule", ScheduleList.as_view()),
    path("disruptions", DisruptionList.as_view()),
    path("craft/runs", CraftRunList.as_view()),
    path("craft/runs/<str:pk>", CraftRunDetail.as_view()),
    path("analytics", analytics),
    path("audit", audit),
    path("demo/disruptions", DemoDisruption.as_view()),
    path("demo/orders/<str:pk>/priority", DemoPriority.as_view()),
    path("demo/craft/run", DemoCraftRun.as_view()),
]
