"""Register an isolated, synthetic two-order manager-priority TEST scope."""
from django.core.management.base import BaseCommand, CommandError
from governance.models import ScheduleScope
from governance.services import fingerprint, request_recipe


class Command(BaseCommand):
    help = 'Create application-only two-order priority TEST metadata.'

    def handle(self, **options):
        probe = ScheduleScope(factory_external_id=1, environment='TEST_PRIORITY', priority_state={})
        initial_hash = fingerprint(request_recipe(probe))
        scope, created = ScheduleScope.objects.get_or_create(
            factory_external_id=1, environment='TEST_PRIORITY',
            defaults={'source_hash': initial_hash, 'priority_state': {}})
        if scope.source_hash != fingerprint(request_recipe(scope)):
            raise CommandError('Existing priority scope source state differs; refusing to overwrite')
        self.stdout.write(f'TEST_PRIORITY scope {scope.pk}: {"created" if created else "already exists"}. No factory data changed.')
