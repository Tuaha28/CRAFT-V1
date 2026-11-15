"""Shop-floor records owned by the web app (never written to the factory database)."""
from django.conf import settings
from django.db import models


class Profile(models.Model):
    """A user's role and the factory/line they work for (null = all)."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='floor_profile')
    role = models.CharField(max_length=20)
    factory_id = models.PositiveSmallIntegerField(null=True, blank=True)
    line_id = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        permissions = [('manage_users', 'Create users and assign roles'),
                       ('view_floor', 'See the approved plan for the floor, line boards and issues'),
                       ('report_issue', 'Report a machine down, an absent worker or a material shortage'),
                       ('handle_issue', 'Acknowledge and close issues, and re-plan after them'),
                       ('enter_output', 'Enter hourly output for a line'),
                       ('record_defect', 'Record quality checks and defects'),
                       ('repair_machine', 'Take and close machine breakdown issues')]


class Issue(models.Model):
    KINDS = [('MACHINE_DOWN', 'Machine down'), ('WORKER_ABSENT', 'Worker absent'),
             ('MATERIAL_SHORT', 'Material short'), ('QUALITY', 'Quality problem'), ('OTHER', 'Other')]
    STATUSES = [('OPEN', 'Open'), ('IN_PROGRESS', 'In progress'), ('RESOLVED', 'Resolved')]
    dataset = models.ForeignKey('planning.Dataset', on_delete=models.PROTECT)
    factory_id = models.PositiveSmallIntegerField()
    line_id = models.PositiveIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=20, choices=KINDS)
    machine_id = models.PositiveIntegerField(null=True, blank=True)
    worker_id = models.PositiveIntegerField(null=True, blank=True)
    order_id = models.CharField(max_length=20, blank=True)
    note = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=STATUSES, default='OPEN')
    reported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    handled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name='+')
    resolution = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    replan = models.ForeignKey('planning.PlanRun', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')


class OutputEntry(models.Model):
    """Pieces finished for one operation of an approved plan in one hour (slot = hours since the plan's start)."""
    run = models.ForeignKey('planning.PlanRun', on_delete=models.PROTECT, related_name='+')
    line_id = models.PositiveIntegerField()
    operation_key = models.CharField(max_length=40)
    slot = models.PositiveIntegerField()
    pieces = models.PositiveIntegerField()
    entered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['run', 'operation_key', 'slot'], name='one_output_per_hour')]


class DefectCheck(models.Model):
    TYPES = ['Broken stitch', 'Skipped stitch', 'Open seam', 'Puckering', 'Uneven hem', 'Wrong measurement',
             'Stain or mark', 'Other']
    run = models.ForeignKey('planning.PlanRun', on_delete=models.PROTECT, related_name='+')
    line_id = models.PositiveIntegerField()
    day = models.PositiveIntegerField()
    operation_key = models.CharField(max_length=40)
    checked = models.PositiveIntegerField()
    defects = models.PositiveIntegerField()
    defect_type = models.CharField(max_length=40, blank=True)
    note = models.TextField(blank=True)
    entered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
