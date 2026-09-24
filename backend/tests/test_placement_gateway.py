"""网关读写与现网占位行为一致性。

"现网"指重构前路由内联的 RailPlacement 操作；本文件以同样的内联 SQL
作为参照实现，逐字段比对网关读侧、逐行比对网关写侧，并锁定
网关读数据喂给 First-Fit 的结果与现网一致。
"""

from datetime import datetime, timedelta

from sqlalchemy import select

from app.models.models import HangRail, RailPlacement, Store, WorkOrder
from app.services import placement_gateway
from app.services.rail_engine import Placement, Segment, first_fit
from app.services.seed import seed_if_empty


# ---- 现网（重构前路由）内联实现的参照复刻，不得再被生产代码引用 ----

def legacy_active_placements(db, rail_id):
    return list(
        db.scalars(
            select(RailPlacement).where(
                RailPlacement.rail_id == rail_id, RailPlacement.active == 1
            )
        ).all()
    )


def legacy_active_segments(db, rail_id):
    return [Segment(p.start_cm, p.end_cm) for p in legacy_active_placements(db, rail_id)]


def legacy_write_placement(db, rail_id, order_id, start_cm, end_cm):
    db.add(
        RailPlacement(
            rail_id=rail_id,
            order_id=order_id,
            start_cm=start_cm,
            end_cm=end_cm,
        )
    )


def legacy_release_for_order(db, order_id):
    rows = db.scalars(
        select(RailPlacement).where(
            RailPlacement.order_id == order_id, RailPlacement.active == 1
        )
    ).all()
    for p in rows:
        p.active = 0


def seeded(db):
    seed_if_empty(db)
    rails = db.scalars(select(HangRail).order_by(HangRail.id)).all()
    orders = db.scalars(select(WorkOrder).order_by(WorkOrder.id)).all()
    return rails, orders


class TestReadPortMatchesLegacy:
    def test_segments_match_legacy_on_seed_data(self, db):
        rails, _ = seeded(db)
        for rail in rails:
            assert placement_gateway.list_active_segments(db, rail.id) == legacy_active_segments(db, rail.id)

    def test_segments_exact_values_on_seed_data(self, db):
        rails, _ = seeded(db)
        # 现网演示数据的确定占位：A 杆 [0,45) [45,80)，B 杆 [0,40)
        assert placement_gateway.list_active_segments(db, rails[0].id) == [
            Segment(0, 45),
            Segment(45, 80),
        ]
        assert placement_gateway.list_active_segments(db, rails[1].id) == [Segment(0, 40)]

    def test_placements_rows_match_legacy(self, db):
        rails, _ = seeded(db)
        for rail in rails:
            got = placement_gateway.list_active_placements(db, rail.id)
            want = legacy_active_placements(db, rail.id)
            assert [(p.rail_id, p.order_id, p.start_cm, p.end_cm, p.active) for p in got] == [
                (p.rail_id, p.order_id, p.start_cm, p.end_cm, p.active) for p in want
            ]

    def test_read_excludes_released_rows(self, db):
        rails, orders = seeded(db)
        legacy_release_for_order(db, orders[0].id)
        db.commit()
        assert placement_gateway.list_active_segments(db, rails[0].id) == legacy_active_segments(db, rails[0].id)
        assert placement_gateway.list_active_segments(db, rails[0].id) == [Segment(45, 80)]

    def test_find_active_for_order(self, db):
        _, orders = seeded(db)
        rows = placement_gateway.find_active_for_order(db, orders[0].id)
        assert len(rows) == 1
        assert (rows[0].start_cm, rows[0].end_cm, rows[0].active) == (0, 45, 1)
        assert placement_gateway.find_active_for_order(db, 99999) == []
        # 已释放的占位不再被查到
        legacy_release_for_order(db, orders[0].id)
        db.commit()
        assert placement_gateway.find_active_for_order(db, orders[0].id) == []


