"""The factory clock: where a simulated factory is in time, how fast it runs, and the engine's state."""
from django.conf import settings
from django.db import models


class FactoryClock(models.Model):
    SPEEDS = (0, 1, 10, 60)  # paused; 1x = one factory hour per real minute; 60x = one factory hour per real second
    dataset = models.ForeignKey('planning.Dataset', on_delete=models.CASCADE)
    factory_id = models.PositiveSmallIntegerField()
    now = models.DateTimeField()
    speed = models.PositiveSmallIntegerField(default=0)
    seed = models.IntegerField(default=1)
    state = models.JSONField(default=dict, blank=True)
    last_tick = models.DateTimeField(null=True, blank=True)
    set_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['dataset', 'factory_id'], name='one_clock_per_factory')]
