import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import create_engine, text
from utils.config import DB_USER, DB_HOST, DB_PORT
from urllib.parse import quote_plus

CLONE_DB = "garment_fyp_db_postmortem_test"
pw = os.getenv("DB_PASS")
server_url = f"mysql+pymysql://{DB_USER}:{quote_plus(pw)}@{DB_HOST}:{DB_PORT}/"
engine = create_engine(server_url)
with engine.connect() as conn:
    conn.execute(text(f"DROP DATABASE IF EXISTS `{CLONE_DB}`"))
    conn.commit()
print(f"Dropped {CLONE_DB} -- disposable test DB cleaned up, production untouched throughout.")
