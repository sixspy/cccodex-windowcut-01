"""事件入账：窗内 / 旁路暂存 / 拒收 / 迟到桶分流。

封口进行中（封口中）的新到事件一律按封口分流册当天口径处置：
本仓择定「甲·旁路暂存」——只写 hold_staging（待放行），正式事件零增；
既有拒收路径在封口期内被册挡回，不得混用。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import (
    SealRoutingDeviation,
    connect,
    get_seal_routing,
    guard_seal_path,
    write_audit,
)
from locks import release, require_lock
from models import EventIn

router = APIRouter(tags=["ingest"])

SEALING = "封口中"
ROUTING_HOLD = "hold_staging"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _route_sealing_event(conn, user: str, w, body: EventIn) -> dict:
    """封口进行中新到：只落旁路暂存；失败则暂存与封口态同事务回滚。"""
    routing = get_seal_routing(conn)  # 只读，绝不在写路径上改册
    try:
        guard_seal_path(
            conn,
            user,
            w["name"],
            routing,
            ROUTING_HOLD,
            "封口中事件按甲路径落旁路暂存",
        )
    except SealRoutingDeviation as exc:
        conn.commit()  # 封口偏了 留痕后拒写
        raise HTTPException(409, "偏离封口分流册：封口中事件不得进旁路暂存") from exc

    ledger = conn.execute(
        """SELECT * FROM seal_ledger WHERE window_id=? AND status=?
           ORDER BY id DESC LIMIT 1""",
        (w["id"], SEALING),
    ).fetchone()
    if ledger is None:
        raise HTTPException(409, "封口中窗口缺少封印台账，拒写")

    now = _now()
    try:
        cur = conn.execute(
            """INSERT INTO hold_staging(window_id,biz_key,event_ts,amount,status,created_at)
               VALUES (?,?,?,?,?,?)""",
            (w["id"], body.biz_key, body.event_ts, body.amount, "待放行", now),
        )
        hold_id = cur.lastrowid
        # 封口态与暂存同事务：计数加一，失败时一并回滚，不留半成品
        conn.execute(
            "UPDATE seal_ledger SET held_count=held_count+1 WHERE id=?",
            (ledger["id"],),
        )
        if body.fail_simulate:
            raise RuntimeError("模拟暂存失败（夹具用）")
        write_audit(
            conn,
            user,
            "ingest_hold_staging",
            body.biz_key,
            {
                "window_id": w["id"],
                "hold_id": hold_id,
                "seal_token": ledger["seal_token"],
                "routing": routing["path"],
            },
        )
        conn.commit()
        return {
            "disposition": "旁路暂存",
            "hold_id": hold_id,
            "window_id": w["id"],
            "status": "待放行",
            "seal_token": ledger["seal_token"],
        }
    except RuntimeError as exc:
        conn.rollback()
        write_audit(
            conn,
            user,
            "ingest_hold_rollback",
            body.biz_key,
            {
                "window_id": w["id"],
                "seal_token": ledger["seal_token"],
                "reason": str(exc),
                "rolled_back": ["hold_staging", "seal_ledger"],
            },
        )
        conn.commit()
        raise HTTPException(500, "封口期暂存失败：暂存与封口态已同事务回滚") from exc


@router.post("/api/events")
def ingest_event(body: EventIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        head = conn.execute(
            "SELECT * FROM windows WHERE name=?", (body.window_name,)
        ).fetchone()
        if not head:
            raise HTTPException(404, "窗口不存在")
        # 与关窗/旁路暂存共用同一把窗锁：后到只记争用
        lock_key = f"window:{head['id']}"
        require_lock(conn, lock_key, user)
        try:
            w = conn.execute(
                "SELECT * FROM windows WHERE id=?", (head["id"],)
            ).fetchone()
            if not w:
                raise HTTPException(404, "窗口不存在")

            if w["status"] == SEALING:
                return _route_sealing_event(conn, user, w, body)

            ts = body.event_ts
            created = _now()
            if w["status"] == "已关闭":
                cur = conn.execute(
                    """INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (None, body.biz_key, ts, body.amount, "迟到桶", created),
                )
                eid = cur.lastrowid
                conn.execute(
                    """INSERT INTO late_bucket(event_id,window_name,biz_key,event_ts,amount,reason,created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        eid,
                        body.window_name,
                        body.biz_key,
                        ts,
                        body.amount,
                        "关窗后到达",
                        created,
                    ),
                )
                write_audit(
                    conn,
                    user,
                    "ingest_late",
                    body.biz_key,
                    {"window": body.window_name},
                )
                conn.commit()
                return {"disposition": "迟到桶", "event_id": eid}

            if not (w["open_at"] <= ts <= w["close_at"]):
                conn.execute(
                    """INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (None, body.biz_key, ts, body.amount, "拒收", created),
                )
                conn.execute(
                    """INSERT INTO reject_slips(window_name,biz_key,event_ts,amount,reason,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        body.window_name,
                        body.biz_key,
                        ts,
                        body.amount,
                        "窗外且窗仍开启",
                        created,
                    ),
                )
                write_audit(
                    conn,
                    user,
                    "ingest_reject",
                    body.biz_key,
                    {"window": body.window_name},
                )
                conn.commit()
                raise HTTPException(409, "事件时间落在窗外且窗仍开启：拒收")

            cur = conn.execute(
                """INSERT INTO events(window_id,biz_key,event_ts,amount,disposition,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (w["id"], body.biz_key, ts, body.amount, "窗内", created),
            )
            write_audit(
                conn,
                user,
                "ingest_in_window",
                body.biz_key,
                {"window_id": w["id"]},
            )
            conn.commit()
            return {"disposition": "窗内", "window_id": w["id"], "event_id": cur.lastrowid}
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.get("/api/events")
def list_events(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/rejects")
def list_rejects(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reject_slips ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}
