import uuid
from django.conf import settings
from django.db import models


class ScheduleScope(models.Model):
    """Whole-factory serialization point; TEST cannot masquerade as real factory state."""
    factory_external_id = models.PositiveIntegerField()
    environment = models.CharField(max_length=16, default='TEST', choices=[('TEST', 'TEST'), ('TEST_SIM', 'TEST_SIM'),
                                                                           ('TEST_PRIORITY', 'TEST_PRIORITY'),
                                                                           ('TEST_RESOURCE', 'TEST_RESOURCE'),
                                                                           ('TEST_FACTORY1', 'TEST_FACTORY1'),
                                                                           ('TEST_CAPTURE', 'TEST_CAPTURE')])
    source_hash = models.CharField(max_length=64)
    priority_state = models.JSONField(default=dict)
    resource_state = models.JSONField(default=dict)
    resource_revision = models.PositiveIntegerField(default=0)
    active_version = models.ForeignKey('ScheduleVersion', null=True, blank=True,
                                      on_delete=models.PROTECT, related_name='+')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['factory_external_id', 'environment'], name='unique_factory_scope')]
        permissions = [('request_run', 'Request scheduling run'), ('promote_schedule', 'Promote validated schedule'),
                       ('change_priority', 'Change controlled order priority')]


class FactoryAccess(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    scope = models.ForeignKey(ScheduleScope, on_delete=models.CASCADE)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'scope'], name='unique_factory_access')]


class CraftRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scope = models.ForeignKey(ScheduleScope, on_delete=models.PROTECT)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=128)
    snapshot = models.JSONField()
    input_hash = models.CharField(max_length=64)
    base_version = models.ForeignKey('ScheduleVersion', null=True, on_delete=models.PROTECT, related_name='+')
    source_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=32, default='QUEUED')
    cp_status = models.CharField(max_length=32, default='NOT_REACHED')
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['scope', 'idempotency_key'], name='unique_run_request')]


class ScheduleVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scope = models.ForeignKey(ScheduleScope, on_delete=models.PROTECT)
    run = models.OneToOneField(CraftRun, on_delete=models.PROTECT)
    status = models.CharField(max_length=32, default='VALIDATED_CANDIDATE')
    operations = models.JSONField()
    evidence = models.JSONField()
    payload_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['scope'], condition=models.Q(status='ACTIVE'),
                                                name='one_active_schedule_per_scope')]


class AuditEvent(models.Model):
    scope = models.ForeignKey(ScheduleScope, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=64)
    entity_id = models.CharField(max_length=64)
    reason = models.TextField()
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
