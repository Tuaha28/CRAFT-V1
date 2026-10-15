"""Deterministic, non-production evidence packets used only by intake tests."""

AS_OF = "2026-09-10T00:00:00"


def _provenance(valid_until="2026-09-11T00:00:00"):
    return {"evidence_version": "fixture-1", "source": "manager_confirmed_execution",
            "authorized_by": "manager-1", "authorization_id": "AUTH-1",
            "captured_at": AS_OF, "valid_until": valid_until}


def _work(source="AUTHORIZED_ESTIMATE"):
    return {"productive_seconds": 28800, "source": source,
            "model_version": "smv-calendar-v1", "assumptions": ["SMV=480 seconds/unit", "crew efficiency=100%"]}


def committed_packet():
    segments = [{"start": "2026-09-10T05:00:00", "end": "2026-09-10T06:00:00"},
                {"start": "2026-09-10T14:00:00", "end": "2026-09-10T16:00:00"}]
    availability = {
        "machine:M1": [{"start": "2026-09-10T05:00:00", "end": "2026-09-10T16:00:00"}],
        "line:L1": [{"start": "2026-09-10T05:00:00", "end": "2026-09-10T16:00:00"}],
        "worker:W1": [{"start": "2026-09-10T05:00:00", "end": "2026-09-10T06:00:00"},
                       {"start": "2026-09-10T14:00:00", "end": "2026-09-10T16:00:00"}],
    }
    return {"contract": "RESIDUAL_EVIDENCE_INTAKE_V1", "database": "garment_fyp_db",
            "factory_id": 1, "as_of": AS_OF, "source_snapshot_hash": "fixture-source",
            "operations": [{"operation_key": "1_1", "order_id": "1", "mode": "COMMITTED_CONTINUATION",
                             "as_of": AS_OF, "completed_lot_ids": ["lot-1"], "residual_lot_ids": ["lot-2", "lot-3"],
                             "quantity_ledger": {"required": 100, "completed": 40, "in_flight": 20, "unstarted": 40, "remaining": 60},
                             "continuation_lots": [{"lot_id": "lot-2", "state": "IN_FLIGHT", "quantity": 20},
                                                    {"lot_id": "lot-3", "state": "NOT_STARTED", "quantity": 40}],
                             "resource_commitments": [{"resource_type": kind, "resource_id": ident, "segments": segments}
                                                       for kind, ident in (("machine", "M1"), ("worker", "W1"), ("line", "L1"))],
                             "resource_availability": availability, "remaining_work": _work(),
                             "precedence": {"predecessor_completion": "2026-09-09T22:00:00",
                                            "successor_earliest_ready": "2026-09-10T00:00:00"},
                             "provenance": _provenance()}]}


def splittable_packet():
    packet = committed_packet()
    op = packet["operations"][0]
    op.update({"mode": "SPLITTABLE_RESIDUAL", "completed_lot_ids": ["lot-1"],
               "residual_lot_ids": ["lot-3"],
               "quantity_ledger": {"required": 100, "completed": 40, "in_flight": 0, "unstarted": 60, "remaining": 60},
               "continuation_lots": [{"lot_id": "lot-3", "state": "NOT_STARTED", "quantity": 60}],
               "resource_commitments": [], "resource_availability": {},
               "split_proof": {"operation_level_residual_supported": True, "no_duplicate_completed_work": True,
                               "predecessor_boundary_preserved": True, "resource_reallocation_authorized": True}})
    return packet