class TestWritePortMatchesLegacy:
    def _new_order(self, db, store_id, ticket, length):
        o = WorkOrder(
            store_id=store_id,
            ticket_code=ticket,
            garment_name="测试衣物",
            length_cm=length,
            status="ready",
            due_at=datetime.utcnow() + timedelta(days=1),
        )
        db.add(o)
        db.flush()
        return o

    def test_write_placement_row_matches_legacy_shape(self, db):
        rails, orders = seeded(db)
        order = self._new_order(db, rails[0].store_id, "T-9001", 50)

        row = placement_gateway.write_placement(
            db, rail_id=rails[0].id, order_id=order.id, placement=Placement(80, 130)
        )
        db.commit()

        assert row.active == 1  # 与现网 db.add(RailPlacement(...)) 默认 active=1 一致
        persisted = db.scalars(
            select(RailPlacement).where(RailPlacement.order_id == order.id)
        ).one()
        assert (persisted.rail_id, persisted.order_id) == (rails[0].id, order.id)
        assert (persisted.start_cm, persisted.end_cm, persisted.active) == (80, 130, 1)
        # 写后读侧与现网参照一致
        assert placement_gateway.list_active_segments(db, rails[0].id) == legacy_active_segments(db, rails[0].id)

    def test_write_then_read_roundtrip(self, db):
        rails, _ = seeded(db)
        order = self._new_order(db, rails[0].store_id, "T-9002", 20)
        placement_gateway.write_placement(db, rail_id=rails[0].id, order_id=order.id, placement=Placement(80, 100))
        db.commit()
        assert Segment(80, 100) in placement_gateway.list_active_segments(db, rails[0].id)
        assert placement_gateway.find_active_for_order(db, order.id)[0].start_cm == 80

    def test_release_matches_legacy_effect(self, db):
        rails, orders = seeded(db)
        released = placement_gateway.release_for_order(db, orders[0].id)
        db.commit()

        assert released == 1
        # 行仍保留但 active=0，与现网循环 p.active = 0 一致
        row = db.scalars(select(RailPlacement).where(RailPlacement.order_id == orders[0].id)).one()
        assert row.active == 0
        # 其它工单占位不受影响，读侧与现网参照一致
        assert placement_gateway.list_active_segments(db, rails[0].id) == legacy_active_segments(db, rails[0].id)
        assert placement_gateway.list_active_segments(db, rails[0].id) == [Segment(45, 80)]
        assert placement_gateway.list_active_segments(db, rails[1].id) == [Segment(0, 40)]

    def test_release_without_active_rows_is_noop(self, db):
        _, orders = seeded(db)
        assert placement_gateway.release_for_order(db, orders[2].id) == 0  # ready 单无占位
        assert placement_gateway.release_for_order(db, 99999) == 0

    def test_release_then_rewrite_matches_legacy_rehang(self, db):
        """释放后同一工单可重新写入占位（现网逾期重挂路径）。"""
        rails, orders = seeded(db)
        placement_gateway.release_for_order(db, orders[0].id)
        db.commit()
        placement_gateway.write_placement(db, rail_id=rails[1].id, order_id=orders[0].id, placement=Placement(40, 85))
        db.commit()
        assert placement_gateway.list_active_segments(db, rails[1].id) == legacy_active_segments(db, rails[1].id)
        assert placement_gateway.list_active_segments(db, rails[1].id) == [Segment(0, 40), Segment(40, 85)]


class TestFirstFitUnchanged:
    """网关读数据喂给引擎的结果与现网一致（First-Fit 结果不变）。"""

    def test_first_fit_on_gateway_segments(self, db):
        rails, _ = seeded(db)
        assert first_fit(200, placement_gateway.list_active_segments(db, rails[0].id), 50) == Placement(80, 130)
        assert first_fit(160, placement_gateway.list_active_segments(db, rails[1].id), 50) == Placement(40, 90)

    def test_first_fit_no_space_on_gateway_segments(self, db):
        rails, _ = seeded(db)
        # A 杆仅剩 120cm 连续空隙，125cm 放不下
        assert first_fit(200, placement_gateway.list_active_segments(db, rails[0].id), 125) is None

    def test_first_fit_matches_legacy_segments(self, db):
        rails, _ = seeded(db)
        for rail in rails:
            for garment in (10, 35, 50, 80, 121):
                assert first_fit(
                    rail.length_cm, placement_gateway.list_active_segments(db, rail.id), garment
                ) == first_fit(rail.length_cm, legacy_active_segments(db, rail.id), garment)
