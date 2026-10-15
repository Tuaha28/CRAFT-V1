"""Register a separate, explicitly simulated Factory-1 TEST scope."""
from django.core.management.base import BaseCommand, CommandError
from governance.models import ScheduleScope
from governance.services import fingerprint, request_recipe


class Command(BaseCommand):
    help = 'Create application-only Factory-1 simulated continuation TEST metadata.'

    def handle(self, **options):
        probe = ScheduleScope(factory_external_id=1, environment='TEST_SIM')
        recipe = request_recipe(probe)
        scope, created = ScheduleScope.objects.get_or_create(
            factory_external_id=1, environment='TEST_SIM',
            defaults={'source_hash': fingerprint(recipe)})
        if scope.source_hash != fingerprint(recipe):
            raise CommandError('Existing simulation scope differs; refusing to overwrite source state')
        self.stdout.write(f'TEST_SIM scope {scope.pk}: {"created" if created else "already exists"}. No factory data changed.')
