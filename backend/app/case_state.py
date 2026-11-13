"""用例执行状态与知识源新鲜度筛选。

列表页、MCP 和缺口报告都要回答同一类问题：
- 这条用例执行过吗？
- enabled 了但一直没有结果？
- 还是 pending 从未启用？
- 知识源重新解析后，用例是否已经过期？
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import Select, func, select

from app.models import CaseResult, KnowledgeSource, TestCase

EXECUTION_STATES = {
    "never_executed",
    "enabled_no_result",
    "pending_not_enabled",
    "stale_after_source",
    "executed",
}
EXECUTION_STATE_PATTERN = "^(never|executed|never_executed|enabled_no_result|pending_not_enabled|stale_after_source)$"


def latest_source_parsed_at(session: Any, project_id: int) -> datetime | None:
    return session.scalar(
        select(func.max(KnowledgeSource.parsed_at)).where(KnowledgeSource.project_id == project_id)
    )


def executed_case_ids(session: Any, project_id: int, case_ids: Iterable[int] | None = None) -> set[int]:
    stmt = select(CaseResult.case_id).where(CaseResult.project_id == project_id)
    if case_ids is not None:
        wanted = list(case_ids)
        if not wanted:
            return set()
        stmt = stmt.where(CaseResult.case_id.in_(wanted))
    return {int(item) for item in session.scalars(stmt).all() if item is not None}


def states_for_case(
    case: TestCase,
    *,
    executed: set[int],
    latest_parsed_at: datetime | None,
) -> list[str]:
    states: list[str] = []
    has_result = case.id in executed
    if not has_result:
        states.append("never_executed")
    if case.status == "enabled" and not has_result:
        states.append("enabled_no_result")
    if case.status == "pending":
        states.append("pending_not_enabled")
    if latest_parsed_at and case.updated_at and case.updated_at < latest_parsed_at:
        states.append("stale_after_source")
    if has_result:
        states.append("executed")
    return states


def apply_execution_state_filter(
    stmt: Select,
    session: Any,
    project_id: int,
    execution_state: str | None,
) -> Select:
    """把执行状态过滤下推到 SQL，避免先 limit 再过滤导致漏数据。"""
    if not execution_state or execution_state == "all":
        return stmt
    state = "never_executed" if execution_state == "never" else execution_state
    executed = select(CaseResult.case_id).where(CaseResult.project_id == project_id)
    if state == "never_executed":
        return stmt.where(~TestCase.id.in_(executed))
    if state == "executed":
        return stmt.where(TestCase.id.in_(executed))
    if state == "enabled_no_result":
        return stmt.where(TestCase.status == "enabled", ~TestCase.id.in_(executed))
    if state == "pending_not_enabled":
        return stmt.where(TestCase.status == "pending")
    if state == "stale_after_source":
        latest = latest_source_parsed_at(session, project_id)
        if latest is None:
            return stmt.where(False)
        return stmt.where(TestCase.updated_at < latest)
    return stmt
