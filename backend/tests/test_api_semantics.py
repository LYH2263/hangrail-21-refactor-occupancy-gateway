"""对外语义回归：上杆 / 取件 / 逾期扫描 / 占用视图。

这些测试刻画重构前路由的现网行为（含 First-Fit 落点），
挂杆占位抽取网关后必须逐条保持不变，不得放宽。
"""

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.router import api_router
from app.database import get_db
from app.models.models import HangRail, Store, WorkOrder


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(api_router, prefix="/api")

    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


@pytest.fixture()
def scene(db):
    store = Store(name="测试店")
    db.add(store)
    db.flush()
    rail_a = HangRail(store_id=store.id, label="A 杆", length_cm=100)
    rail_b = HangRail(store_id=store.id, label="B 杆", length_cm=60)
    db.add_all([rail_a, rail_b])
    db.flush()
    now = datetime.utcnow()

    def order(ticket, length, due_delta, status="ready"):
        o = WorkOrder(
            store_id=store.id,
            ticket_code=ticket,
            garment_name=f"衣物-{ticket}",
            length_cm=length,
            status=status,
            due_at=now + due_delta,
        )
        db.add(o)
        db.flush()
        return o

    orders = {
        "o1": order("T-1001", 45, timedelta(days=1)),
        "o2": order("T-1002", 35, timedelta(days=1)),
        "o3": order("T-1003", 50, timedelta(hours=-1)),
        "o4": order("T-1004", 70, timedelta(days=1)),
        "o5": order("T-1005", 10, timedelta(days=1)),
        "o7": order("T-1007", 20, timedelta(hours=-2)),
    }
    db.commit()
    return {"store": store, "rail_a": rail_a, "rail_b": rail_b, "orders": orders}


def hang(client, order_id, rail_id=None):
    body = {"order_id": order_id}
    if rail_id is not None:
        body["rail_id"] = rail_id
    return client.post("/api/hang", json=body)


def occupancy(client, rail_id):
    resp = client.get(f"/api/occupancy/{rail_id}")
    assert resp.status_code == 200
    return resp.json()


class TestHang:
    def test_first_fit_leftmost_and_chaining(self, client, scene):
        rail_a = scene["rail_a"]
        o = scene["orders"]

        resp = hang(client, o["o1"].id)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "hung"
        assert body["hung_at"] is not None
        assert [(s["start_cm"], s["end_cm"]) for s in occupancy(client, rail_a.id)["segments"]] == [(0, 45)]

        assert hang(client, o["o2"].id).status_code == 200
        assert [(s["start_cm"], s["end_cm"]) for s in occupancy(client, rail_a.id)["segments"]] == [
            (0, 45),
            (45, 80),
        ]

        # A 杆仅剩 20cm，50cm 的 o3 落到 B 杆头部
        assert hang(client, o["o3"].id).status_code == 200
        assert [(s["start_cm"], s["end_cm"]) for s in occupancy(client, scene["rail_b"].id)["segments"]] == [(0, 50)]

        # 10cm 的 o5 仍能塞进 A 杆剩余空隙
        assert hang(client, o["o5"].id).status_code == 200
        assert [(s["start_cm"], s["end_cm"]) for s in occupancy(client, rail_a.id)["segments"]] == [
            (0, 45),
            (45, 80),
            (80, 90),
        ]

    def test_hang_explicit_rail(self, client, scene):
        rail_b = scene["rail_b"]
        o = scene["orders"]
        resp = hang(client, o["o5"].id, rail_id=rail_b.id)
        assert resp.status_code == 200
        assert [(s["start_cm"], s["end_cm"]) for s in occupancy(client, rail_b.id)["segments"]] == [(0, 10)]

    def test_hang_no_space_returns_409(self, client, scene):
        o = scene["orders"]
        assert hang(client, o["o1"].id).status_code == 200  # A: 0-45，余 55
        resp = hang(client, o["o4"].id)  # 70 > A 剩 55，> B 60
        assert resp.status_code == 409
        assert resp.json()["detail"] == "挂杆空间不足"

    def test_hang_explicit_rail_no_space_returns_409(self, client, scene):
        o = scene["orders"]
        resp = hang(client, o["o4"].id, rail_id=scene["rail_b"].id)  # 70 > 60
        assert resp.status_code == 409

    def test_hang_unknown_order_404(self, client, scene):
        resp = hang(client, 99999)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "工单不存在"

    def test_hang_unknown_rail_404(self, client, scene):
        resp = hang(client, scene["orders"]["o1"].id, rail_id=99999)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "无可用挂杆"

    def test_hang_rejects_non_ready_status(self, client, scene):
        o = scene["orders"]
        assert hang(client, o["o1"].id).status_code == 200
        # 已上杆不可重复上杆
        resp = hang(client, o["o1"].id)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "工单状态不可上杆"
        # 已取件不可上杆
        assert client.post("/api/pickup", json={"ticket_code": "T-1001"}).status_code == 200
        resp = hang(client, o["o1"].id)
        assert resp.status_code == 400


