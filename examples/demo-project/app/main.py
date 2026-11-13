"""示例被测项目（演示用）——用户中心服务。

注意：本文件**不是可运行的服务**，它只是给「代码路由抽取」用的样例代码
（刻意使用各框架常见的路由写法）。可运行的被测系统见
``examples/demo-target/server.py``。

这里故意保留了两个缺陷，用来验证 Shakedown 能不能「真实测出 bug」：

1. ``POST /api/orders`` 没有校验 amount 必须大于 0（负数金额被接受）；
2. ``GET /api/admin/stats`` 没有做鉴权（未带 token 也能访问）。

代码里的路由写法用于验证「代码抽取路由」能力。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any

_lock = threading.Lock()
_db_path = ":memory:"
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, email TEXT, role TEXT)")
        _conn.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, user_id INTEGER, amount INTEGER, remark TEXT, status TEXT)")
    return _conn


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# ── 用户 ────────────────────────────────────────────────────
@app.get("/api/users")
def list_users(page: int = 1, size: int = 20):
    """用户列表（分页）。"""
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    start = max(0, (page - 1) * size)
    return {"items": [_row_to_dict(r) for r in rows[start : start + size]], "total": len(rows)}


@app.post("/api/users")
def create_user(body: dict[str, Any]):
    """创建用户。"""
    name = (body or {}).get("name")
    email = (body or {}).get("email")
    if not name or not email:
        return {"message": "name and email are required"}, 400
    conn = _get_conn()
    with _lock:
        cursor = conn.execute(
            "INSERT INTO users (name, email, role) VALUES (?, ?, ?)",
            (name, email, (body or {}).get("role", "user")),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return _row_to_dict(row), 201


@app.get("/api/users/{id}")
def get_user(id: int):
    """用户详情。"""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (id,)).fetchone()
    if row is None:
        return {"message": "user not found"}, 404
    return _row_to_dict(row)


@app.delete("/api/users/{id}")
def delete_user(id: int):
    """删除用户（注意：spec 与文档里都没有这个接口，属于代码独有）。"""
    conn = _get_conn()
    with _lock:
        cursor = conn.execute("DELETE FROM users WHERE id = ?", (id,))
        conn.commit()
    if cursor.rowcount == 0:
        return {"message": "user not found"}, 404
    return {"message": "deleted"}


# ── 订单 ────────────────────────────────────────────────────
@app.post("/api/orders")
def create_order(body: dict[str, Any]):
    """创建订单。

    BUG（故意保留）：没有校验 amount > 0，负数金额会被接受。
    """
    payload = body or {}
    user_id = payload.get("user_id")
    amount = payload.get("amount")
    if user_id is None or amount is None:
        return {"message": "user_id and amount are required"}, 400
    conn = _get_conn()
    with _lock:
        cursor = conn.execute(
            "INSERT INTO orders (user_id, amount, remark, status) VALUES (?, ?, ?, ?)",
            (user_id, amount, payload.get("remark", ""), "created"),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return _row_to_dict(row), 201


@app.get("/api/orders/{id}")
def get_order(id: int):
    """订单详情。"""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (id,)).fetchone()
    if row is None:
        return {"message": "order not found"}, 404
    return _row_to_dict(row)


# ── 管理 ────────────────────────────────────────────────────
@app.get("/api/admin/stats")
def admin_stats():
    """平台统计。

    BUG（故意保留）：缺少鉴权判断，未登录也能访问。
    """
    conn = _get_conn()
    users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    orders = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
    return {"users": users, "orders": orders, "generated_at": int(time.time())}
