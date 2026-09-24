from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.models import HangRail, RailPlacement, Store, WorkOrder


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def build_scene(db):
    """Store with two rails and four orders, mirroring the live seed shape.

    rail_a (200cm): hung_1 [0,45), hung_2 [45,80) active; picked [80,110) released.
    rail_b (160cm): empty.
    """
    store = Store(name="测试店")
    db.add(store)
    db.flush()
    rail_a = HangRail(store_id=store.id, label="A 杆", length_cm=200)
    rail_b = HangRail(store_id=store.id, label="B 杆", length_cm=160)
    db.add_all([rail_a, rail_b])
    db.flush()
    now = datetime.utcnow()
    hung_1 = WorkOrder(store_id=store.id, ticket_code="T-001", garment_name="羊毛大衣",
                       length_cm=45, status="hung", due_at=now + timedelta(days=1), hung_at=now)
    hung_2 = WorkOrder(store_id=store.id, ticket_code="T-002", garment_name="西装套装",
                       length_cm=35, status="hung", due_at=now + timedelta(days=1), hung_at=now)
    ready = WorkOrder(store_id=store.id, ticket_code="T-003", garment_name="羽绒服",
                      length_cm=50, status="ready", due_at=now + timedelta(days=1))
    picked = WorkOrder(store_id=store.id, ticket_code="T-004", garment_name="连衣裙",
                       length_cm=30, status="picked", due_at=now + timedelta(days=1), hung_at=now)
    db.add_all([hung_1, hung_2, ready, picked])
    db.flush()
    db.add_all([
        RailPlacement(rail_id=rail_a.id, order_id=hung_1.id, start_cm=0, end_cm=45),
        RailPlacement(rail_id=rail_a.id, order_id=hung_2.id, start_cm=45, end_cm=80),
        RailPlacement(rail_id=rail_a.id, order_id=picked.id, start_cm=80, end_cm=110, active=0),
    ])
    db.commit()
    return SimpleNamespace(
        store=store, rail_a=rail_a, rail_b=rail_b,
        hung_1=hung_1, hung_2=hung_2, ready=ready, picked=picked,
    )


@pytest.fixture()
def scene(db):
    return build_scene(db)
