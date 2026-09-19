"""Paid research tasks — the Arena's "create an agent" lane (x402 on Celo).

The buyer configures what they want watched (market, assignment, how long, how
often), pays ONCE for the whole run, and a background runner delivers one
report per check. No login, no exchange keys, no orders: a task only ever
reads the market and writes text.

Deliberately self-contained:
  * it never imports the trading engine, the points ledger or `agents` —
    a running task cannot touch a live agent, and vice versa;
  * every check is one `chat_service._run_chat` call (the same analyst that
    already answers the paid /chat and /brief products);
  * all state lives in two additive tables, so a restart just resumes.

Pricing is a pure function of the request body (`unit × checks`), which is why
no quote has to be stored: the 402 challenge, the signature check and the
execution all recompute the same number from the same fields.

OFF unless X402_TASKS_ENABLED=1.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .. import db
from ..models import config_model
from ..services import oplog
from . import x402

# ------------------------------------------------------------- the offer --
# Kept in lockstep with the Arena form: changing a choice here means changing
# the radio buttons in web/arena.html too.
TASK_TYPES = ("monitor", "research")
PRODUCT_OF = {"monitor": "task_monitor", "research": "task_research"}
HOURS_CHOICES = (1, 6, 24)
FREQ_CHOICES = (15, 60)
MAX_CHECKS = 96                       # 24 h × every 15 min — the biggest run we sell
MAX_TOTAL_USD = 10.0                  # hard ceiling per order (x402 price bound)
GOAL_MAX = 600
NAME_MAX = 60

# Guards. A task is cheap to us (one DeepSeek call per check ≈ $0.0006) but the
# analyst key is shared with the paid chat and with every live agent on this
# box, so the runner stays deliberately small.
MAX_RUNNING_PER_PAYER = int(os.environ.get("X402_TASKS_PER_WALLET", "3") or 3)
RUN_BATCH = int(os.environ.get("X402_TASKS_BATCH", "2") or 2)
DAILY_CHECK_BUDGET = int(os.environ.get("X402_TASKS_DAILY_CHECKS", "600") or 600)
RETRY_DELAY_S = 120                   # a failed check retries once, soon
MAX_ATTEMPTS_PER_CHECK = 2
MAX_CONSECUTIVE_FAILS = 3             # then the task stops and says so
GRACE_S = 900                         # a run may finish up to 15 min past its window

RECOVER_TTL_S = 600                   # the recovery signature must be fresh
_SYMBOL_RE = re.compile(r"[A-Za-z0-9:._\-]{1,24}")
_ADDR_RE = re.compile(r"0x[0-9a-fA-F]{40}")

_schema_ready = False


class Invalid(ValueError):
    """Bad task configuration — the message is safe to show a buyer."""


# ----------------------------------------------------------------- switch --

def enabled() -> bool:
    return (os.environ.get("X402_TASKS_ENABLED", "0").strip().lower() in ("1", "true", "on", "yes")
            and x402.enabled())


# ---------------------------------------------------------------- pricing --

def unit_usd(task_type: str) -> float:
    """Per-check price, from the same admin/env-overridable table as the other
    products (bounded to [0.001, 10] there)."""
    return x402.price_usd(PRODUCT_OF[task_type])


def checks_for(hours: int, frequency_min: int) -> int:
    return int(hours * 60 / frequency_min)


def parse(body: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize + validate a task configuration. Raises Invalid with a
    buyer-safe message; never trusts a client-supplied price."""
    if not isinstance(body, dict):
        raise Invalid("invalid request body")
    task_type = str(body.get("task") or "").strip().lower()
    if task_type not in TASK_TYPES:
        raise Invalid("task must be 'monitor' or 'research'")
    symbol = str(body.get("symbol") or "").strip().upper()
    if not symbol or not _SYMBOL_RE.fullmatch(symbol):
        raise Invalid("invalid symbol")
    try:
        hours = int(body.get("hours"))
        frequency = int(body.get("frequency"))
    except (TypeError, ValueError):
        raise Invalid("hours and frequency must be numbers")
    if hours not in HOURS_CHOICES:
        raise Invalid(f"hours must be one of {list(HOURS_CHOICES)}")
    if frequency not in FREQ_CHOICES:
        raise Invalid(f"frequency must be one of {list(FREQ_CHOICES)} minutes")
    goal = str(body.get("goal") or "").strip()[:GOAL_MAX]
    if not goal:
        raise Invalid("goal is required — describe what to watch and what you need")
    name = str(body.get("name") or "").strip()[:NAME_MAX]
    checks = checks_for(hours, frequency)
    if checks < 1 or checks > MAX_CHECKS:
        raise Invalid(f"a run is between 1 and {MAX_CHECKS} checks")
    return {"task": task_type, "symbol": symbol, "hours": hours, "frequency": frequency,
            "goal": goal, "name": name, "checks": checks}


