"""Build and validate a fresh isolated schema; never activate it automatically."""
from pathlib import Path
import os, sys, json, hashlib
from datetime import datetime
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
import mysql.connector
from data_generation import seed_database_fixed as seed
from data_generation.validation_records import validate_and_record


def main():
    reference = datetime.fromisoformat(os.getenv('SEED_REFERENCE_TIME','2026-09-10T00:00:00'))
    planning_seed = int(os.getenv('SEED_PLANNING_SEED', str(seed.SEED)))
    actual_seed = int(os.getenv('SEED_ACTUAL_EXECUTION_SEED', str(seed.SEED)))
    source = seed.db_config['database']
    target = source + '_seed_v2_' + datetime.now().strftime('%Y%m%d_%H%M%S')
    assert all(c.isalnum() or c=='_' for c in source+target)
    out=ROOT/'outputs/seeder_v2'/target;out.mkdir(parents=True, exist_ok=False)
    manifest={'source_schema':source,'database':target,'reference_time':reference.isoformat(),
              'company':'Needle Craft Jordan','workforce':9000,
              'planning_seed':planning_seed,'actual_execution_seed':actual_seed,
              'provenance':'Resampled synthetic scenarios; no observed production outcomes',
              'policy':'One factory per order; scenario-wide leave exclusion; same-worker pause/resume; synthetic repair before work',
              'success':False}
    manifest['source_hashes']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [ROOT/'data_generation'/n for n in ('seed_database_fixed.py','simulation_calendar.py','validate_seed.py','grid.xls','Plan.xlsx','PPC-1.xlsx','WorkStation.xlsx')]}
    path=out/'manifest.json'
    path.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    config=dict(seed.db_config)
    conn=mysql.connector.connect(**config);cur=conn.cursor()
    try:
        cur.execute(f'CREATE DATABASE `{target}`')
        cur.execute(f'SHOW FULL TABLES FROM `{source}` WHERE Table_type=\'BASE TABLE\'')
        tables=[r[0] for r in cur.fetchall()]
        ddls=[]
        for table in tables:
            cur.execute(f'SHOW CREATE TABLE `{source}`.`{table}`');ddls.append(cur.fetchone()[1])
        cur.execute(f'USE `{target}`');cur.execute('SET FOREIGN_KEY_CHECKS=0')
        for ddl in ddls:cur.execute(ddl)
        cur.execute('SET FOREIGN_KEY_CHECKS=1')
    finally:
        cur.close();conn.close()
    seed.db_config['database']=target
    try:
        seed.execute_complete_seed(reference_time=reference,
                                   planning_seed=planning_seed,
                                   actual_seed=actual_seed)
        config['database']=target
        conn=mysql.connector.connect(**config)
        try:
            conn.start_transaction(readonly=True)
            record=validate_and_record(conn)
            result=record['result']
            manifest['validation_id']=record['validation_id']
            manifest['latest_validation']=str(ROOT/'outputs/dataset_validations'/target/'latest.json')
        finally:conn.close()
        (out/'validation.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        if not result['valid']:raise RuntimeError(f"Staging validation failed: {result['errors']}")
        manifest['success']=True
    except BaseException as exc:
        manifest['error']=repr(exc);raise
    finally:
        path.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps(manifest,indent=2))


if __name__=='__main__':main()
