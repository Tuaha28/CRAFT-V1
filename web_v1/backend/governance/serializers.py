from rest_framework import serializers
from .models import AuditEvent, CraftRun, ScheduleScope, ScheduleVersion


class ScopeSerializer(serializers.ModelSerializer):
    request_snapshot = serializers.SerializerMethodField()
    capture = serializers.SerializerMethodField()

    def get_request_snapshot(self, obj):
        from .services import request_recipe
        return request_recipe(obj)

    def get_capture(self, obj):
        """Read-only selection context for a captured TEST scope; selection rules stay server-side."""
        if obj.environment != 'TEST_CAPTURE':
            return None
        state = obj.resource_state or {}
        base = state.get('capture_state') or {}
        approved = [row['operation_key'] for row in obj.active_version.operations] if obj.active_version else []
        return {'prepared_keys': list(base.get('selected_keys', ())),
                'selected_keys': list(state.get('selected_keys', ())),
                'approved_keys': approved,
                'window_size': state.get('window_size'),
                'ledger_operations': len(base.get('operation_ledger', {})),
                'source_hash': base.get('source_hash')}

    class Meta:
        model = ScheduleScope
        fields = ['id', 'factory_external_id', 'environment', 'source_hash', 'active_version',
                  'priority_state', 'request_snapshot', 'capture']


class RunSerializer(serializers.ModelSerializer):
    class Meta:
        model = CraftRun
        fields = ['id', 'scope', 'status', 'cp_status', 'input_hash', 'source_hash', 'base_version',
                  'created_at', 'started_at', 'finished_at', 'result']


class CandidateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScheduleVersion
        fields = ['id', 'scope', 'run', 'status', 'operations', 'evidence', 'payload_hash', 'created_at']


class RunRequestSerializer(serializers.Serializer):
    scope = serializers.IntegerField(min_value=1)
    idempotency_key = serializers.CharField(max_length=128)
    snapshot = serializers.JSONField()


class DecisionSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=2000, allow_blank=False)


class PriorityRequestSerializer(serializers.Serializer):
    order_id = serializers.ChoiceField(choices=['A', 'B'])
    tier = serializers.ChoiceField(choices=['NORMAL', 'HIGH', 'URGENT'])
    target_due_seconds = serializers.IntegerField(min_value=1, max_value=3600)
    reason = serializers.CharField(max_length=2000, allow_blank=False)
    expected_source_hash = serializers.RegexField(regex=r'^[0-9a-f]{64}$')


class CaptureSelectionSerializer(serializers.Serializer):
    operation_keys = serializers.ListField(child=serializers.CharField(max_length=80),
                                           min_length=1, max_length=200)
    window_size = serializers.IntegerField(min_value=1, max_value=25, required=False)


class AuditEventSerializer(serializers.ModelSerializer):
    actor = serializers.SerializerMethodField()

    def get_actor(self, obj):
        return obj.actor.get_username() if obj.actor_id else None

    class Meta:
        model = AuditEvent
        fields = ['id', 'scope', 'actor', 'action', 'entity_id', 'reason', 'details', 'created_at']
