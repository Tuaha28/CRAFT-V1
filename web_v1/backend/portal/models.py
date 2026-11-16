"""Customer portal records owned by the web app: who a customer is and what they asked for."""
from django.conf import settings
from django.db import models


class CustomerAccount(models.Model):
    """A sign-in for one customer company: they see only orders whose purchase order names this client."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='customer_account')
    client_name = models.CharField(max_length=150)
    dataset = models.ForeignKey('planning.Dataset', on_delete=models.PROTECT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def customer_id(self):
        return f'CUST-{self.pk}'

    class Meta:
        permissions = [('handle_request', 'Review and answer customer requests')]


class CustomerRequest(models.Model):
    """A request a customer confirmed (quote, sample, order change, support). Staff accept or decline it;
    nothing here changes a plan or the factory data."""
    KINDS = [('quote', 'Quotation'), ('sample', 'Sample'), ('order_change', 'Order change'), ('support', 'Support')]
    STATUSES = [('PENDING', 'Waiting for review'), ('ACCEPTED', 'Accepted'), ('DECLINED', 'Declined')]
    account = models.ForeignKey(CustomerAccount, on_delete=models.CASCADE, related_name='requests')
    kind = models.CharField(max_length=20, choices=KINDS)
    description = models.TextField()
    order_id = models.CharField(max_length=20, blank=True)
    style_code = models.CharField(max_length=40, blank=True)
    quantity = models.PositiveIntegerField(null=True, blank=True)
    colour = models.CharField(max_length=100, blank=True)
    destination = models.CharField(max_length=200, blank=True)
    idempotency_key = models.CharField(max_length=100)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=STATUSES, default='PENDING')
    staff_note = models.TextField(blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def request_id(self):
        return f'REQ-{self.pk}'

    class Meta:
        constraints = [models.UniqueConstraint(fields=['account', 'idempotency_key'], name='one_request_per_key')]
