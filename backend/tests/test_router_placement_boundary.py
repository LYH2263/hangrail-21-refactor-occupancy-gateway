"""Boundary tests: the route layer must not depend on the scattered
RailPlacement update paths any more, and the external semantics of
hang / pickup / occupancy / overdue must stay exactly as before."""

import ast
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.api.router as router_module
from app.api.router import api_router
from app.database import get_db
from app.gateway import placement_gateway
from app.models.models import WorkOrder

ROUTER_SOURCE = Path(router_module.__file__).read_text(encoding="utf-8")
ROUTER_TREE = ast.parse(ROUTER_SOURCE)


# ---------------------------------------------------------------------------
# Static boundary: router must go through the gateway, not RailPlacement
# ---------------------------------------------------------------------------


def test_router_does_not_reference_railplacement():
    names = {node.id for node in ast.walk(ROUTER_TREE) if isinstance(node, ast.Name)}
    assert "RailPlacement" not in names
    imported = {
        alias.name
        for node in ast.walk(ROUTER_TREE)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "RailPlacement" not in imported


def test_router_has_no_scattered_active_writes():
    for node in ast.walk(ROUTER_TREE):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                assert not (
                    isinstance(target, ast.Attribute) and target.attr == "active"
                ), "router still flips RailPlacement.active directly"


def test_router_does_not_construct_segments():
    for node in ast.walk(ROUTER_TREE):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "Segment", "router still builds engine Segment itself"


def test_router_uses_gateway_ports():
    gateway_calls = {
        node.func.attr
        for node in ast.walk(ROUTER_TREE)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "placement_gateway"
    }
    assert {
        "list_active_segments",
        "write_placement",
        "release_placements",
    } <= gateway_calls


# ---------------------------------------------------------------------------
# External semantics: hang / pickup / occupancy / overdue via HTTP
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(api_router, prefix="/api")

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_hang_places_first_fit_and_marks_order(client, db, scene):
    resp = client.post("/api/hang", json={"order_id": scene.ready.id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "hung"
    assert body["hung_at"] is not None
    # first fit on rail A: [0,45) and [45,80) occupied -> [80,130)
    views = placement_gateway.list_active_segments(db, scene.rail_a.id)
    assert [(v.order_id, v.start_cm, v.end_cm) for v in views] == [
        (scene.hung_1.id, 0, 45),
        (scene.hung_2.id, 45, 80),
        (scene.ready.id, 80, 130),
    ]


def test_hang_falls_through_to_next_rail(client, db, scene):
    scene.ready.length_cm = 150  # rail A has only 120cm free -> rail B
    db.commit()
    resp = client.post("/api/hang", json={"order_id": scene.ready.id})
    assert resp.status_code == 200
    views = placement_gateway.list_active_segments(db, scene.rail_b.id)
    assert [(v.order_id, v.start_cm, v.end_cm) for v in views] == [(scene.ready.id, 0, 150)]


def test_hang_conflict_when_no_rail_fits(client, db, scene):
    scene.ready.length_cm = 170  # fits neither 200cm rail (120 free) nor 160cm rail
    db.commit()
    resp = client.post("/api/hang", json={"order_id": scene.ready.id})
    assert resp.status_code == 409


def test_hang_pinned_rail_does_not_spill(client, db, scene):
    scene.ready.length_cm = 150
    db.commit()
    resp = client.post(
        "/api/hang", json={"order_id": scene.ready.id, "rail_id": scene.rail_a.id}
    )
    assert resp.status_code == 409
    assert placement_gateway.list_active_segments(db, scene.rail_b.id) == []


def test_hang_rejects_bad_order_or_status(client, db, scene):
    assert client.post("/api/hang", json={"order_id": 999999}).status_code == 404
    assert (
        client.post("/api/hang", json={"order_id": scene.picked.id}).status_code == 400
    )


def test_pickup_releases_placement(client, db, scene):
    resp = client.post("/api/pickup", json={"ticket_code": scene.hung_1.ticket_code})
    assert resp.status_code == 200
    assert resp.json()["status"] == "picked"
    assert placement_gateway.find_placements_by_order(db, scene.hung_1.id) == []
    views = placement_gateway.list_active_segments(db, scene.rail_a.id)
    assert [v.order_id for v in views] == [scene.hung_2.id]


def test_pickup_rejects_invalid_ticket_or_status(client, db, scene):
    assert client.post("/api/pickup", json={"ticket_code": "NOPE"}).status_code == 404
    assert (
        client.post("/api/pickup", json={"ticket_code": scene.ready.ticket_code}).status_code
        == 400
    )


def test_occupancy_lists_active_segments_sorted(client, db, scene):
    resp = client.get(f"/api/occupancy/{scene.rail_a.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rail_id"] == scene.rail_a.id
    assert body["label"] == "A 杆"
    assert body["length_cm"] == 200
    assert [(s["order_id"], s["start_cm"], s["end_cm"]) for s in body["segments"]] == [
        (scene.hung_1.id, 0, 45),
        (scene.hung_2.id, 45, 80),
    ]
    assert body["segments"][0]["ticket_code"] == scene.hung_1.ticket_code
    assert body["segments"][0]["garment_name"] == scene.hung_1.garment_name


def test_occupancy_missing_rail(client):
    assert client.get("/api/occupancy/999999").status_code == 404


def test_overdue_scan_marks_orders_and_keeps_placements(client, db, scene):
    scene.hung_1.due_at = datetime.utcnow() - timedelta(hours=1)
    scene.ready.due_at = datetime.utcnow() - timedelta(days=1)
    db.commit()
    resp = client.post("/api/overdue/scan")
    assert resp.status_code == 200
    marked = {o["id"] for o in resp.json()}
    assert marked == {scene.hung_1.id, scene.ready.id}
    assert db.get(WorkOrder, scene.hung_1.id).status == "overdue"
    assert db.get(WorkOrder, scene.ready.id).status == "overdue"
    assert db.get(WorkOrder, scene.hung_2.id).status == "hung"
    # overdue orders stay on the rail: placements untouched
    views = placement_gateway.list_active_segments(db, scene.rail_a.id)
    assert [v.order_id for v in views] == [scene.hung_1.id, scene.hung_2.id]
