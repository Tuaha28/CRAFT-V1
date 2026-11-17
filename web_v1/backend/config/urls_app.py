"""Integration server URLs: governance (login, legacy TEST scopes), CRAFT V3 planning, the read-only ERP and the shop floor."""
from governance.urls import urlpatterns as governance
from planning.urls import urlpatterns as planning
from erp.urls import urlpatterns as erp
from floor.urls import urlpatterns as floor
from assistant.urls import urlpatterns as assistant
from portal.urls import urlpatterns as portal
from sim.urls import urlpatterns as sim

urlpatterns = [*governance, *planning, *erp, *floor, *assistant, *portal, *sim]
