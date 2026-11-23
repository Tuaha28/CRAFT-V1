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
                       ('repair_machine', 'Take and close machine breakdown issues'),
                       ('view_plans', 'See plans and their results'),
                       ('view_orders', 'See orders and styles'),
                       ('view_workers', 'See the worker list (names and skills)'),
                       ('view_machines', 'See the machine list'),
                       ('view_overview', 'See the factory overview, stock and quality summary'),
                       ('view_activity', 'See the activity log (who did what)'),
                       ('complete_step', 'Mark a step complete or closed short'),
                       ('ship_order', 'Pack and ship finished orders'),
                       ('close_order', 'Close orders')]


class Issue(models.Model):
    KINDS = [('MACHINE_DOWN', 'Machine down'), ('WORKER_ABSENT', 'Worker absent'), ('LINE_STOPPED', 'Line stopped'),
             ('MATERIAL_SHORT', 'Material short'), ('QUALITY', 'Quality problem'), ('OTHER', 'Other')]
    STATUSES = [('OPEN', 'Open'), ('IN_PROGRESS', 'In progress'), ('RESOLVED', 'Resolved')]
    dataset = models.ForeignKey('planning.Dataset', on_delete=models.PROTECT)
    factory_id = models.PositiveSmallIntegerField()
    line_id = models.PositiveIntegerField(null=True, blank=True)
    kind = models.CharField(max_length=20, choices=KINDS)
    machine_id = models.PositiveIntegerField(null=True, blank=True)
    worker_id = models.PositiveIntegerField(null=True, blank=True)
    order_id = models.CharField(max_length=20, blank=True)
    # Material short: which raw material (factory database id) and when it is expected to arrive.
    material_id = models.PositiveIntegerField(null=True, blank=True)
    arrival_date = models.DateField(null=True, blank=True)
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
    # Defective pieces count against the step until someone marks them reworked.
    reworked_at = models.DateTimeField(null=True, blank=True)
    reworked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name='+')


class StepStatus(models.Model):
    """A finished step of an order (key "<order>_<operation_id>") in a factory, across plans: complete with all its
    pieces (by itself or marked), or closed short with fewer pieces and a reason. No row = still open."""
    STATES = [('COMPLETE', 'Complete'), ('SHORT', 'Closed short'), ('OPEN', 'Reopened')]  # reopened: no auto-complete
    dataset = models.ForeignKey('planning.Dataset', on_delete=models.PROTECT)
    factory_id = models.PositiveSmallIntegerField()
    operation_key = models.CharField(max_length=40)
    state = models.CharField(max_length=10, choices=STATES)
    pieces = models.PositiveIntegerField()  # the step's final count
    marked = models.BooleanField(default=False)  # someone pressed Mark complete (else it filled up by itself)
    filled = models.PositiveIntegerField(default=0)  # pieces Mark complete recorded for the missing hours
    fill_entry = models.ForeignKey(OutputEntry, null=True, blank=True, on_delete=models.SET_NULL, related_name='+')
    reason = models.TextField(blank=True)
    set_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    finished_at = models.DateTimeField()  # factory time

    class Meta:
        constraints = [models.UniqueConstraint(fields=['dataset', 'factory_id', 'operation_key'], name='one_status_per_step')]


class OrderStatus(models.Model):
    """Where a production order is after sewing. No row = in production (or not started)."""
    STAGES = [('SEWN', 'Sewn'), ('QC_PASSED', 'Final inspection passed'), ('QC_FAILED', 'Final inspection failed'),
              ('PACKED', 'Packed'), ('SHIPPED', 'Shipped'), ('CLOSED', 'Closed')]
    dataset = models.ForeignKey('planning.Dataset', on_delete=models.PROTECT)
    factory_id = models.PositiveSmallIntegerField()
    order_id = models.CharField(max_length=20)
    stage = models.CharField(max_length=10, choices=STAGES)
    sewn_at = models.DateTimeField(null=True, blank=True)  # factory time; empty when closed before sewing finished
    inspection_note = models.TextField(blank=True)
    cartons = models.PositiveIntegerField(null=True, blank=True)
    carrier = models.CharField(max_length=100, blank=True)
    tracking = models.CharField(max_length=100, blank=True)
    shipped_on = models.DateField(null=True, blank=True)
    close_reason = models.TextField(blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['dataset', 'factory_id', 'order_id'], name='one_status_per_order')]
