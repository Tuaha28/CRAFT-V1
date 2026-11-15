import uuid
from django.conf import settings
from django.db import models


class Dataset(models.Model):
    """A pinned, read-only factory capture (snapshot files plus SHA-256 manifest)."""
    slug = models.CharField(max_length=80, unique=True)
    label = models.CharField(max_length=120)
    origin = models.CharField(max_length=16, choices=[('SNAPSHOT', 'SNAPSHOT'), ('LIVE_CAPTURE', 'LIVE_CAPTURE')])
    path = models.CharField(max_length=500)
    database = models.CharField(max_length=200, blank=True)
    load_pct = models.PositiveSmallIntegerField(null=True)
    reference_time = models.CharField(max_length=32, blank=True)
    factories = models.JSONField(default=list)
    captured_at = models.CharField(max_length=40, blank=True)
    registered_at = models.DateTimeField(auto_now_add=True)


class PlanRun(models.Model):
    """One CRAFT V3 run (or one live read-only capture) executed by the worker."""
    KINDS = [('SCHEDULE', 'SCHEDULE'), ('CAPTURE', 'CAPTURE')]
    MODES = [('FULL', 'FULL'), ('QUICK', 'QUICK')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=16, choices=KINDS, default='SCHEDULE')
    dataset = models.ForeignKey(Dataset, null=True, blank=True, on_delete=models.PROTECT)
    factory_id = models.PositiveSmallIntegerField(null=True)
    orders = models.PositiveSmallIntegerField(null=True)
    order_offset = models.PositiveIntegerField(default=0)
    mode = models.CharField(max_length=8, choices=MODES, default='QUICK')
    status = models.CharField(max_length=16, default='QUEUED')
    stage = models.CharField(max_length=64, blank=True)
    progress = models.FloatField(default=0.0)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True)
    heartbeat_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    pid = models.PositiveIntegerField(null=True)
    out_dir = models.CharField(max_length=500, blank=True)
    log_tail = models.TextField(blank=True)
    error = models.TextField(blank=True)
    summary = models.JSONField(default=dict)
    decision = models.CharField(max_length=16, default='PENDING')
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name='+')
    decided_at = models.DateTimeField(null=True)
    decision_reason = models.TextField(blank=True)
    # What-if runs: re-plan the same orders after a disruption, or with one new
    # order added. They run on a scenario copy of the capture (scenario_dir).
    purpose = models.CharField(max_length=16, default='PLAN',
                               choices=[('PLAN', 'PLAN'), ('DISRUPTION', 'DISRUPTION'), ('PROMISE', 'PROMISE')])
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT, related_name='what_ifs')
    scenario = models.JSONField(default=dict, blank=True)
    order_ids = models.JSONField(default=list, blank=True)
    scenario_dir = models.CharField(max_length=500, blank=True)

    @property
    def capture_path(self):
        return self.scenario_dir or (self.dataset.path if self.dataset else '')

    class Meta:
        permissions = [('request_plan', 'Request a CRAFT V3 plan or a read-only capture'),
                       ('approve_plan', 'Approve or reject a validated CRAFT V3 plan')]


class PlanEvent(models.Model):
    run = models.ForeignKey(PlanRun, null=True, blank=True, on_delete=models.CASCADE, related_name='events')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=40)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
