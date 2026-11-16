"""Rebuild one demo copy (<database>_demo) from its original, throwing away the app's changes.

Without --run it only prints the commands (safe). With --run it asks you to type RESET, then
dumps the original with the read-only account and loads the copy with the writer account.
The original is only read. Folder of mysql.exe/mysqldump.exe: CRAFT_MYSQL_BIN (else PATH).
"""
import os
import subprocess
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from planning.models import Dataset
from planning.services import event


class Command(BaseCommand):
    help = 'Rebuild a demo copy of a load database from its original (prints the commands unless --run).'

    def add_arguments(self, parser):
        parser.add_argument('--load', required=True, help='30, 60 or 90')
        parser.add_argument('--run', action='store_true', help='actually rebuild the copy (asks to type RESET)')

    def handle(self, **options):
        try:
            load = int(options['load'])
        except ValueError:
            raise CommandError('--load must be 30, 60 or 90') from None
        dataset = Dataset.objects.filter(load_pct=load).exclude(database='').order_by('-registered_at').first()
        if load not in (30, 60, 90) or dataset is None:
            raise CommandError(f'No {options["load"]}% load data is registered; use 30, 60 or 90')
        original = dataset.database
        copy = original + settings.CRAFT_DEMO_SUFFIX
        tools = Path(os.environ.get('CRAFT_MYSQL_BIN', ''))
        dump, mysql = str(tools / 'mysqldump') if str(tools) != '.' else 'mysqldump', \
            str(tools / 'mysql') if str(tools) != '.' else 'mysql'
        self.stdout.write(f'Original (only read): {original}\nDemo copy (rebuilt):  {copy}\n')
        self.stdout.write(f'  {dump} -u <reader> -p --single-transaction {original} | {mysql} -u <writer> -p {copy}\n')
        if not options['run']:
            self.stdout.write('Nothing was changed. Add --run to rebuild the copy.\n')
            return
        if input('Type RESET to rebuild the demo copy: ').strip() != 'RESET':
            raise CommandError('Not confirmed; nothing was changed')
        from dotenv import dotenv_values
        from erp.writer import _secret
        repo = {**dotenv_values(settings.ERP_ENV_FILE), **os.environ}
        reader, reader_pw = repo.get('LEGACY_DB_USER', ''), repo.get('LEGACY_DB_PASSWORD', '')
        writer, writer_pw = _secret('CRAFT_WRITER_USER'), _secret('CRAFT_WRITER_PASSWORD')
        if not (reader and reader_pw and writer and writer_pw):
            raise CommandError('The reader and writer accounts must both be configured')
        source = subprocess.Popen([dump, '-u', reader, '--single-transaction', '--skip-lock-tables', original],
                                  stdout=subprocess.PIPE, env={**os.environ, 'MYSQL_PWD': reader_pw})
        target = subprocess.run([mysql, '-u', writer, copy], stdin=source.stdout, capture_output=True,
                                env={**os.environ, 'MYSQL_PWD': writer_pw}, check=False)
        source.stdout.close()
        if source.wait() or target.returncode:
            raise CommandError('The rebuild failed: ' + (target.stderr or b'').decode(errors='replace')[-500:])
        event(None, None, 'DEMO_RESET', f'Demo copy {copy} rebuilt from {original}')
        self.stdout.write(self.style.SUCCESS(f'{copy} rebuilt from {original}.'))
