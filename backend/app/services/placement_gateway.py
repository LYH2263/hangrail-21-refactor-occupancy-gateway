"""挂杆占位网关：RailPlacement 的唯一读写入口。

读端口：
- list_active_segments   按杆列出 active 占位段（First-Fit 的 occupied 输入）
- list_active_placements 按杆列出 active 占位行（占用视图联工单展示）
- find_active_for_order  按工单查找 active 占位

写端口：
- write_placement        写入一条 active 占位（上杆）
- release_for_order      释放工单全部 active 占位（取件）

约束：上杆、取件、逾期扫描等业务流程只允许经写端口改库；
路由层不得再直接拼 Segment 或散落改写 active。
网关函数一律不 commit，事务边界与现网一致，由调用方统一提交。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.models import RailPlacement
from app.services.rail_engine import Placement, Segment


# ---------- 读端口 ----------

def list_active_segments(db: Session, rail_id: int) -> list[Segment]:
    """按杆列出 active 占位段，与现网 first_fit 的 occupied 输入一致。"""
    return [Segment(p.start_cm, p.end_cm) for p in list_active_placements(db, rail_id)]


def list_active_placements(db: Session, rail_id: int) -> list[RailPlacement]:
    """按杆列出 active 占位行。"""
    return list(
        db.scalars(
            select(RailPlacement).where(
                RailPlacement.rail_id == rail_id, RailPlacement.active == 1
            )
        ).all()
    )


def find_active_for_order(db: Session, order_id: int) -> list[RailPlacement]:
    """按工单查找 active 占位。"""
    return list(
        db.scalars(
            select(RailPlacement).where(
                RailPlacement.order_id == order_id, RailPlacement.active == 1
            )
        ).all()
    )


# ---------- 写端口 ----------

def write_placement(db: Session, *, rail_id: int, order_id: int, placement: Placement) -> RailPlacement:
    """写入一条 active 占位（上杆）。不 commit，由调用方统一提交。"""
    row = RailPlacement(
        rail_id=rail_id,
        order_id=order_id,
        start_cm=placement.start_cm,
        end_cm=placement.end_cm,
    )
    db.add(row)
    return row


def release_for_order(db: Session, order_id: int) -> int:
    """释放工单全部 active 占位（取件），返回释放条数。不 commit。"""
    rows = find_active_for_order(db, order_id)
    for row in rows:
        row.active = 0
    return len(rows)
