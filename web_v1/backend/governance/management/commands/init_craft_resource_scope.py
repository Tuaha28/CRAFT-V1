"""Create application-owned TEST-only resource-state proof scope."""
from django.core.management.base import BaseCommand, CommandError
from governance.models import ScheduleScope
from governance.resource_fixture import initial_state
from governance.services import fingerprint, request_recipe


class Command(BaseCommand):
    help = 'Initialize synthetic TEST_RESOURCE state in craft_web_db only.'

    def handle(self, **options):
        state = initial_state()
        probe = ScheduleScope(factory_external_id=1, environment='TEST_RESOURCE',
                              resource_state=state)
        expected = fingerprint(request_recipe(probe))
        scope, created = ScheduleScope.objects.get_or_create(
            factory_external_id=1, environment='TEST_RESOURCE',
            defaults={'source_hash': expected, 'resource_state': state})
        if scope.source_hash != fingerprint(request_recipe(scope)):
            raise CommandError('Existing resource scope differs; refusing overwrite')
        self.stdout.write(f'TEST_RESOURCE scope {scope.pk}: {"created" if created else "preserved"}')
