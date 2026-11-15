from django.urls import path
from . import views

urlpatterns = [
    path('api/v1/assistant/status', views.status),
    path('api/v1/assistant/route', views.route),
]
