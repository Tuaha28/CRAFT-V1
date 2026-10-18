"""Run a CRAFT evaluation script with the SELECT-only legacy reader account.

utils.config reads DB_USER/DB_PASS via load_dotenv(), which never overrides
variables already present in the environment. Exporting the verified
craft_reader credentials first makes every MySQL access in the target run
read-only at the database level, independent of the script's own discipline.

Usage: venv\\Scripts\\python.exe scratch/run_readonly.py <script.py> [args...]
"""
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values  # noqa: E402
import pymysql  # noqa: E402
from web_v1.backend.integrations.craft.legacy_repository import verify_select_only_grants  # noqa: E402

config = dotenv_values(ROOT / ".env")
user, password = config.get("LEGACY_DB_USER"), config.get("LEGACY_DB_PASSWORD")
if not user or user.lower() == "root" or not password:
    raise SystemExit("READONLY_READER_NOT_CONFIGURED")
probe = pymysql.connect(host=config.get("LEGACY_DB_HOST", "localhost"),
                        port=int(config.get("LEGACY_DB_PORT", 3306)), user=user,
                        password=password, database="garment_fyp_db", connect_timeout=5,
                        cursorclass=pymysql.cursors.DictCursor)
try:
    with probe.cursor() as cursor:
        cursor.execute("SHOW GRANTS")
        verify_select_only_grants(cursor.fetchall())
finally:
    probe.close()

os.environ.update(DB_USER=user, DB_PASS=password,
                  DB_HOST=config.get("LEGACY_DB_HOST", "localhost"),
                  DB_PORT=str(config.get("LEGACY_DB_PORT", 3306)), DB_NAME="garment_fyp_db")
script = Path(sys.argv[1]).resolve()
sys.argv = [str(script), *sys.argv[2:]]
print(f"READ_ONLY_DB_ACCOUNT_VERIFIED: running {script.name}", flush=True)
runpy.run_path(str(script), run_name="__main__")
