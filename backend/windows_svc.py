"""业务窗开/关/列表与封口分流册。

关窗采用两段式：先置「封口中」并落封印台账（提交，让并发方看得见），
再收口为「已关闭」。封口进行中新到事件的去向由封口分流册当天口径决定，
业务模块只读册、不改册。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user
from db import (
    SEAL_ROUTING_PATHS,
    connect,
    ensure_seal_routing,
    get_seal_routing,
    note_deviation,
    write_audit,
)
from locks import release, require_lock
from models import WindowCreateIn

router = APIRouter(tags=["windows"])

SEALING = "封口中"
CLOSED = "已关闭"
OPEN = "开启"


class SealRoutingIn(BaseModel):
    path: str
    note: str = ""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def seal_ledger_row(conn, window_id: int):
    return conn.execute(
        """SELECT * FROM seal_ledger WHERE window_id=?
           ORDER BY id DESC LIMIT 1""",
        (window_id,),
    ).fetchone()




@router.get("/api/windows")
def list_windows(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute("SELECT * FROM windows ORDER BY id").fetchall()
        items = []
        for r in rows:
            d = dict(r)
            led = seal_ledger_row(conn, r["id"])
            d["seal_ledger"] = dict(led) if led else None
            items.append(d)
        return {"items": items}


@router.post("/api/windows")
def create_window(body: WindowCreateIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        try:
            conn.execute(
                "INSERT INTO windows(name,open_at,close_at,status,generation) VALUES (?,?,?,?,1)",
                (body.name, body.open_at, body.close_at, OPEN),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(409, f"窗口创建失败: {exc}") from exc
        wid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        write_audit(conn, user, "window_create", body.name, {"id": wid})
        conn.commit()
        row = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
        return dict(row)


def _begin_seal(conn, user: str, w) -> str:
    """封口开始：封口中 + 封印台账 + 快照已在场正式事件数。"""
    now = _now()
    token = f"seal-{w['id']}-{w['generation']}-{now}"
    snapshot = conn.execute(
        """SELECT COUNT(*) c FROM events
           WHERE window_id=? AND disposition='窗内'""",
        (w["id"],),
    ).fetchone()["c"]
    conn.execute(
        "UPDATE windows SET status=?, seal_token=? WHERE id=?",
        (SEALING, token, w["id"]),
    )
    conn.execute(
        """INSERT INTO seal_ledger(window_id,seal_token,status,snapshot_count,held_count,started_at,actor)
           VALUES (?,?,?,?,0,?,?)""",
        (w["id"], token, SEALING, snapshot, now, user),
    )
    ledger = get_seal_routing(conn)  # 册定口径只读，不改
    write_audit(
        conn,
        user,
        "window_seal_start",
        str(w["id"]),
        {
            "seal_token": token,
            "snapshot_count": snapshot,
            "routing": ledger["path"] if ledger else None,
        },
    )
    conn.commit()
    return token


def _finish_seal(conn, user: str, w) -> dict:
    """封口收口：已关闭 + 台账完成，旁路暂存保持待放行，不进正式合计。"""
    now = _now()
    ledger = seal_ledger_row(conn, w["id"])
    held = conn.execute(
        """SELECT COUNT(*) c FROM hold_staging
           WHERE window_id=? AND status='待放行'""",
        (w["id"],),
    ).fetchone()["c"]
    conn.execute("UPDATE windows SET status=? WHERE id=?", (CLOSED, w["id"]))
    if ledger is not None:
        conn.execute(
            """UPDATE seal_ledger SET status='已封口', held_count=?, finished_at=?
               WHERE id=?""",
            (held, now, ledger["id"]),
        )
    write_audit(
        conn,
        user,
        "window_seal_finish",
        str(w["id"]),
        {"seal_token": w["seal_token"], "held_pending": held},
    )
    conn.commit()
    return {"ok": True, "seal_token": w["seal_token"], "held_pending": held}


@router.post("/api/windows/{wid}/close")
def close_window(wid: int, authorization: AuthHeader = None):
    user = require_user(authorization)
    lock_key = f"window:{wid}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            w = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
            if not w:
                raise HTTPException(404, "窗口不存在")
            if w["status"] == CLOSED:
                return {"ok": True, "already": True}
            if w["status"] == SEALING:
                # 续跑未完成的收口：新到事件已被旁路暂存截住，不会补进正式合计
                return _finish_seal(conn, user, w)
            if w["status"] != OPEN:
                raise HTTPException(409, f"窗口状态 {w['status']} 不可关窗")
            token = _begin_seal(conn, user, w)
            w = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
            result = _finish_seal(conn, user, w)
            result["seal_token"] = token
            return result
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.get("/api/windows/{wid}")
def get_window(wid: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        w = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
        if not w:
            raise HTTPException(404, "窗口不存在")
        ev = conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM events WHERE window_id=? AND disposition='窗内'",
            (wid,),
        ).fetchone()
        holds = conn.execute(
            """SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM hold_staging
               WHERE window_id=? AND status='待放行'""",
            (wid,),
        ).fetchone()
        led = seal_ledger_row(conn, wid)
        return {
            "window": dict(w),
            "seal_ledger": dict(led) if led else None,
            "in_window_count": ev["c"],
            "in_window_amount": ev["s"],
            "hold_pending_count": holds["c"],
            "hold_pending_amount": holds["s"],
        }


@router.get("/api/seal-routing")
def read_seal_routing(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = ensure_seal_routing(conn)
        conn.commit()
        return {
            "biz_date": row["biz_date"],
            "path": row["path"],
            "path_label": row["path_label"],
            "chosen_by": row["chosen_by"],
            "created_at": row["created_at"],
            "allowed": SEAL_ROUTING_PATHS,
        }


@router.post("/api/seal-routing")
def set_seal_routing(body: SealRoutingIn, authorization: AuthHeader = None):
    """当天口径仅可落一次；已有不同口径即拒写并记封口偏了。"""
    user = require_user(authorization)
    if body.path not in SEAL_ROUTING_PATHS:
        raise HTTPException(400, "path 须为 hold_staging 或 reject_slips")
    with connect() as conn:
        row = ensure_seal_routing(conn, user)
        conn.commit()
        if row["path"] != body.path:
            note_deviation(
                conn,
                user,
                row["biz_date"],
                row,
                body.path,
                "当天口径已定，禁止改口",
            )
            conn.commit()  # 拒写也要留痕，先提交审计再抛
            raise HTTPException(409, "当天封口分流册已定，禁止改口")
        if body.note and body.note != row["note"]:
            write_audit(
                conn,
                user,
                "seal_routing_note",
                row["biz_date"],
                {"path": row["path"], "note": body.note},
            )
        conn.commit()
        return {
            "biz_date": row["biz_date"],
            "path": row["path"],
            "path_label": row["path_label"],
        }


@router.get("/api/seal-ledger")
def list_seal_ledger(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.*, w.name AS window_name FROM seal_ledger s
               JOIN windows w ON w.id=s.window_id ORDER BY s.id DESC LIMIT 100"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}
