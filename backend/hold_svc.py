"""旁路暂存：封口进行中新到事件的唯一落点（封口分流册甲路径）。

约定：
- 只有「封口中」窗口的旁路暂存才受理；开启/已关闭窗口走入账常规分流。
- 当天册定口径非旁路暂存时，写暂存一律拒写并记「封口偏了」。
- 暂存不触碰正式事件；放行是另一笔业务，母仓仅登记，正式窗内零改。
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
from models import HoldIn

router = APIRouter(tags=["hold"])

SEALING = "封口中"
SEAL_PATH = "hold_staging"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@router.get("/api/holds")
def list_holds(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT h.*, w.name AS window_name, w.status AS window_status
               FROM hold_staging h
               LEFT JOIN windows w ON w.id=h.window_id
               ORDER BY h.id DESC LIMIT 200"""
        ).fetchall()
        pending = conn.execute(
            """SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM hold_staging
               WHERE status='待放行'"""
        ).fetchone()
        return {
            "items": [dict(r) for r in rows],
            "pending_count": pending["c"],
            "pending_amount": pending["s"],
        }


@router.post("/api/holds")
def create_hold(body: HoldIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        head = conn.execute(
            "SELECT * FROM windows WHERE name=?", (body.window_name,)
        ).fetchone()
        if not head:
            raise HTTPException(404, "窗口不存在")
        # 与关窗/入账共用同一把窗锁：后到只记争用
        lock_key = f"window:{head['id']}"
        require_lock(conn, lock_key, user)
        try:
            w = conn.execute(
                "SELECT * FROM windows WHERE id=?", (head["id"],)
            ).fetchone()
            if not w:
                raise HTTPException(404, "窗口不存在")
            if w["status"] != SEALING:
                raise HTTPException(
                    409, "旁路暂存只受理封口进行中的窗口；其余请走入账分流"
                )
            routing = get_seal_routing(conn)  # 只读，写路径不改册
            try:
                guard_seal_path(
                    conn, user, w["name"], routing, SEAL_PATH, "封口中手工补录暂存"
                )
            except SealRoutingDeviation as exc:
                conn.commit()  # 封口偏了 留痕后拒写
                raise HTTPException(409, "偏离封口分流册：拒写并记封口偏了") from exc

            ledger = conn.execute(
                """SELECT * FROM seal_ledger WHERE window_id=? AND status=?
                   ORDER BY id DESC LIMIT 1""",
                (w["id"], SEALING),
            ).fetchone()
            if ledger is None:
                raise HTTPException(409, "封口中窗口缺少封印台账，拒写")

            now = _now()
            cur = conn.execute(
                """INSERT INTO hold_staging(window_id,biz_key,event_ts,amount,status,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (w["id"], body.biz_key, body.event_ts, body.amount, "待放行", now),
            )
            write_audit(
                conn,
                user,
                "hold_create",
                body.biz_key,
                {
                    "window_id": w["id"],
                    "hold_id": cur.lastrowid,
                    "seal_token": ledger["seal_token"],
                },
            )
            conn.commit()
            return {
                "ok": True,
                "hold_id": cur.lastrowid,
                "status": "待放行",
                "seal_token": ledger["seal_token"],
            }
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.post("/api/holds/{hold_id}/release-stub")
def release_hold_stub(hold_id: int, authorization: AuthHeader = None):
    """空桩：放行不在此落地，正式事件零改；仅登记放行意图与册定口径。"""
    user = require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM hold_staging WHERE id=?", (hold_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "暂存不存在")
        if row["status"] != "待放行":
            raise HTTPException(409, f"暂存状态 {row['status']} 不可放行")
        w = conn.execute(
            "SELECT * FROM windows WHERE id=?", (row["window_id"],)
        ).fetchone()
        if w is not None and w["status"] == SEALING:
            raise HTTPException(409, "封口未结束，放行不得触碰正式窗内")
        routing = get_seal_routing(conn)
        write_audit(
            conn,
            user,
            "hold_release_stub",
            str(hold_id),
            {
                "window_id": row["window_id"],
                "routing": routing["path"],
                "note": "空桩：正式事件零改，放行写路径待题面",
            },
        )
        conn.commit()
        return {"ok": True, "stub": True, "status": row["status"]}
