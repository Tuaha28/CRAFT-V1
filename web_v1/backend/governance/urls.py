from django.urls import path
from . import views

urlpatterns = [
    path('api/v1/governance/session', views.session),
    path('api/v1/governance/login', views.sign_in),
    path('api/v1/governance/scopes', views.Scopes.as_view()),
    path('api/v1/governance/scopes/<int:scope_id>/capture-selection', views.select_capture_work),
    path('api/v1/governance/scopes/<int:scope_id>/priority', views.change_priority),
    path('api/v1/governance/events', views.Events.as_view()),
    path('api/v1/governance/runs', views.Runs.as_view()),
    path('api/v1/governance/runs/<uuid:run_id>/cancel', views.cancel),
    path('api/v1/governance/candidates', views.Candidates.as_view()),
    path('api/v1/governance/candidates/<uuid:candidate_id>/promote', views.promote),
    path('api/v1/governance/candidates/<uuid:candidate_id>/reject', views.reject),
    path('api/v1/governance/candidates/<uuid:candidate_id>/compare', views.compare),
]
