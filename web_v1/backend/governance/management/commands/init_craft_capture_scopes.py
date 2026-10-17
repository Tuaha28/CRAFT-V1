"""Initialize isolated, pinned-source TEST scopes for captured factories 1-6."""
from django.core.management.base import BaseCommand, CommandError

from governance.models import ScheduleScope
from governance.services import fingerprint, request_recipe
from integrations.craft.capture_test_state import build_base, base_for


class Command(BaseCommand):
    help = ('Prepare bounded, explicitly simulated captured-source TEST scopes. '
            'Does not connect to or modify the legacy factory database.')

    def add_arguments(self, parser):
        parser.add_argument('--factory-id', type=int, choices=range(1, 7), action='append')
        parser.add_argument('--domain-limit', type=int, default=200)
        parser.add_argument('--initial-target', type=int, default=25)

    def handle(self, *args, **options):
        domain_limit = options['domain_limit']
        initial_target = options['initial_target']
        if not 1 <= domain_limit <= 200 or not 1 <= initial_target <= 200:
            raise CommandError('Domain and initial target must be between 1 and 200')
        factories = options['factory_id'] or list(range(1, 7))
        for factory_id in factories:
            try:
                base = build_base(factory_id, domain_limit=domain_limit)
                selected = base['selected_keys'][:min(initial_target, len(base['selected_keys']))]
                window_size = min(5, len(selected))
                state = {'classification': 'SIMULATED_TEST', 'production_admission_enabled': False,
                         'capture_state': base, 'selected_keys': selected,
                         'cycle_target': len(selected), 'window_size': window_size,
                         'adaptive_retries': True}
                probe = ScheduleScope(factory_external_id=factory_id, environment='TEST_CAPTURE',
                                      source_hash='pending', resource_state=state)
                expected_hash = fingerprint(request_recipe(probe))
                scope, created = ScheduleScope.objects.get_or_create(
                    factory_external_id=factory_id, environment='TEST_CAPTURE',
                    defaults={'source_hash': expected_hash, 'resource_state': state})
                if created:
                    detail = f'created; domain={len(base["selected_keys"])}; selected={len(selected)}'
                else:
                    current = base_for(scope)
                    if current['fingerprint'] != base['fingerprint']:
                        raise CommandError(f'Existing Factory {factory_id} capture scope differs; preserved')
                    current_recipe = request_recipe(scope)
                    if scope.source_hash != fingerprint(current_recipe):
                        raise CommandError(f'Existing Factory {factory_id} scope is stale; preserved')
                    detail = f'preserved; domain={len(current["selected_keys"])}'
                self.stdout.write(f'Factory {factory_id} TEST_CAPTURE scope {scope.pk}: {detail}')
            except (ValueError, KeyError, OSError) as exc:
                raise CommandError(f'Factory {factory_id} capture preparation failed: {exc}') from exc
