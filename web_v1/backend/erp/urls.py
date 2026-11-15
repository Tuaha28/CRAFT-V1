from django.urls import path
from . import views

P = 'api/v1/erp/<int:dataset_id>/<int:factory_id>/'
urlpatterns = [
    path(P + 'summary', views.summary),
    path(P + 'orders', views.orders),
    path(P + 'orders/<int:order_id>', views.order_detail),
    path(P + 'styles', views.styles),
    path(P + 'styles/<int:product_id>', views.style_detail),
    path(P + 'materials', views.materials),
    path(P + 'workforce', views.workforce),
    path(P + 'machines', views.machines),
    path(P + 'quality', views.quality),
    path(P + 'search', views.search),
]
