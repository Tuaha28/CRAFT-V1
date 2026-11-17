from django.urls import path
from . import views

urlpatterns = [
    path('api/v1/planning/session', views.session),
    path('api/v1/planning/logout', views.sign_out),
    path('api/v1/planning/datasets', views.datasets),
    path('api/v1/planning/datasets/<int:dataset_id>/factories/<int:factory_id>/orders', views.orderbook),
    path('api/v1/planning/runs', views.runs),
    path('api/v1/planning/captures', views.captures),
    path('api/v1/planning/runs/<uuid:run_id>', views.run_detail),
    path('api/v1/planning/runs/<uuid:run_id>/cancel', views.cancel),
    path('api/v1/planning/runs/<uuid:run_id>/decision', views.decide),
    path('api/v1/planning/runs/<uuid:run_id>/workbook', views.workbook),
    path('api/v1/planning/runs/<uuid:run_id>/disruption', views.disruption),
    path('api/v1/planning/runs/<uuid:run_id>/promise', views.promise),
    path('api/v1/planning/current/<int:dataset_id>/<int:factory_id>', views.current_plan),
    path('api/v1/planning/experiments', views.experiments),
    path('api/v1/planning/events', views.events),
    path('api/v1/planning/priority', views.priority),
    path('api/v1/planning/autoplan', views.autoplan_setting),
    path('api/v1/planning/autoplan/all', views.autoplan_all),
]