def quote(v: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic price for a parsed configuration."""
    unit = unit_usd(v["task"])
    total = round(unit * v["checks"], 6)
    if total > MAX_TOTAL_USD:
        raise Invalid(f"this run would cost more than the ${MAX_TOTAL_USD:.0f} per-order limit")
    return {**v, "product": PRODUCT_OF[v["task"]], "unit_usd": unit, "total_usd": total,
            "asset": "USDC", "checks": v["checks"],
            "window_s": v["hours"] * 3600, "interval_s": v["frequency"] * 60}


def quote_from_body(body: Dict[str, Any]) -> Dict[str, Any]:
    return quote(parse(body))


# ------------------------------------------------------------------ store --

def ensure_schema() -> None:
    """Additive, self-healing — safe to call any time (constitution §3)."""
    global _schema_ready
    if _schema_ready:
        return
    db.execute("""CREATE TABLE IF NOT EXISTS x402_tasks (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id       TEXT NOT NULL,
        payer         TEXT NOT NULL,
        nonce         TEXT NOT NULL,
        payment_id    INTEGER NOT NULL DEFAULT 0,
        tx            TEXT NOT NULL DEFAULT '',
        product       TEXT NOT NULL DEFAULT '',
        task_type     TEXT NOT NULL DEFAULT 'monitor',
        symbol        TEXT NOT NULL DEFAULT '',
        hours         INTEGER NOT NULL DEFAULT 0,
        frequency     INTEGER NOT NULL DEFAULT 0,
        checks_total  INTEGER NOT NULL DEFAULT 0,
        checks_done   INTEGER NOT NULL DEFAULT 0,
        checks_failed INTEGER NOT NULL DEFAULT 0,
        amount_usd    REAL NOT NULL DEFAULT 0,
        name          TEXT NOT NULL DEFAULT '',
        goal          TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL DEFAULT 'awaiting_payment',
        token_hash    TEXT NOT NULL DEFAULT '',
        created_at    REAL NOT NULL DEFAULT 0,
        started_at    REAL NOT NULL DEFAULT 0,
        next_run_at   REAL NOT NULL DEFAULT 0,
        ended_at      REAL NOT NULL DEFAULT 0,
        meta_json     TEXT NOT NULL DEFAULT ''
    )""")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uidx_x402_tasks_id ON x402_tasks(task_id)")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS uidx_x402_tasks_pay ON x402_tasks(payer, nonce)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_x402_tasks_due ON x402_tasks(status, next_run_at)")
    db.execute("""CREATE TABLE IF NOT EXISTS x402_task_runs (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id   TEXT NOT NULL,
        seq       INTEGER NOT NULL,
        ts        REAL NOT NULL,
        ok        INTEGER NOT NULL DEFAULT 0,
        headline  TEXT NOT NULL DEFAULT '',
        body_json TEXT NOT NULL DEFAULT '',
        error     TEXT NOT NULL DEFAULT ''
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_x402_task_runs ON x402_task_runs(task_id, seq)")
    # Additive columns for tables that already exist (constitution §3: additive
    # and self-healing). A column another process added between the check and
    # the ALTER is benign.
    existing = {r["name"] for r in db.query_all("PRAGMA table_info(x402_tasks)")}
    for name, decl in (("shared", "INTEGER NOT NULL DEFAULT 0"),
                       ("share_id", "TEXT NOT NULL DEFAULT ''"),
                       ("shared_at", "REAL NOT NULL DEFAULT 0")):
        if name not in existing:
            try:
                db.execute(f"ALTER TABLE x402_tasks ADD COLUMN {name} {decl}")
            except Exception:
                pass
    db.execute("CREATE INDEX IF NOT EXISTS idx_x402_tasks_share ON x402_tasks(share_id)")
    _schema_ready = True


def _meta(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(row.get("meta_json") or "{}") or {}
    except ValueError:
        return {}


def _set_meta(task_id: str, **fields: Any) -> None:
    row = db.query_one("SELECT meta_json FROM x402_tasks WHERE task_id=?", (task_id,)) or {}
    m = _meta(row)
    m.update(fields)
    db.execute("UPDATE x402_tasks SET meta_json=? WHERE task_id=?",
               (json.dumps(m, default=str), task_id))


def _hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def by_payment(payer: str, nonce: str) -> Optional[Dict[str, Any]]:
    ensure_schema()
    return db.query_one("SELECT * FROM x402_tasks WHERE payer=? AND nonce=?",
                        ((payer or "").lower(), (nonce or "").lower()))


def by_id(task_id: str) -> Optional[Dict[str, Any]]:
    ensure_schema()
    return db.query_one("SELECT * FROM x402_tasks WHERE task_id=?", (str(task_id or ""),))


def running_for(payer: str) -> int:
    ensure_schema()
    row = db.query_one("SELECT COUNT(*) n FROM x402_tasks WHERE payer=? AND status='running'",
                       ((payer or "").lower(),)) or {}
    return int(row.get("n") or 0)


def create(v: Dict[str, Any], payer: str, nonce: str, payment_id: int,
           amount_usd: float) -> Tuple[Dict[str, Any], str]:
    """Record a paid task (status `awaiting_payment` until settlement lands).

    Idempotent per (payer, nonce): a buyer retrying the same signature after a
    lost reply gets the SAME task back, never a second one. The access token is
    returned once and only its hash is stored."""
    ensure_schema()
    existing = by_payment(payer, nonce)
    if existing:
        # Redelivery after a lost reply. Reaching here already required a
        # signature that recovers to this payer (routes._gate), so handing the
        # rightful buyer a fresh token is safe — and it is the only way they
        # can reach a task whose first response never arrived.
        token = secrets.token_urlsafe(32)
        db.execute("UPDATE x402_tasks SET token_hash=? WHERE task_id=?",
                   (_hash(token), existing["task_id"]))
        return by_id(existing["task_id"]) or existing, token
    token = secrets.token_urlsafe(32)
    task_id = "t_" + uuid.uuid4().hex[:16]
    now = time.time()
    db.execute(
        "INSERT INTO x402_tasks(task_id, payer, nonce, payment_id, product, task_type, symbol, "
        "hours, frequency, checks_total, amount_usd, name, goal, status, token_hash, created_at, "
        "meta_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (task_id, (payer or "").lower(), (nonce or "").lower(), int(payment_id or 0),
         PRODUCT_OF[v["task"]], v["task"], v["symbol"], int(v["hours"]), int(v["frequency"]),
         int(v["checks"]), float(amount_usd or 0), v.get("name") or "", v["goal"],
         "awaiting_payment", _hash(token), now, "{}"))
    row = by_id(task_id) or {}
    return row, token


def activate(task_id: str, tx: str = "") -> Optional[Dict[str, Any]]:
    """Settlement landed → the run starts now (first check on the next tick)."""
    row = by_id(task_id)
    if not row or row["status"] != "awaiting_payment":
        return row
    now = time.time()
    db.execute("UPDATE x402_tasks SET status='running', tx=?, started_at=?, next_run_at=? "
               "WHERE task_id=? AND status='awaiting_payment'",
               ((tx or "").lower(), now, now, task_id))
    return by_id(task_id)


def stop(task_id: str, reason: str = "stopped_by_buyer") -> Optional[Dict[str, Any]]:
    """Soft stop — the row and every delivered report stay (constitution §4).
    No refund: the run was paid for in full up front and the buyer is told so
    before they sign."""
    row = by_id(task_id)
    if not row or row["status"] not in ("running", "awaiting_payment"):
        return row
    db.execute("UPDATE x402_tasks SET status='stopped', ended_at=?, next_run_at=0 WHERE task_id=?",
               (time.time(), task_id))
    _set_meta(task_id, stop_reason=reason)
    return by_id(task_id)


def runs_of(task_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    ensure_schema()
    rows = db.query_all("SELECT seq, ts, ok, headline, body_json, error FROM x402_task_runs "
                        "WHERE task_id=? ORDER BY seq ASC, id ASC LIMIT ?", (task_id, int(limit)))
    out = []
    for r in rows:
        try:
            body = json.loads(r.get("body_json") or "{}") or {}
        except ValueError:
            body = {}
        out.append({"seq": int(r["seq"]), "ts": float(r["ts"]), "ok": bool(r["ok"]),
                    "headline": r.get("headline") or "", "report": body,
                    "error": r.get("error") or ""})
    return out


# ------------------------------------------------------------------ views --

def view(row: Dict[str, Any], with_runs: bool = True) -> Dict[str, Any]:
    """The buyer's view of their own task. Never the token hash or meta."""
    d = {
        "task_id": row["task_id"],
        "task": row["task_type"],
        "symbol": row["symbol"],
        "name": row.get("name") or "",
        "goal": row.get("goal") or "",
        "hours": int(row["hours"]),
        "frequency": int(row["frequency"]),
        "checks_total": int(row["checks_total"]),
        "checks_done": int(row["checks_done"]),
        "checks_failed": int(row["checks_failed"]),
        "status": row["status"],
        "amount_usd": round(float(row.get("amount_usd") or 0), 6),
        "created_at": float(row.get("created_at") or 0),
        "started_at": float(row.get("started_at") or 0),
        "next_run_at": float(row.get("next_run_at") or 0),
        "ended_at": float(row.get("ended_at") or 0),
        "payer_short": (row.get("payer") or "")[:6] + "…" + (row.get("payer") or "")[-4:],
        "shared": bool(int(row.get("shared") or 0)),
        "share_url": share_url(row),
        "payment": {"tx": row.get("tx") or "",
                    "explorer": (x402.CHAIN["explorer_tx"] + row["tx"]) if row.get("tx") else "",
                    "asset": "USDC", "network": x402.NETWORK},
    }
    if with_runs:
        d["runs"] = runs_of(row["task_id"])
    return d


def summary() -> Dict[str, Any]:
    """Public, anonymous: how much real work this lane has done. Never a goal,
    never a full address."""
    ensure_schema()
    row = db.query_one(
        "SELECT COUNT(*) n, COUNT(DISTINCT payer) payers, COALESCE(SUM(amount_usd),0) usd, "
        "COALESCE(SUM(checks_done),0) reports FROM x402_tasks WHERE status<>'awaiting_payment'") or {}
    running = db.query_one("SELECT COUNT(*) n FROM x402_tasks WHERE status='running'") or {}
    # "Came back on another day and paid again" — a real user action. Days a
    # task spent running on its own are deliberately NOT counted (per the
    # product note: auto-running for days is not the same as returning).
    ret = db.query_one(
        "SELECT COUNT(*) c FROM (SELECT payer, COUNT(DISTINCT date(created_at,'unixepoch')) d "
        "FROM x402_tasks WHERE status<>'awaiting_payment' GROUP BY payer HAVING d>=2)") or {}
    shared = db.query_one("SELECT COUNT(*) n FROM x402_tasks WHERE shared=1") or {}
    return {"tasks": int(row.get("n") or 0), "payers": int(row.get("payers") or 0),
            "usd": round(float(row.get("usd") or 0), 4),
            "reports": int(row.get("reports") or 0),
            "running": int(running.get("n") or 0),
            "returning_payers": int(ret.get("c") or 0),
            "shared": int(shared.get("n") or 0)}


def admin_recent(limit: int = 20) -> List[Dict[str, Any]]:
    """Operator view: the latest runs with their payment, for /admin. The goal
    is the buyer's own text, so it is truncated and never leaves the console."""
    ensure_schema()
    rows = db.query_all("SELECT * FROM x402_tasks ORDER BY id DESC LIMIT ?", (int(limit),))
    return [{"task_id": r["task_id"], "payer": (r["payer"] or "")[:10] + "…",
             "task": r["task_type"], "symbol": r["symbol"],
             "hours": int(r["hours"]), "frequency": int(r["frequency"]),
             "checks": f'{int(r["checks_done"])}/{int(r["checks_total"])}',
             "failed": int(r["checks_failed"]), "status": r["status"],
             "usd": round(float(r["amount_usd"] or 0), 4),
             "tx": r.get("tx") or "", "created_at": float(r.get("created_at") or 0),
             "goal": (r.get("goal") or "")[:120]} for r in rows]


def set_shared(task_id: str, shared: bool) -> Optional[Dict[str, Any]]:
    """Buyer opt-in: publish this run's reports at a read-only link, or take it
    back down. Sharing is OFF by default and only the buyer (who holds the
    access token) can turn it on — the goal text is their own words, so it is
    never public unless they say so."""
    row = by_id(task_id)
    if not row:
        return None
    if shared:
        sid = (row.get("share_id") or "") or ("r_" + secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12])
        db.execute("UPDATE x402_tasks SET shared=1, share_id=?, shared_at=? WHERE task_id=?",
                   (sid, time.time(), task_id))
    else:
        # Keep the id so re-sharing reuses the same link (and an old link that
        # was sent to someone simply stops working while it is off).
        db.execute("UPDATE x402_tasks SET shared=0 WHERE task_id=?", (task_id,))
    return by_id(task_id)


def share_url(row: Dict[str, Any]) -> str:
    sid = (row or {}).get("share_id") or ""
    return f"{x402.public_base()}/r/{sid}" if sid and int(row.get("shared") or 0) else ""


def by_share_id(share_id: str) -> Optional[Dict[str, Any]]:
    ensure_schema()
    sid = str(share_id or "").strip()
    if not sid:
        return None
    return db.query_one("SELECT * FROM x402_tasks WHERE share_id=? AND shared=1", (sid,))


def public_view(row: Dict[str, Any]) -> Dict[str, Any]:
    """What a shared link shows: the assignment, every delivered report and the
    on-chain receipt — never the token, never the full payer address."""
    d = view(row, with_runs=True)
    d.pop("payer", None)
    d["shared"] = True
    d["share_url"] = share_url(row)
    d["shared_at"] = float(row.get("shared_at") or 0)
    return d


def shared_recent(limit: int = 20) -> List[Dict[str, Any]]:
    """Public delivery samples — what a judge or a new buyer can open without
    owning a task. Only runs their buyer published."""
    ensure_schema()
    rows = db.query_all("SELECT * FROM x402_tasks WHERE shared=1 AND share_id<>'' "
                        "ORDER BY shared_at DESC LIMIT ?", (int(limit),))
    return [{"share_id": r["share_id"], "url": share_url(r), "task": r["task_type"],
             "symbol": r["symbol"], "name": r.get("name") or "",
             "reports": int(r["checks_done"]), "status": r["status"],
             "usd": round(float(r["amount_usd"] or 0), 4),
             "tx": r.get("tx") or "", "shared_at": float(r.get("shared_at") or 0)} for r in rows]


# --------------------------------------------------------------- recovery --

def recovery_message(address: str, issued_at: int) -> str:
    """Plain-text challenge the buyer signs to list their tasks on a new
    device. No transaction, no gas — and it is bound to a timestamp so an old
    signature cannot be replayed forever."""
    return ("ManekiAI — restore my Arena tasks\n\n"
            f"Wallet: {(address or '').lower()}\n"
            f"Issued: {int(issued_at)}\n\n"
            "Signing this only lists the tasks this wallet already paid for.")


def recover(address: str, issued_at: int, signature: str) -> List[Dict[str, Any]]:
    """Verify the signature locally (ecrecover), then hand back this payer's
    tasks WITH fresh access tokens. Raises Invalid on a bad/stale signature."""
    ensure_schema()
    address = (address or "").strip().lower()
    if not _ADDR_RE.fullmatch(address):
        raise Invalid("invalid wallet address")
    now = int(time.time())
    try:
        issued = int(issued_at)
    except (TypeError, ValueError):
        raise Invalid("invalid timestamp")
    if abs(now - issued) > RECOVER_TTL_S:
        raise Invalid("this signature has expired — sign again")
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        recovered = Account.recover_message(
            encode_defunct(text=recovery_message(address, issued)), signature=signature)
    except Exception:
        raise Invalid("signature verification failed")
    if str(recovered).lower() != address:
        raise Invalid("signature does not match this wallet")
    rows = db.query_all("SELECT * FROM x402_tasks WHERE payer=? AND status<>'awaiting_payment' "
                        "ORDER BY id DESC LIMIT 50", (address,))
    out = []
    for r in rows:
        token = secrets.token_urlsafe(32)
        db.execute("UPDATE x402_tasks SET token_hash=? WHERE task_id=?", (_hash(token), r["task_id"]))
        out.append({**view(r, with_runs=False), "token": token})
    return out


def token_ok(row: Dict[str, Any], token: str) -> bool:
    th = (row or {}).get("token_hash") or ""
    return bool(th) and secrets.compare_digest(th, _hash(token or ""))


# --------------------------------------------------------------- the run --

_PROMPT = {
    "monitor": (
        "You are ManekiAI's research agent on a standing assignment a client has already paid for. "
        "This is check {seq} of {total}, one every {freq} minutes on {sym}.\n\n"
        "The client's assignment: {goal}\n\n"
        "Report, using the market snapshot above: (1) what has changed since your last check and "
        "whether it matters, (2) the levels and signals worth watching right now, (3) the main risk, "
        "(4) one line on whether the client should pay attention or can leave it alone. "
        "Be specific with numbers. If nothing meaningful changed, say so plainly instead of padding."),
    "research": (
        "You are ManekiAI's research agent on a standing research assignment a client has already "
        "paid for. This is check {seq} of {total}, one every {freq} minutes on {sym}.\n\n"
        "The client's question: {goal}\n\n"
        "Using the market snapshot above, advance the research: (1) the current read and the evidence "
        "for it, (2) the strongest argument against it, (3) what new information would change your "
        "view, (4) where this leaves the client's question. Be specific with numbers and do not "
        "repeat your previous check verbatim."),
}


def _budget_ok() -> bool:
    """Daily ceiling on model calls this lane may make — the analyst key is
    shared with the paid chat and with every live agent on this box."""
    row = db.query_one("SELECT COUNT(*) n FROM x402_task_runs WHERE ts>=?",
                       (time.time() - 86400,)) or {}
    return int(row.get("n") or 0) < DAILY_CHECK_BUDGET


def due_tasks(limit: int = RUN_BATCH) -> List[Dict[str, Any]]:
    ensure_schema()
    return db.query_all(
        "SELECT * FROM x402_tasks WHERE status='running' AND next_run_at>0 AND next_run_at<=? "
        "ORDER BY next_run_at ASC LIMIT ?", (time.time(), int(limit)))


def _finish(task_id: str, status: str) -> None:
    db.execute("UPDATE x402_tasks SET status=?, ended_at=?, next_run_at=0 WHERE task_id=?",
               (status, time.time(), task_id))


def _expired(row: Dict[str, Any]) -> bool:
    """Past its own window (plus grace) — a box that was down for hours must
    not suddenly fire a day's worth of checks back to back."""
    started = float(row.get("started_at") or 0)
    return bool(started) and time.time() > started + int(row["hours"]) * 3600 + GRACE_S


def _history(task_id: str, n: int = 2) -> List[Dict[str, Any]]:
    rows = db.query_all("SELECT headline, body_json FROM x402_task_runs WHERE task_id=? AND ok=1 "
                        "ORDER BY seq DESC LIMIT ?", (task_id, int(n)))
    out = []
    for r in reversed(rows):
        try:
            body = json.loads(r.get("body_json") or "{}") or {}
        except ValueError:
            body = {}
        text = body.get("text") or r.get("headline") or ""
        if text:
            out.append({"role": "assistant", "content": text[:1200]})
    return out


def _record_run(task_id: str, seq: int, ok: bool, headline: str, body: Dict[str, Any],
                error: str = "") -> None:
    db.execute("INSERT INTO x402_task_runs(task_id, seq, ts, ok, headline, body_json, error) "
               "VALUES(?,?,?,?,?,?,?)",
               (task_id, int(seq), time.time(), 1 if ok else 0, (headline or "")[:300],
                json.dumps(body or {}, default=str), (error or "")[:300]))


def run_one(row: Dict[str, Any]) -> Dict[str, Any]:
    """Execute ONE check of one task. Never raises: a failed check is recorded
    and retried once, and a task that keeps failing stops with a reason the
    buyer can read."""
    from ..services import chat_service
    task_id = row["task_id"]
    if _expired(row):
        _finish(task_id, "completed" if int(row["checks_done"]) else "failed")
        return {"task_id": task_id, "skipped": "window_over"}
    seq = int(row["checks_done"]) + 1
    meta = _meta(row)
    attempts = int(meta.get("attempts_for_seq") or 0) if int(meta.get("seq") or 0) == seq else 0
    sym = row["symbol"]
    full = sym if ":" in sym else f"xyz:{sym}"
    prompt = _PROMPT[row["task_type"]].format(
        seq=seq, total=int(row["checks_total"]), freq=int(row["frequency"]), sym=sym,
        goal=row.get("goal") or "")
    try:
        c = config_model.load("")          # the operator's shared analyst key
        data = chat_service._run_chat(c, _history(task_id), prompt, full, False)
        billable = bool(data.pop("_billable", False))
    except Exception as e:                  # noqa: BLE001 — a check must never kill the loop
        data, billable = {}, False
        oplog.error("x402.task_run", repr(e)[:200], params={"task_id": task_id, "seq": seq})
    if not billable:
        attempts += 1
        _set_meta(task_id, seq=seq, attempts_for_seq=attempts)
        db.execute("UPDATE x402_tasks SET checks_failed=checks_failed+1 WHERE task_id=?", (task_id,))
        if attempts >= MAX_ATTEMPTS_PER_CHECK:
            # Give up on this check, burn the slot, keep the schedule honest.
            _record_run(task_id, seq, False, "", {}, "the analyst could not produce this check")
            fails = int(meta.get("consecutive_fails") or 0) + 1
            db.execute("UPDATE x402_tasks SET checks_done=checks_done+1 WHERE task_id=?", (task_id,))
            _set_meta(task_id, consecutive_fails=fails, attempts_for_seq=0)
            if fails >= MAX_CONSECUTIVE_FAILS:
                _finish(task_id, "failed")
                return {"task_id": task_id, "failed": seq, "stopped": True}
            _advance(task_id, row)
            return {"task_id": task_id, "failed": seq}
        db.execute("UPDATE x402_tasks SET next_run_at=? WHERE task_id=?",
                   (time.time() + RETRY_DELAY_S, task_id))
        return {"task_id": task_id, "retry": seq}

    structured = {"on_topic": data.get("on_topic", True), "headline": data.get("headline", ""),
                  "points": data.get("points") or [], "analysis": data.get("analysis"),
                  "note": data.get("note")}
    body = {"text": chat_service._flatten(structured), "structured": structured,
            "mark": data.get("mark"), "symbol": sym, "seq": seq,
            "total": int(row["checks_total"]), "ts": time.time()}
    _record_run(task_id, seq, True, structured.get("headline") or "", body)
    db.execute("UPDATE x402_tasks SET checks_done=checks_done+1 WHERE task_id=?", (task_id,))
    _set_meta(task_id, consecutive_fails=0, attempts_for_seq=0, seq=seq)
    fresh = by_id(task_id) or row
    if int(fresh["checks_done"]) >= int(fresh["checks_total"]):
        _finish(task_id, "completed")
        return {"task_id": task_id, "check": seq, "completed": True}
    _advance(task_id, fresh)
    return {"task_id": task_id, "check": seq}


def _advance(task_id: str, row: Dict[str, Any]) -> None:
    """Schedule the next check on the paid cadence, never in the past (a box
    that slept does not owe a burst)."""
    step = int(row["frequency"]) * 60
    nxt = max(time.time() + 1, float(row.get("next_run_at") or 0) + step)
    db.execute("UPDATE x402_tasks SET next_run_at=? WHERE task_id=?", (nxt, task_id))


def run_due(limit: int = RUN_BATCH) -> Dict[str, Any]:
    """One tick of the runner (called from app.py every 60 s)."""
    if not enabled():
        return {}
    ensure_schema()
    if not _budget_ok():
        return {"skipped": "daily_budget"}
    out: Dict[str, Any] = {"ran": 0, "completed": 0, "failed": 0}
    for row in due_tasks(limit):
        r = run_one(row)
        if r.get("check"):
            out["ran"] += 1
        if r.get("completed"):
            out["completed"] += 1
        if r.get("failed"):
            out["failed"] += 1
    return out if (out["ran"] or out["completed"] or out["failed"]) else {}


def sweep_stale() -> int:
    """Close runs whose window is over but that never got their last checks in
    (box was down). Cheap, idempotent, called from the same loop."""
    ensure_schema()
    rows = db.query_all("SELECT * FROM x402_tasks WHERE status='running'")
    n = 0
    for r in rows:
        if _expired(r):
            _finish(r["task_id"], "completed" if int(r["checks_done"]) else "failed")
            n += 1
    return n
