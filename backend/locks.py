"""窗口锁与争用登记（公共并发面，题面争用句应对得上）。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user
from db import connect, write_audit

router = APIRouter(tags=["locks"])


class LockIn(BaseModel):
    lock_key: str
    action: str = "acquire"


def try_acquire(conn, lock_key: str, holder: str) -> bool:
    now = datetime.now().isoformat(timespec="seconds")
    # INSERT OR IGNORE 让 SQLite 主键唯一性兜住并发：后到者等前者提交后拿到 0 行
    cur = conn.execute(
        "INSERT OR IGNORE INTO window_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
        (lock_key, holder, now),
    )
    if cur.rowcount == 1:
        return True
    row = conn.execute(
        "SELECT holder FROM window_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    return bool(row) and row["holder"] == holder


def record_contention(conn, lock_key: str, holder: str, holder_before: str) -> None:
    """争用必须落库：先写 contention_log 与审计，再允许抛 409。"""
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, holder, "争用", f"持有者={holder_before}", now),
    )
    write_audit(
        conn,
        holder,
        "lock_contention",
        lock_key,
        {"holder_in_place": holder_before},
        created_at=now,
    )


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM window_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


def require_lock(conn, lock_key: str, holder: str) -> None:
    """占窗锁。约定：须在业务写之前调用，本函数会提交一次事务。

    占锁成功先落库，让并发方看得见；争用时只记争用与审计，不写业务。
    """
    if try_acquire(conn, lock_key, holder):
        conn.commit()
        return
    row = conn.execute(
        "SELECT holder FROM window_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    record_contention(conn, lock_key, holder, row["holder"] if row else "未知")
    conn.commit()
    raise HTTPException(409, "争用：后到只记争用，不写业务")


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM window_locks ORDER BY acquired_at DESC"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/contention")
def list_contention(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM contention_log ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/locks/acquire")
def acquire_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        ok = try_acquire(conn, body.lock_key, user)
        if not ok:
            row = conn.execute(
                "SELECT holder FROM window_locks WHERE lock_key=?", (body.lock_key,)
            ).fetchone()
            record_contention(
                conn, body.lock_key, user, row["holder"] if row else "未知"
            )
            conn.commit()
            raise HTTPException(409, "争用：后到只记争用")
        write_audit(conn, user, "lock_acquire", body.lock_key, {"ok": True})
        conn.commit()
        return {"ok": True, "lock_key": body.lock_key, "holder": user}



@router.post("/api/locks/release")
def release_lock(body: LockIn, authorization: AuthHeader = None):
    user = require_user(authorization)
    with connect() as conn:
        release(conn, body.lock_key, user)
        write_audit(conn, user, "lock_release", body.lock_key, {})
        conn.commit()
    return {"ok": True}
