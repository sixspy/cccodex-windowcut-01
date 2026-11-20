"""审计与互证只读旁路。"""
from __future__ import annotations

from fastapi import APIRouter

from auth import AuthHeader, require_user
from db import connect, ensure_seal_routing

router = APIRouter(tags=["audit"])


@router.get("/api/audit")
def list_audit(authorization: AuthHeader = None, limit: int = 100):
    require_user(authorization)
    limit = max(1, min(limit, 500))
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_trail ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/audit/actions")
def audit_actions(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT action, COUNT(*) c FROM audit_trail
               GROUP BY action ORDER BY c DESC"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/dashboard")
def dashboard(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        open_n = conn.execute(
            "SELECT COUNT(*) c FROM windows WHERE status='开启'"
        ).fetchone()["c"]
        sealing_n = conn.execute(
            "SELECT COUNT(*) c FROM windows WHERE status='封口中'"
        ).fetchone()["c"]
        closed_n = conn.execute(
            "SELECT COUNT(*) c FROM windows WHERE status='已关闭'"
        ).fetchone()["c"]
        late_n = conn.execute("SELECT COUNT(*) c FROM late_bucket").fetchone()["c"]
        reject_n = conn.execute("SELECT COUNT(*) c FROM reject_slips").fetchone()["c"]
        hold_pending = conn.execute(
            "SELECT COUNT(*) c FROM hold_staging WHERE status='待放行'"
        ).fetchone()["c"]
        hold_amount = conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM hold_staging WHERE status='待放行'"
        ).fetchone()["s"]
        mats = conn.execute("SELECT COUNT(*) c FROM materializations").fetchone()["c"]
        contention = conn.execute("SELECT COUNT(*) c FROM contention_log").fetchone()["c"]
        deviations = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='封口偏了'"
        ).fetchone()["c"]
        routing = ensure_seal_routing(conn)
        conn.commit()
        return {
            "open_windows": open_n,
            "sealing_windows": sealing_n,
            "closed_windows": closed_n,
            "late_bucket": late_n,
            "rejects": reject_n,
            "holds": hold_pending,
            "hold_amount": hold_amount,
            "materializations": mats,
            "contention": contention,
            "seal_deviations": deviations,
            "seal_routing": {"biz_date": routing["biz_date"], "path": routing["path"]},
        }
