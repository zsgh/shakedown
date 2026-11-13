"""演示用的「被测系统」：真实可跑的 HTTP 服务（故意留了两个 bug）。

    python examples/demo-target/server.py --port 9100

缺陷：
1. ``POST /api/orders`` 不校验 amount > 0；
2. ``GET /api/admin/stats`` 不做鉴权。
"""
from __future__ import annotations

import argparse
import sqlite3
import threading
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="用户中心（被测系统）")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(":memory:", check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT, role TEXT)")
        _conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, amount INTEGER, remark TEXT, status TEXT)")
    return _conn


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "time": int(time.time())}


@app.get("/api/users")
def list_users(page: int = 1, size: int = 20) -> dict[str, Any]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    start = max(0, (page - 1) * size)
    return {"items": [_row_to_dict(r) for r in rows[start : start + size]], "total": len(rows)}


@app.post("/api/users")
async def create_user(request: Request) -> Any:
    body = await request.json()
    name = (body or {}).get("name")
    email = (body or {}).get("email")
    if not name or not email:
        return JSONResponse({"message": "name and email are required"}, status_code=400)
    conn = _get_conn()
    with _lock:
        cursor = conn.execute(
            "INSERT INTO users (name, email, role) VALUES (?, ?, ?)",
            (name, email, (body or {}).get("role", "user")),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return JSONResponse(_row_to_dict(row), status_code=201)


@app.get("/api/users/{user_id}")
def get_user(user_id: int) -> Any:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return JSONResponse({"message": "user not found"}, status_code=404)
    return _row_to_dict(row)


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int) -> Any:
    conn = _get_conn()
    with _lock:
        cursor = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    if cursor.rowcount == 0:
        return JSONResponse({"message": "user not found"}, status_code=404)
    return {"message": "deleted"}


@app.post("/api/orders")
async def create_order(request: Request) -> Any:
    payload = await request.json()
    user_id = payload.get("user_id")
    amount = payload.get("amount")
    if user_id is None or amount is None:
        return JSONResponse({"message": "user_id and amount are required"}, status_code=400)
    # BUG（故意保留）：缺少 amount > 0 的校验
    conn = _get_conn()
    with _lock:
        cursor = conn.execute(
            "INSERT INTO orders (user_id, amount, remark, status) VALUES (?, ?, ?, ?)",
            (user_id, amount, payload.get("remark", ""), "created"),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return JSONResponse(_row_to_dict(row), status_code=201)


@app.get("/api/orders/{order_id}")
def get_order(order_id: int) -> Any:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        return JSONResponse({"message": "order not found"}, status_code=404)
    return _row_to_dict(row)


@app.get("/api/admin/stats")
def admin_stats() -> dict[str, Any]:
    # BUG（故意保留）：缺少鉴权判断
    conn = _get_conn()
    users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    orders = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
    return {"users": users, "orders": orders, "generated_at": int(time.time())}


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
