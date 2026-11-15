"""Create the planner roles and, optionally, a local user for the web app.

  manage.py planning_setup                       # groups only
  manage.py planning_setup --user NAME --role manager --password-file PATH

A new user's password is generated and written to --password-file (keep that
file out of git); it is never printed.
"""
from pathlib import Path
import secrets
from django.contrib.auth.models import Group, Permission, User
from django.core.management.base import BaseCommand, CommandError
from planning.datasets import discover

ROLES = {'planner': ('Planner', ['request_plan']),
         'manager': ('Production manager', ['request_plan', 'approve_plan'])}


class Command(BaseCommand):
    help = 'Create planner roles and optionally a local user.'

    def add_arguments(self, parser):
        parser.add_argument('--user')
        parser.add_argument('--role', choices=sorted(ROLES), default='planner')
        parser.add_argument('--password-file')

    def handle(self, user=None, role='planner', password_file=None, **options):
        groups = {}
        for key, (name, codenames) in ROLES.items():
            group, _ = Group.objects.get_or_create(name=name)
            group.permissions.set(Permission.objects.filter(content_type__app_label='planning', codename__in=codenames))
            groups[key] = group
        self.stdout.write(f'Roles ready: {", ".join(name for name, _ in ROLES.values())}')
        self.stdout.write(f'Factory captures found: {len(discover())}')
        if not user:
            return
        if User.objects.filter(username=user).exists():
            account = User.objects.get(username=user)
            self.stdout.write(f'User {user} exists; password unchanged')
        else:
            if not password_file:
                raise CommandError('--password-file is required to create a user')
            path = Path(password_file)
            if path.exists():
                raise CommandError(f'{path} already exists; choose a new file')
            password = secrets.token_urlsafe(18)
            account = User.objects.create_user(user, password=password)
            path.write_text(f'username={user}\npassword={password}\n', encoding='utf-8')
            self.stdout.write(f'User {user} created; password written to {path}')
        account.groups.add(groups[role])
        self.stdout.write(f'{user} is a {ROLES[role][0]}')
