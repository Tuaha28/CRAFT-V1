from django.core.management.base import BaseCommand, CommandError
from governance.models import ScheduleScope
from governance.services import fingerprint


class Command(BaseCommand):
    help = 'Create metadata for the explicitly synthetic Factory-1 proof scope only.'
    def handle(self, **options):
        recipe = {'contract': 'RESIDUAL_VERTICAL_SLICE_V1', 'source': 'SYNTHETIC_FIXTURE'}
        scope, created = ScheduleScope.objects.get_or_create(factory_external_id=1, environment='TEST',
            defaults={'source_hash': fingerprint(recipe)})
        if scope.source_hash != fingerprint(recipe):
            raise CommandError('Existing scope differs; refusing to overwrite source state')
        self.stdout.write(f'TEST scope {scope.pk}: {"created" if created else "already exists"}. No factory data changed.')
