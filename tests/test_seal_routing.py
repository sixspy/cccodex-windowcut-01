"""封口分流红测：关窗进行中的新事件泄漏 + 双人抢关同窗争用。

本文件先于业务实现落地。首版两条用例必须见红：
1. 窗口处于「封口中」时，新到事件不得再进正式窗内合计；
2. 两个账号同时抢关同一窗口，后到者只记争用、不得写业务。
"""
from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from app import app, init_db
from db import connect, ensure_seal_routing, write_audit


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "seal.db")
    init_db()
    yield


def _auth(c: TestClient, username: str = "admin", password: str = "123456") -> dict:
    tok = c.post(
        "/api/login", json={"username": username, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _mark_sealing(window_id: int = 2) -> None:
    """夹具：把窗口置于「关窗封口进行中」，模拟关闭事务已开始但未结束。

    与真实两段式关窗同态：窗口态=封口中、封印台账存在、正式事件快照在案。
    """
    with connect() as conn:
        token = f"seal-fixture-{window_id}-g1"
        conn.execute(
            "UPDATE windows SET status='封口中', seal_token=? WHERE id=?",
            (token, window_id),
        )
        snapshot = conn.execute(
            """SELECT COUNT(*) c FROM events
               WHERE window_id=? AND disposition='窗内'""",
            (window_id,),
        ).fetchone()["c"]
        conn.execute(
            """INSERT INTO seal_ledger(window_id,seal_token,status,snapshot_count,held_count,started_at,actor)
               VALUES (?,?,?,?,0,?,?)""",
            (window_id, token, "封口中", snapshot, "2026-11-02T23:59:01", "fixture"),
        )
        write_audit(
            conn,
            "fixture",
            "window_seal_start",
            str(window_id),
            {"seal_token": token, "snapshot_count": snapshot},
            created_at="2026-11-02T23:59:01",
        )
        conn.commit()


def test_seal_leak_event_must_not_enter_official_window_totals():
    """红测 1：封口进行中新到事件仍直写正式窗内合计 → 必须见红。

    口径：只允许落 hold_staging（旁路暂存），正式窗内事件零增；
    也不得落 reject_slips（当天口径只能择一，不得混用）。
    """
    c = TestClient(app)
    h = _auth(c)
    _mark_sealing(2)

    r = c.post(
        "/api/events",
        headers=h,
        json={
            "window_name": "W-1102",
            "biz_key": "SEAL-LEAK-1",
            "event_ts": "2026-11-02T12:00:00",
            "amount": 8.8,
        },
    )
    assert r.status_code == 200
    assert r.json()["disposition"] == "旁路暂存"

    with connect() as conn:
        official = conn.execute(
            """SELECT COUNT(*) c FROM events
               WHERE window_id=2 AND disposition='窗内'"""
        ).fetchone()["c"]
        holds = conn.execute(
            """SELECT COUNT(*) c FROM hold_staging
               WHERE window_id=2 AND biz_key='SEAL-LEAK-1' AND status='待放行'"""
        ).fetchone()["c"]
        rejects = conn.execute(
            "SELECT COUNT(*) c FROM reject_slips WHERE biz_key='SEAL-LEAK-1'"
        ).fetchone()["c"]

    assert official == 0, "封口进行中的新事件不得进入正式窗内合计"
    assert holds == 1, "封口进行中的新事件必须可在旁路暂存查到"
    assert rejects == 0, "选定旁路暂存后不得混用拒收路径"


def test_two_actors_closing_same_window_record_contention_only():
    """红测 2：双人同时抢关同一窗口，后到只记争用 → 必须见红。

    closer 先占 window:2 窗锁，admin 后到关窗：409、窗口不落封口、
    只留 contention_log/审计，不得写任何业务。
    """
    c = TestClient(app)
    admin = _auth(c, "admin", "123456")
    closer = _auth(c, "closer", "close123")

    assert (
        c.post(
            "/api/locks/acquire", headers=closer, json={"lock_key": "window:2"}
        ).status_code
        == 200
    )

    r = c.post("/api/windows/2/close", headers=admin)
    assert r.status_code == 409

    with connect() as conn:
        w = conn.execute("SELECT * FROM windows WHERE id=2").fetchone()
        contention = conn.execute(
            "SELECT COUNT(*) c FROM contention_log WHERE lock_key='window:2'"
        ).fetchone()["c"]
        audits = [
            x["action"]
            for x in conn.execute(
                "SELECT action FROM audit_trail ORDER BY id"
            ).fetchall()
        ]

    assert w["status"] == "开启", "后到争用者不得推进关窗/封口态"
    assert w["seal_token"] is None
    assert contention >= 1, "后到者必须留下争用记录"
    assert "lock_contention" in audits, "争用必须同时进入审计"
    assert "window_seal_start" not in audits, "争用者不得写封口业务"


def test_ingest_contends_for_window_lock_during_seal():
    """两路抢写同一窗口须占同一把窗锁：后到只记争用，不写暂存也不进正式合计。"""
    c = TestClient(app)
    admin = _auth(c, "admin", "123456")
    closer = _auth(c, "closer", "close123")
    _mark_sealing(2)

    assert (
        c.post(
            "/api/locks/acquire", headers=closer, json={"lock_key": "window:2"}
        ).status_code
        == 200
    )
    r = c.post(
        "/api/events",
        headers=admin,
        json={
            "window_name": "W-1102",
            "biz_key": "CONTEND-1",
            "event_ts": "2026-11-02T12:00:00",
            "amount": 2.0,
        },
    )
    assert r.status_code == 409
    assert "争用" in r.json()["detail"]

    with connect() as conn:
        holds = conn.execute(
            "SELECT COUNT(*) c FROM hold_staging WHERE biz_key='CONTEND-1'"
        ).fetchone()["c"]
        official = conn.execute(
            """SELECT COUNT(*) c FROM events
               WHERE window_id=2 AND disposition='窗内'"""
        ).fetchone()["c"]
        contention = conn.execute(
            "SELECT COUNT(*) c FROM contention_log WHERE lock_key='window:2'"
        ).fetchone()["c"]
        ledger = conn.execute(
            """SELECT * FROM seal_ledger WHERE window_id=2
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()

    assert holds == 0
    assert official == 0
    assert contention == 1
    assert ledger["held_count"] == 0


def test_materialize_blocked_while_sealing():
    """封口未结束不得物化：避免把封口中快照当定稿。"""
    c = TestClient(app)
    h = _auth(c)
    _mark_sealing(2)
    assert c.post("/api/windows/2/materialize", headers=h).status_code == 409


def test_seal_hold_failure_rolls_back_staging_with_seal_state():
    """封口期暂存失败：暂存与封口态同事务回滚，正式窗内与台账都不留半成品。"""
    c = TestClient(app)
    h = _auth(c)
    _mark_sealing(2)

    r = c.post(
        "/api/events",
        headers=h,
        json={
            "window_name": "W-1102",
            "biz_key": "SEAL-FAIL-1",
            "event_ts": "2026-11-02T12:00:00",
            "amount": 6.6,
            "fail_simulate": True,
        },
    )
    assert r.status_code == 500

    with connect() as conn:
        holds = conn.execute(
            "SELECT COUNT(*) c FROM hold_staging WHERE biz_key='SEAL-FAIL-1'"
        ).fetchone()["c"]
        official = conn.execute(
            """SELECT COUNT(*) c FROM events
               WHERE window_id=2 AND disposition='窗内'"""
        ).fetchone()["c"]
        ledger = conn.execute(
            """SELECT * FROM seal_ledger WHERE window_id=2
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        audit = conn.execute(
            """SELECT COUNT(*) c FROM audit_trail
               WHERE action='ingest_hold_rollback' AND target='SEAL-FAIL-1'"""
        ).fetchone()["c"]

    assert holds == 0, "失败时暂存不得残留"
    assert official == 0, "失败时正式窗内不得有半成品"
    assert ledger["status"] == "封口中", "封口态须被保留，不得被失败穿帮"
    assert ledger["held_count"] == 0, "暂存计数须同事务回滚"
    assert audit == 1, "回滚须留审计"


def test_seal_then_close_materialize_reconcile_five_views_agree():
    """封口→收口→物化→对账：暂存可查、正式窗内无半成品、五处同态。"""
    c = TestClient(app)
    h = _auth(c)
    _mark_sealing(2)

    r = c.post(
        "/api/events",
        headers=h,
        json={
            "window_name": "W-1102",
            "biz_key": "SEAL-HOLD-OK",
            "event_ts": "2026-11-02T12:00:00",
            "amount": 5.5,
        },
    )
    assert r.status_code == 200 and r.json()["disposition"] == "旁路暂存"
    hold_id = r.json()["hold_id"]

    assert c.post("/api/windows/2/close", headers=h).status_code == 200
    assert c.post("/api/windows/2/materialize", headers=h).status_code == 200

    recon = c.get("/api/reconcile/2", headers=h).json()
    holds = c.get("/api/holds", headers=h).json()
    ledger = c.get("/api/seal-ledger", headers=h).json()["items"]
    win = c.get("/api/windows/2", headers=h).json()
    mats = c.get("/api/materializations", headers=h).json()["items"]

    assert recon["window_events"] == 0
    assert recon["window_amount"] == 0
    assert recon["hold_pending"] == 1
    assert recon["hold_pending_amount"] == 5.5
    assert recon["rejects"] == 0
    assert recon["aligned"] is True
    assert recon["seal_ledger"]["status"] == "已封口"
    assert recon["seal_ledger"]["held_count"] == 1
    assert recon["seal_audits"] == 2
    assert any(x["id"] == hold_id and x["status"] == "待放行" for x in holds["items"])
    assert any(x["window_name"] == "W-1102" and x["status"] == "已封口" for x in ledger)
    assert win["window"]["status"] == "已关闭"
    assert win["in_window_count"] == 0 and win["hold_pending_count"] == 1
    assert any(
        m["window_name"] == "W-1102" and m["row_count"] == 0 for m in mats
    )


def test_seal_routing_ledger_cannot_change_same_day():
    """封口分流册当天只可一锤：改口须拒写并记封口偏了。"""
    c = TestClient(app)
    h = _auth(c)

    got = c.get("/api/seal-routing", headers=h).json()
    assert got["path"] == "hold_staging"
    assert got["path_label"] == "甲·旁路暂存"

    ok = c.post("/api/seal-routing", headers=h, json={"path": "hold_staging"})
    assert ok.status_code == 200

    bad = c.post("/api/seal-routing", headers=h, json={"path": "reject_slips"})
    assert bad.status_code == 409

    with connect() as conn:
        row = conn.execute("SELECT * FROM seal_routing_ledger").fetchone()
        deviations = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='封口偏了'"
        ).fetchone()["c"]
    assert row["path"] == "hold_staging"
    assert deviations == 1


def test_hold_write_rejected_when_routing_is_reject_path():
    """册定拒收口径时写暂存须拒写并记封口偏了（两路不得混用）。"""
    c = TestClient(app)
    h = _auth(c)
    _mark_sealing(2)
    with connect() as conn:
        ensure_seal_routing(conn, "fixture")
        conn.execute(
            "UPDATE seal_routing_ledger SET path='reject_slips', path_label='乙·开始瞬间拒收'"
        )
        conn.commit()

    r = c.post(
        "/api/holds",
        headers=h,
        json={
            "window_name": "W-1102",
            "biz_key": "MIX-1",
            "event_ts": "2026-11-02T12:00:00",
            "amount": 1,
        },
    )
    assert r.status_code == 409
    with connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM hold_staging WHERE biz_key='MIX-1'"
        ).fetchone()["c"]
        dev = conn.execute(
            """SELECT COUNT(*) c FROM audit_trail
               WHERE action='封口偏了' AND target='W-1102'"""
        ).fetchone()["c"]
    assert n == 0
    assert dev == 1