class TestPickup:
    def test_pickup_releases_segment(self, client, scene):
        rail_a = scene["rail_a"]
        o = scene["orders"]
        hang(client, o["o1"].id)
        hang(client, o["o2"].id)

        resp = client.post("/api/pickup", json={"ticket_code": "T-1001"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "picked"
        assert [(s["start_cm"], s["end_cm"]) for s in occupancy(client, rail_a.id)["segments"]] == [(45, 80)]

    def test_pickup_unknown_ticket_404(self, client, scene):
        resp = client.post("/api/pickup", json={"ticket_code": "NOPE"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "取件码无效"

    def test_pickup_not_hung_400(self, client, scene):
        o = scene["orders"]
        # ready 单不能取件
        resp = client.post("/api/pickup", json={"ticket_code": "T-1002"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "工单未在挂杆上"
        # 取件后不可重复取
        hang(client, o["o1"].id)
        assert client.post("/api/pickup", json={"ticket_code": "T-1001"}).status_code == 200
        resp = client.post("/api/pickup", json={"ticket_code": "T-1001"})
        assert resp.status_code == 400


class TestOverdue:
    def test_scan_marks_ready_and_hung_only(self, client, scene, db):
        o = scene["orders"]
        hang(client, o["o1"].id)  # hung，未到期
        hang(client, o["o3"].id)  # hung，已过期
        hang(client, o["o2"].id)
        client.post("/api/pickup", json={"ticket_code": "T-1002"})  # picked，未到期

        resp = client.post("/api/overdue/scan")
        assert resp.status_code == 200
        marked = {item["ticket_code"] for item in resp.json()}
        # o3(hung 过期)、o7(ready 过期) 被标记；o1 hung 未到期、o2 picked、o4/o5 ready 未到期 不动
        assert marked == {"T-1003", "T-1007"}

        db.expire_all()
        assert db.get(WorkOrder, o["o1"].id).status == "hung"
        assert db.get(WorkOrder, o["o2"].id).status == "picked"
        assert db.get(WorkOrder, o["o3"].id).status == "overdue"
        assert db.get(WorkOrder, o["o7"].id).status == "overdue"
        assert db.get(WorkOrder, o["o4"].id).status == "ready"

    def test_overdue_list_sorted_by_due_at(self, client, scene):
        client.post("/api/overdue/scan")
        resp = client.get("/api/overdue")
        assert resp.status_code == 200
        dues = [item["due_at"] for item in resp.json()]
        assert dues == sorted(dues)
        assert {item["ticket_code"] for item in resp.json()} == {"T-1003", "T-1007"}

    def test_overdue_order_can_hang_again(self, client, scene):
        o = scene["orders"]
        client.post("/api/overdue/scan")  # o7 -> overdue（从未上杆，无占位）
        resp = hang(client, o["o7"].id)
        assert resp.status_code == 200
        assert resp.json()["status"] == "hung"
        assert [(s["start_cm"], s["end_cm"]) for s in occupancy(client, scene["rail_a"].id)["segments"]] == [(0, 20)]


class TestOccupancy:
    def test_segments_sorted_and_join_order_fields(self, client, scene):
        rail_a = scene["rail_a"]
        o = scene["orders"]
        hang(client, o["o2"].id)  # A: 0-35
        hang(client, o["o1"].id)  # A: 35-80
        body = occupancy(client, rail_a.id)
        assert body["rail_id"] == rail_a.id
        assert body["label"] == "A 杆"
        assert body["length_cm"] == 100
        assert [(s["ticket_code"], s["start_cm"], s["end_cm"]) for s in body["segments"]] == [
            ("T-1002", 0, 35),
            ("T-1001", 35, 80),
        ]
        assert body["segments"][0]["garment_name"] == "衣物-T-1002"

    def test_unknown_rail_404(self, client, scene):
        resp = client.get("/api/occupancy/99999")
        assert resp.status_code == 404
