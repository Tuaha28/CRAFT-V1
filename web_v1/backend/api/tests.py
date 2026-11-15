from django.test import override_settings
from rest_framework.test import APITestCase


# The mock demo API is only mounted by the base URL config (config.urls), not by the
# integration URL config the app runs on, so pin it for these tests.
@override_settings(ROOT_URLCONF='config.urls')
class MockApiTests(APITestCase):
    def test_health_discloses_mock_boundary(self):
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["mode"], "DEMO / MOCK DATA")
        self.assertEqual(response.data["database"], "not connected")

    def test_factories_and_paginated_orders(self):
        self.assertEqual(self.client.get("/api/v1/factories").status_code, 200)
        response = self.client.get("/api/v1/orders?page_size=5")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 5)
        self.assertEqual(response.data["count"], 36)

    def test_order_detail_includes_routing(self):
        response = self.client.get("/api/v1/orders/PO-1001")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["routing"]), 3)

    def test_operational_read_routes(self):
        for path in [
            "/api/v1/lines", "/api/v1/resources/machines", "/api/v1/resources/workers",
            "/api/v1/schedule", "/api/v1/disruptions", "/api/v1/craft/runs",
            "/api/v1/craft/runs/CR-20261007-0041", "/api/v1/analytics", "/api/v1/audit",
            "/api/v1/dashboard/summary",
        ]:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, response.data)

    def test_demo_disruption_is_explicitly_in_memory(self):
        response = self.client.post("/api/v1/demo/disruptions", {"type": "Machine breakdown", "severity": "Warning", "entity": "M-1", "factory_id": 1, "description": "Demo test"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["demo"])
        self.assertIn("Simulation only", response.data["notice"])

    def test_demo_priority_is_in_memory(self):
        response = self.client.post("/api/v1/demo/orders/PO-1001/priority", {"priority": "Urgent"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["demo"])
        self.assertEqual(response.data["order"]["priority"], "Urgent")

    def test_demo_craft_run_does_not_invoke_scheduler(self):
        response = self.client.post("/api/v1/demo/craft/run", {"factory_id": 2, "trigger": "Demo test"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["run"]["controller_status"], "COMPUTATIONAL_LIMIT")
