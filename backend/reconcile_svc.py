"""窗内事件与物化结果互证：封口态/暂存/拒收/物化/审计五处同态。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["reconcile"])


def _window_state(conn, w) -> dict:
    """单一读路径：五处口径一次取齐，避免各页各读各的造成假绿。"""
    ev = conn.execute(
        """SELECT COUNT(*) c, COALESCE(SUM(amount),0) s
           FROM events WHERE window_id=? AND disposition='窗内'""",
        (w["id"],),
    ).fetchone()
    mat = conn.execute(
        "SELECT * FROM materializations WHERE window_id=? ORDER BY id DESC LIMIT 1",
        (w["id"],),
    ).fetchone()
    late = conn.execute(
        "SELECT COUNT(*) c FROM late_bucket WHERE window_name=?", (w["name"],)
    ).fetchone()["c"]
    rejects = conn.execute(
        "SELECT COUNT(*) c FROM reject_slips WHERE window_name=?", (w["name"],)
    ).fetchone()["c"]
    holds_pending = conn.execute(
        """SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM hold_staging
           WHERE window_id=? AND status='待放行'""",
        (w["id"],),
    ).fetchone()
    held_official = conn.execute(
        """SELECT COUNT(*) c FROM hold_staging
           WHERE window_id=? AND status IN ('已放行','已作废')""",
        (w["id"],),
    ).fetchone()["c"]
    seal = conn.execute(
        """SELECT * FROM seal_ledger WHERE window_id=?
           ORDER BY id DESC LIMIT 1""",
        (w["id"],),
    ).fetchone()
    seal_audits = conn.execute(
        """SELECT COUNT(*) c FROM audit_trail
           WHERE target=? AND action IN
             ('window_seal_start','window_seal_finish')""",
        (str(w["id"]),),
    ).fetchone()["c"]
    deviations = conn.execute(
        """SELECT COUNT(*) c FROM audit_trail
           WHERE action='封口偏了' AND target=?""",
        (w["name"],),
    ).fetchone()["c"]

    aligned = (
        bool(mat)
        and mat["row_count"] == ev["c"]
        and abs(mat["total_amount"] - ev["s"]) < 1e-6
    )
    return {
        "window": dict(w),
        "window_events": ev["c"],
        "window_amount": ev["s"],
        "materialized": dict(mat) if mat else None,
        "late_bucket": late,
        "rejects": rejects,
        "hold_pending": holds_pending["c"],
        "hold_pending_amount": holds_pending["s"],
        "hold_settled": held_official,
        "seal_ledger": dict(seal) if seal else None,
        "seal_audits": seal_audits,
        "seal_deviations": deviations,
        "aligned": aligned,
    }


@router.get("/api/reconcile/{wid}")
def reconcile(wid: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        w = conn.execute("SELECT * FROM windows WHERE id=?", (wid,)).fetchone()
        if not w:
            raise HTTPException(404, "窗口不存在")
        return _window_state(conn, w)


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        windows = conn.execute("SELECT * FROM windows ORDER BY id").fetchall()
        items = []
        for w in windows:
            st = _window_state(conn, w)
            items.append(
                {
                    "window_id": w["id"],
                    "name": w["name"],
                    "status": w["status"],
                    "aligned": st["aligned"],
                    "window_events": st["window_events"],
                    "late_bucket": st["late_bucket"],
                    "rejects": st["rejects"],
                    "hold_pending": st["hold_pending"],
                    "seal_status": (st["seal_ledger"] or {}).get("status"),
                    "seal_deviations": st["seal_deviations"],
                }
            )
        return {"items": items}
