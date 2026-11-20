"""空桩与旁路面冒烟：证明模块文件存在且接口可登记。"""
from fastapi.testclient import TestClient

from app import app, init_db
import pytest


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "stub.db")
    init_db()
    yield


def _auth(c):
    tok = c.post(
        "/api/login", json={"username": "admin", "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def test_hold_and_reopen_stubs():
    c = TestClient(app)
    h = _auth(c)
    # 旁路暂存只受理封口进行中的窗口：先把 W-1102 置于封口中（与真实两段式同态）
    from db import connect

    with connect() as conn:
        token = "seal-fixture-2-g1"
        conn.execute(
            "UPDATE windows SET status='封口中', seal_token=? WHERE id=2", (token,)
        )
        conn.execute(
            """INSERT INTO seal_ledger(window_id,seal_token,status,snapshot_count,held_count,started_at,actor)
               VALUES (2,?,?,0,0,'2026-11-02T23:59:01','fixture')""",
            (token, "封口中"),
        )
        conn.commit()

    r = c.post(
        "/api/holds",
        headers=h,
        json={
            "window_name": "W-1102",
            "biz_key": "H1",
            "event_ts": "2026-11-02T15:00:00",
            "amount": 1.5,
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "待放行"
    hid = r.json()["hold_id"]
    # 封口未结束不得放行
    assert c.post(f"/api/holds/{hid}/release-stub", headers=h).status_code == 409
    c.post("/api/windows/2/close", headers=h)  # 续跑收口
    assert c.post(f"/api/holds/{hid}/release-stub", headers=h).json()["stub"] is True

    stub = c.post(
        "/api/windows/2/reopen-stub", headers=h, json={"mode": "增量补丁"}
    ).json()
    assert stub["stub"] is True
    assert stub["mode"] == "增量补丁"
    assert c.get("/api/reopen-ledger", headers=h).json()["items"]


def test_hold_only_for_sealing_window():
    """开启窗/已关闭窗不得直接写暂存，必须走入账常规分流。"""
    c = TestClient(app)
    h = _auth(c)
    r = c.post(
        "/api/holds",
        headers=h,
        json={
            "window_name": "W-1102",
            "biz_key": "H-OPEN",
            "event_ts": "2026-11-02T15:00:00",
            "amount": 1.5,
        },
    )
    assert r.status_code == 409


def test_reject_outside_open_window():
    c = TestClient(app)
    h = _auth(c)
    r = c.post(
        "/api/events",
        headers=h,
        json={
            "window_name": "W-1102",
            "biz_key": "OUT",
            "event_ts": "2026-10-01T00:00:00",
            "amount": 9,
        },
    )
    assert r.status_code == 409
    rejects = c.get("/api/rejects", headers=h).json()["items"]
    assert any(x["biz_key"] == "OUT" for x in rejects)
