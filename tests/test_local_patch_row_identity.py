"""Exercise real UPDATE/rollback behavior on an isolated in-memory SQL database."""
from datetime import datetime, timedelta
import pandas as pd
import pytest
from sqlalchemy import create_engine, MetaData, Table, Column, Integer, String, DateTime, select
from sqlalchemy.orm import Session
import agent.impact_assessor as impact

T0 = datetime(2026, 9, 10, 8)


@pytest.fixture
def store(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    meta = MetaData()
    schedule = Table('Job_Shop_Schedules', meta,
        Column('schedule_item_id', Integer, primary_key=True),
        Column('production_order_id', Integer), Column('operation_id', Integer),
        Column('asset_id', Integer), Column('line_id', Integer), Column('employee_id', Integer),
        Column('execution_status', String),
        Column('planned_start_time', DateTime), Column('planned_end_time', DateTime))
    meta.create_all(engine)
    with engine.begin() as conn:
        conn.execute(schedule.insert(), [dict(schedule_item_id=i,production_order_id=663,
            operation_id=1874,asset_id=2025,line_id=10,employee_id=None,execution_status='Scheduled',
            planned_start_time=T0+timedelta(hours=i-1),planned_end_time=T0+timedelta(hours=i))
            for i in (1,2,3)])
    def sessions():
        with Session(engine) as session:
            yield session
    monkeypatch.setattr(impact,'get_db_session',sessions)
    def rows():
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(schedule).order_by(schedule.c.schedule_item_id)).mappings()]
    yield engine,schedule,rows
    engine.dispose()


def assessment(row):
    return dict(schedule_item_id=row['schedule_item_id'],original_snapshot=dict(row),
        production_order_id=row['production_order_id'],operation_id=row['operation_id'],
        original_asset_id=row['asset_id'],candidate_asset_id=3000,candidate_line_id=20,candidate_start_time=T0+timedelta(days=1),
        duration=timedelta(hours=1),impact_hours=1.0,donor_plan=None)


def test_only_intended_lot_moves_when_old_key_matches_three_rows(store):
    _,_,rows=store
    before=rows()
    assert impact.apply_local_patch(assessment(before[1]))
    after=rows()
    assert after[0]==before[0] and after[2]==before[2]
    assert after[1]['asset_id']==3000
    assert after[1]['line_id']==20
    assert after[1]['planned_start_time']==T0+timedelta(days=1)


@pytest.mark.parametrize('change',['delete','time','worker','status'])
def test_stale_or_missing_row_is_rejected(store,change):
    engine,schedule,rows=store
    proposed=assessment(rows()[0])
    with engine.begin() as conn:
        if change=='delete':conn.execute(schedule.delete().where(schedule.c.schedule_item_id==1))
        else:
            values={'time':{'planned_end_time':T0+timedelta(hours=9)},
                    'worker':{'employee_id':42},'status':{'execution_status':'Done'}}[change]
            conn.execute(schedule.update().where(schedule.c.schedule_item_id==1).values(**values))
    before=rows()
    assert not impact.apply_local_patch(proposed)
    assert rows()==before


def test_stale_donor_rolls_back_affected_update(store):
    engine,schedule,rows=store
    before=rows();proposed=assessment(before[0]);donor=before[1]
    proposed['donor_plan']=dict(schedule_item_id=2,original_snapshot=dict(donor),
        new_start=T0+timedelta(days=2),new_end=T0+timedelta(days=2,hours=1))
    with engine.begin() as conn:
        conn.execute(schedule.update().where(schedule.c.schedule_item_id==2).values(employee_id=42))
    before=rows()
    assert not impact.apply_local_patch(proposed)
    assert rows()==before


def test_missing_identity_never_falls_back_to_nonunique_key(store):
    _,_,rows=store;proposed=assessment(rows()[0]);del proposed['schedule_item_id']
    before=rows();assert not impact.apply_local_patch(proposed);assert rows()==before


def test_tentative_release_targets_only_the_donor_lot(store):
    _,_,rows=store;bookings={2025:pd.DataFrame(rows())}
    impact._release_tentative_slot(bookings,2025,663,1874,schedule_item_id=2)
    assert bookings[2025].schedule_item_id.tolist()==[1,3]
    with pytest.raises(ValueError,match='unique schedule_item_id'):
        impact._release_tentative_slot(bookings,2025,663,1874)


def test_donor_assessment_carries_unique_id_and_original_fields(monkeypatch,store):
    _,_,rows=store;row=pd.Series(rows()[1])
    monkeypatch.setattr(impact,'_find_slot',lambda *a:(T0+timedelta(hours=2),row))
    monkeypatch.setattr(impact,'get_order_slack_days',lambda oid, now=None: 100)
    monkeypatch.setattr(impact,'urgency_score',lambda *a,**k:0)
    monkeypatch.setattr(impact,'decide_tier',lambda *a,**k:{'tier':impact.Tier.LOCAL_PATCH})
    _,donor=impact._attempt_donor_preemption('other',1,timedelta(hours=1),2025,pd.DataFrame([rows()[1]]),T0+timedelta(hours=1),set())
    assert donor['schedule_item_id']==2
    assert donor['original_snapshot']==rows()[1]


def test_multiple_blockers_reject_preemption_with_ids(monkeypatch, store):
    _, _, rows = store
    monkeypatch.setattr(impact, 'get_order_slack_days', lambda oid, now=None: 100)
    monkeypatch.setattr(impact, 'urgency_score', lambda *a, **k: 0)
    monkeypatch.setattr(impact, 'decide_tier', lambda *a, **k: {'tier': impact.Tier.LOCAL_PATCH})
    rejections = []
    start, donor = impact._attempt_donor_preemption('other', 1, timedelta(hours=1),
        2025, pd.DataFrame(rows()[:2]), T0, set(), rejections=rejections)
    assert start is None and donor is None
    assert rejections[0]['blocking_schedule_item_ids'] == [1]
    assert rejections[0]['code'] == 'PREEMPTION_RESERVATION_CONFLICT'


def test_commit_rejects_overlap_with_unchanged_lot(store):
    _, _, rows = store
    before = rows()
    proposed = assessment(before[2])
    proposed.update(candidate_asset_id=2025, candidate_start_time=T0)
    proposed['donor_plan'] = dict(schedule_item_id=2, original_snapshot=before[1],
        new_start=T0+timedelta(hours=3), new_end=T0+timedelta(hours=4))
    assert not impact.apply_local_patch(proposed)
    assert proposed['patch_rejection']['blocking_schedule_item_ids'] == [1]
    assert rows() == before
