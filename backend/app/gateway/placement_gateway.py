"""Rail placement occupancy gateway.

Single gateway for every read and write of ``RailPlacement`` rows (挂杆占位).
Route handlers must go through this module instead of querying or mutating
``RailPlacement`` directly.

Read port (读侧):
    - :func:`list_active_segments` — list the active segments on one rail.
    - :func:`find_placements_by_order` — find the active placements of one work order.

Write port (写侧):
    - :func:`write_placement` — persist a new active placement.
    - :func:`release_placements` — release (deactivate) an order's active placements.

The write port never commits: the caller owns the transaction so a placement
write and the matching order-status change stay atomic, exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.models import RailPlacement
from app.services.rail_engine import Segment


@dataclass(frozen=True)
class ActiveSegment:
    """Read-only view of one active placement row."""

    placement_id: int
    rail_id: int
    order_id: int
    start_cm: float
    end_cm: float

    def to_engine_segment(self) -> Segment:
        """Adapt to the rail-engine segment used by First-Fit."""
        return Segment(self.start_cm, self.end_cm)


def _to_view(row: RailPlacement) -> ActiveSegment:
    return ActiveSegment(
        placement_id=row.id,
        rail_id=row.rail_id,
        order_id=row.order_id,
        start_cm=row.start_cm,
        end_cm=row.end_cm,
    )


# ---------------------------------------------------------------------------
# Read port
# ---------------------------------------------------------------------------


def list_active_segments(db: Session, rail_id: int) -> list[ActiveSegment]:
    """List the active segments on a rail, ordered by start position."""
    rows = db.scalars(
        select(RailPlacement)
        .where(RailPlacement.rail_id == rail_id, RailPlacement.active == 1)
        .order_by(RailPlacement.start_cm)
    ).all()
    return [_to_view(row) for row in rows]


def find_placements_by_order(db: Session, order_id: int) -> list[ActiveSegment]:
    """Find the active placements of a work order."""
    rows = db.scalars(
        select(RailPlacement).where(
            RailPlacement.order_id == order_id, RailPlacement.active == 1
        )
    ).all()
    return [_to_view(row) for row in rows]


# ---------------------------------------------------------------------------
# Write port
# ---------------------------------------------------------------------------


def write_placement(
    db: Session,
    *,
    rail_id: int,
    order_id: int,
    start_cm: float,
    end_cm: float,
) -> ActiveSegment:
    """Persist a new active placement. Does not commit."""
    row = RailPlacement(
        rail_id=rail_id,
        order_id=order_id,
        start_cm=start_cm,
        end_cm=end_cm,
    )
    db.add(row)
    db.flush()
    return _to_view(row)


def release_placements(db: Session, order_id: int) -> int:
    """Release every active placement of a work order. Does not commit.

    Returns the number of placements released.
    """
    rows = db.scalars(
        select(RailPlacement).where(
            RailPlacement.order_id == order_id, RailPlacement.active == 1
        )
    ).all()
    for row in rows:
        row.active = 0
    return len(rows)
