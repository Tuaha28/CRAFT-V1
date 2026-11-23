from django.urls import path
from . import views

P = 'api/v1/floor/<int:dataset_id>/<int:factory_id>/'
urlpatterns = [
    path(P + 'lines', views.lines),
    path(P + 'lines/<int:line_id>', views.line_day),
    path(P + 'board', views.board),
    path(P + 'output', views.output),
    path(P + 'checks', views.checks),
    path(P + 'issues', views.issues),
    path(P + 'replan', views.replan),
    path(P + 'steps/<str:key>/complete', views.complete_step),
    path(P + 'steps/<str:key>/reopen', views.reopen_step),
    path(P + 'orders/statuses', views.order_statuses),
    path(P + 'orders/<str:order_id>', views.order_status),
    path(P + 'orders/<str:order_id>/<str:action>', views.order_action),
    path('api/v1/floor/checks/<int:check_id>/rework', views.rework),
    path('api/v1/floor/issues/<int:issue_id>/status', views.move),
    path('api/v1/floor/users', views.users),
    path('api/v1/floor/users/<int:user_id>', views.change_user),
    path('api/v1/floor/users/<int:user_id>/password', views.new_password),
]
