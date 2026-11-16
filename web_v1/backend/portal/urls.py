from django.urls import path
from . import agent_api, views

urlpatterns = [
    # the customer assistant service calls these with its service key
    path('customer/products', agent_api.products),
    path('customer/orders', agent_api.order_list),
    path('customer/orders/<str:ref>', agent_api.order_detail),
    path('customer/requests', agent_api.create_request),
    # the portal pages
    path('api/v1/portal/me', views.me),
    path('api/v1/portal/orders', views.order_list),
    path('api/v1/portal/orders/<str:ref>', views.order_detail),
    path('api/v1/portal/products', views.products),
    path('api/v1/portal/requests', views.my_requests),
    path('api/v1/portal/chat', views.chat),
    path('api/v1/portal/actions/<uuid:action_id>/confirm', views.confirm_action),
    path('api/v1/portal/staff/requests', views.staff_requests),
    path('api/v1/portal/staff/requests/<int:pk>', views.staff_decide),
    path('api/v1/portal/staff/accounts', views.staff_accounts),
    path('api/v1/portal/staff/clients', views.staff_clients),
]
