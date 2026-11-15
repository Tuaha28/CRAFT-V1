"""Create the factory roles and, optionally, users for the web app.

  manage.py floor_setup                                     # roles only
  manage.py floor_setup --user NAME --role supervisor --factory 4 --line 44 --password-file PATH
  manage.py floor_setup --demo 4:44 --password-file PATH    # one user per role (supervisor on line 44)

New passwords are generated and written to --password-file (keep it out of
git; the file must not exist yet); they are never printed. An existing user
keeps their password and only gets the role.
"""
from pathlib import Path
import secrets
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from floor.models import Profile
from floor.roles import LINE_REQUIRED, ROLES, ensure_groups
from floor.services import _assign

LINE_SCOPED = {'supervisor', 'qc'}


class Command(BaseCommand):
    help = 'Create factory roles and optionally users.'

    def add_arguments(self, parser):
        parser.add_argument('--user')
        parser.add_argument('--role', choices=sorted(ROLES))
        parser.add_argument('--factory', type=int)
        parser.add_argument('--line', type=int)
        parser.add_argument('--demo', help='FACTORY:LINE - create demo_<role> users')
        parser.add_argument('--password-file')

    def handle(self, user=None, role=None, factory=None, line=None, demo=None, password_file=None, **options):
        groups = ensure_groups()
        self.stdout.write('Roles ready: ' + ', '.join(name for name, _ in ROLES.values()))
        wanted = []
        if user:
            if not role:
                raise CommandError('--role is required with --user')
            if role in LINE_REQUIRED and not line:
                raise CommandError(f'--line is required for {role}')
            wanted.append((user, role, factory, line))
        if demo:
            try:
                f, ln = (int(x) for x in demo.split(':'))
            except ValueError as exc:
                raise CommandError('--demo takes FACTORY:LINE, e.g. 4:44') from exc
            wanted += [(f'demo_{key}', key, f if key != 'admin' else None, ln if key in LINE_SCOPED else None)
                       for key in ROLES if key != 'manager']
        if not wanted:
            return
        new = [w for w in wanted if not User.objects.filter(username=w[0]).exists()]
        if new:
            if not password_file:
                raise CommandError('--password-file is required to create users')
            path = Path(password_file)
            if path.exists():
                raise CommandError(f'{path} already exists; choose a new file')
            lines = []
            for name, *_ in new:
                password = secrets.token_urlsafe(12)
                User.objects.create_user(name, password=password)
                lines.append(f'{name}={password}')
            path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            self.stdout.write(f'{len(new)} user(s) created; passwords written to {path}')
        for name, key, f, ln in wanted:
            account = User.objects.get(username=name)
            _assign(account, key, f, ln, groups)
            self.stdout.write(f'{name}: {ROLES[key][0]}{f" (factory {f}" if f else ""}'
                              f'{f", line {ln})" if ln else (")" if f else "")}')
        self.stdout.write(f'Profiles: {Profile.objects.count()}')
