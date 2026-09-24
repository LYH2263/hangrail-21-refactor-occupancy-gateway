"""Placement gateway read/write ports must behave exactly like the live
(现网) direct-ORM placement access they replace."""

from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.gateway import placement_gateway
from app.models.models import RailPlacement, WorkOrder
from app.services.rail_engine import Segment, first_fit
from tests.conftest import build_scene

FIXED_HUNG_AT = datetime(2026, 1, 1, 8, 0, 0)


def legacy_active_rows(db, rail_id):
    """The pre-refactor router query for active placements on a rail."""
    return db.scalars(
        select(RailPlacement).where(
            RailPlacement.rail_id == rail_id, RailPlacement.active == 1
        )
    ).all()


def test_list_active_segments_matches_legacy_query(db, scene):
    legacy = legacy_active_rows(db, scene.rail_a.id)
    views = placement_gateway.list_active_segments(db, scene.rail_a.id)
    assert [(v.order_id, v.start_cm, v.end_cm) for v in views] == [
        (p.order_id, p.start_cm, p.end_cm)
        for p in sorted(legacy, key=lambda p: p.start_cm)
    ]


def test_list_active_segments_excludes_inactive(db, scene):
    views = placement_gateway.list_active_segments(db, scene.rail_a.id)
    assert scene.picked.id not in {v.order_id for v in views}
    assert [v.order_id for v in views] == [scene.hung_1.id, scene.hung_2.id]


def test_list_active_segments_empty_rail(db, scene):
    assert placement_gateway.list_active_segments(db, scene.rail_b.id) == []


def test_find_placements_by_order(db, scene):
    views = placement_gateway.find_placements_by_order(db, scene.hung_1.id)
    assert len(views) == 1
    view = views[0]
    assert view.rail_id == scene.rail_a.id
    assert (view.start_cm, view.end_cm) == (0, 45)
    assert view.placement_id is not None


def test_find_placements_by_order_skips_released_and_unknown(db, scene):
    assert placement_gateway.find_placements_by_order(db, scene.picked.id) == []
    assert placement_gateway.find_placements_by_order(db, 999999) == []


def test_write_placement_persists_active_row(db, scene):
    view = placement_gateway.write_placement(
        db, rail_id=scene.rail_b.id, order_id=scene.ready.id, start_cm=0, end_cm=50
    )
    row = db.scalar(select(RailPlacement).where(RailPlacement.id == view.placement_id))
    assert row is not None
    assert row.active == 1
    assert (row.rail_id, row.order_id, row.start_cm, row.end_cm) == (
        scene.rail_b.id, scene.ready.id, 0, 50,
    )
    # round-trips through the read port
    reread = placement_gateway.list_active_segments(db, scene.rail_b.id)
    assert [(v.order_id, v.start_cm, v.end_cm) for v in reread] == [(scene.ready.id, 0, 50)]


def test_write_placement_does_not_commit(db, scene):
    placement_gateway.write_placement(
        db, rail_id=scene.rail_b.id, order_id=scene.ready.id, start_cm=0, end_cm=50
    )
    db.rollback()
    assert placement_gateway.list_active_segments(db, scene.rail_b.id) == []


def test_release_placements_flips_active_and_keeps_row(db, scene):
    released = placement_gateway.release_placements(db, scene.hung_1.id)
    assert released == 1
    row = db.scalar(
        select(RailPlacement).where(RailPlacement.order_id == scene.hung_1.id)
    )
    assert row is not None and row.active == 0
    remaining = placement_gateway.list_active_segments(db, scene.rail_a.id)
    assert [v.order_id for v in remaining] == [scene.hung_2.id]


def test_release_placements_without_active_rows(db, scene):
    assert placement_gateway.release_placements(db, scene.ready.id) == 0
    assert placement_gateway.release_placements(db, scene.picked.id) == 0


def test_engine_segments_feed_first_fit(db, scene):
    views = placement_gateway.list_active_segments(db, scene.rail_a.id)
    occupied = [v.to_engine_segment() for v in views]
    assert occupied == [Segment(0, 45), Segment(45, 80)]
    place = first_fit(scene.rail_a.length_cm, occupied, scene.ready.length_cm)
    assert (place.start_cm, place.end_cm) == (80, 130)


def _fresh_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _placement_dump(db):
    rows = db.scalars(select(RailPlacement).order_by(RailPlacement.id)).all()
    return [(r.rail_id, r.order_id, r.start_cm, r.end_cm, r.active) for r in rows]


def _add_ready_order(db, store, ticket_code, length_cm):
    order = WorkOrder(
        store_id=store.id,
        ticket_code=ticket_code,
        garment_name="婚纱",
        length_cm=length_cm,
        status="ready",
        due_at=datetime.utcnow() + timedelta(days=1),
    )
    db.add(order)
    db.flush()
    return order


def _legacy_hang(db, order, rails):
    """Pre-refactor hang path, verbatim from the old router."""
    for rail in rails:
        active = legacy_active_rows(db, rail.id)
        occupied = [Segment(p.start_cm, p.end_cm) for p in active]
        place = first_fit(rail.length_cm, occupied, order.length_cm)
        if place is None:
            continue
        db.add(
            RailPlacement(
                rail_id=rail.id,
                order_id=order.id,
                start_cm=place.start_cm,
                end_cm=place.end_cm,
            )
        )
        order.status = "hung"
        order.hung_at = FIXED_HUNG_AT
        db.commit()
        return True
    return False


def _gateway_hang(db, order, rails):
    for rail in rails:
        active = placement_gateway.list_active_segments(db, rail.id)
        occupied = [seg.to_engine_segment() for seg in active]
        place = first_fit(rail.length_cm, occupied, order.length_cm)
        if place is None:
            continue
        placement_gateway.write_placement(
            db,
            rail_id=rail.id,
            order_id=order.id,
            start_cm=place.start_cm,
            end_cm=place.end_cm,
        )
        order.status = "hung"
        order.hung_at = FIXED_HUNG_AT
        db.commit()
        return True
    return False


def test_gateway_matches_live_hang_and_pickup(db, scene):
    """Gateway-driven hang+pickup leaves the placements table identical to
    the legacy direct-ORM path on the same workload."""
    legacy_db = _fresh_session()
    try:
        legacy_scene = build_scene(legacy_db)
        legacy_rails = [legacy_scene.rail_a, legacy_scene.rail_b]
        gateway_rails = [scene.rail_a, scene.rail_b]

        # hang the ready order (50cm): first fit lands at [80,130) on rail A
        assert _legacy_hang(legacy_db, legacy_scene.ready, legacy_rails)
        assert _gateway_hang(db, scene.ready, gateway_rails)
        assert _placement_dump(db) == _placement_dump(legacy_db)

        # a 150cm order skips rail A (only 120cm free) and lands on rail B
        big_legacy = _add_ready_order(legacy_db, legacy_scene.store, "T-150", 150)
        big_gateway = _add_ready_order(db, scene.store, "T-150", 150)
        assert _legacy_hang(legacy_db, big_legacy, legacy_rails)
        assert _gateway_hang(db, big_gateway, gateway_rails)
        assert _placement_dump(db) == _placement_dump(legacy_db)

        # pickup releases exactly the same rows
        placement_gateway.release_placements(db, scene.hung_1.id)
        for p in legacy_active_rows(legacy_db, legacy_scene.rail_a.id):
            if p.order_id == legacy_scene.hung_1.id:
                p.active = 0
        db.commit()
        legacy_db.commit()
        assert _placement_dump(db) == _placement_dump(legacy_db)
    finally:
        legacy_db.close()
