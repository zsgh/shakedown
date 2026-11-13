"""用例规格修复：把「执行器不可能判对」的断言改成它认的形状。

生成器历史上把命令步骤的输出断言写成 ``status_code``（执行器拿退出码 0 去比
文本标记），或者在请求步骤上写 ``exit_code``。这类断言不是业务判断错误，
改的是断言的**类型**，不碰期望值和步骤本身，因此可以确定性修复。

修复只动断言的 ``kind`` / ``operator``，并保留原值到 ``kind_before``，
方便在用例详情里看到「改过什么」。
"""
from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.executor.case_schema import collect_assertions
from app.logging import get_logger
from app.models import Project, TestCase

logger = get_logger("shakedown.spec_repair")

_COMMAND_ACTIONS = {"command"}


def _is_numeric(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and value.strip().lstrip("-").isdigit()


def repair_assertion(
    action: str,
    assertion: dict[str, Any],
    command: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """就地修复一条断言；返回变更说明，无需修复时返回 None。

    ``command`` 是这一步真实执行的 stdout/stderr/exit_code。有它时才能判断
    「断言查错了输出流」——期望文本只出现在另一个流里的情况。
    """
    if not isinstance(assertion, dict):
        return None
    kind = str(assertion.get("kind") or "")
    operator = str(assertion.get("operator") or "eq")
    expected = assertion.get("expected")

    if action in _COMMAND_ACTIONS:
        if command and kind in {"stdout_contains", "stderr_contains"}:
            expected_text = str(expected)
            own_key = "stdout" if kind == "stdout_contains" else "stderr"
            other_key = "stderr" if own_key == "stdout" else "stdout"
            own = str(command.get(own_key) or "")
            other = str(command.get(other_key) or "")
            if expected_text and expected_text not in own and expected_text in other:
                assertion["kind_before"] = kind
                assertion["kind"] = "stderr_contains" if own_key == "stdout" else "stdout_contains"
                return {
                    "before": kind,
                    "after": assertion["kind"],
                    "reason": f"期望文本只出现在 {other_key}，改查 {other_key}",
                }
        if kind == "text_contains":
            assertion["kind_before"] = kind
            assertion["kind"] = "stdout_contains"
            return {"before": "text_contains", "after": "stdout_contains", "reason": "命令步骤应比较 stdout"}
        if kind == "status_code":
            assertion["kind_before"] = kind
            if _is_numeric(expected):
                assertion["kind"] = "exit_code"
                after = "exit_code"
                reason = "命令步骤应以退出码判定"
            else:
                assertion["kind"] = "stdout_contains"
                after = "stdout_contains"
                reason = "命令步骤拿退出码比文本标记，改为 stdout 包含判定"
                if operator not in {"contains", "not_contains"}:
                    assertion["operator"] = "contains"
            return {"before": "status_code", "after": after, "reason": reason}
        return None

    if kind in {"exit_code", "stdout_contains", "stderr_contains"}:
        assertion["kind_before"] = kind
        if kind == "exit_code" and _is_numeric(expected):
            assertion["kind"] = "status_code"
            after = "status_code"
        else:
            assertion["kind"] = "text_contains"
            after = "text_contains"
        return {"before": kind, "after": after, "reason": "非命令步骤不应使用命令断言"}

    if kind == "status_code" and not _is_numeric(expected) and operator in {"contains", "not_contains", "matches"}:
        assertion["kind_before"] = kind
        assertion["kind"] = "text_contains"
        return {
            "before": "status_code",
            "after": "text_contains",
            "reason": "状态码断言的期望是文本，改为响应体包含判定",
        }
    return None


def repair_spec(
    spec: dict[str, Any],
    commands: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """就地修复一份 spec，返回变更列表（不改动原对象之外的状态）。"""
    changes: list[dict[str, Any]] = []
    steps = spec.get("steps") or []
    by_step: dict[int, dict[str, Any]] = {}
    for item in commands or []:
        if isinstance(item, dict) and isinstance(item.get("step_index"), int):
            by_step[item["step_index"]] = item
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        action = str(step.get("action") or "")
        for assertion in step.get("assert") or []:
            change = repair_assertion(action, assertion, by_step.get(index))
            if change:
                changes.append({"step_index": index, "scope": "step", "action": action, **change})
    for assertion in spec.get("assertions") or []:
        if not isinstance(assertion, dict):
            continue
        step_index = assertion.get("step_index")
        action = ""
        if isinstance(step_index, int) and 0 <= step_index < len(steps):
            action = str((steps[step_index] or {}).get("action") or "")
        change = repair_assertion(
            action,
            assertion,
            by_step.get(step_index) if isinstance(step_index, int) else None,
        )
        if change:
            changes.append(
                {"step_index": step_index, "scope": "case", "action": action or "未绑定步骤", **change}
            )
    return changes


def repair_case_spec(case: TestCase, commands: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """修复一条用例并同步 steps / expected 列。"""
    spec = copy.deepcopy(case.spec or {})
    changes = repair_spec(spec, commands)
    if not changes:
        return []
    case.spec = spec
    case.steps = spec.get("steps") or []
    case.expected = {"assertions": collect_assertions(spec)}
    logger.info("已修复用例 %s 的断言类型：%s", case.id, changes)
    return changes


def preview_spec_repairs(db: Session, project: Project, *, case_ids: list[int] | None = None) -> dict[str, Any]:
    stmt = select(TestCase).where(TestCase.project_id == project.id)
    if case_ids:
        stmt = stmt.where(TestCase.id.in_([int(item) for item in case_ids]))
    cases = list(db.scalars(stmt.order_by(TestCase.id)).all())
    rows: list[dict[str, Any]] = []
    total = 0
    for case in cases:
        changes = repair_spec(copy.deepcopy(case.spec or {}))
        if not changes:
            continue
        total += len(changes)
        rows.append(
            {
                "case_id": case.id,
                "title": case.title,
                "status": case.status,
                "changes": changes,
            }
        )
    return {"affected_cases": len(rows), "assertions": total, "cases": rows}


def apply_spec_repairs(
    db: Session,
    project: Project,
    *,
    case_ids: list[int] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """批量修复；``dry_run`` 只返回预览。"""
    preview = preview_spec_repairs(db, project, case_ids=case_ids)
    if dry_run:
        return {**preview, "applied": False, "updated_cases": 0, "updated_assertions": 0}
    updated_cases = 0
    updated_assertions = 0
    for row in preview["cases"]:
        case = db.get(TestCase, row["case_id"])
        if case is None:
            continue
        changes = repair_case_spec(case)
        if changes:
            updated_cases += 1
            updated_assertions += len(changes)
    db.flush()
    return {
        **preview,
        "applied": True,
        "updated_cases": updated_cases,
        "updated_assertions": updated_assertions,
    }


__all__ = [
    "apply_spec_repairs",
    "preview_spec_repairs",
    "repair_assertion",
    "repair_case_spec",
    "repair_spec",
]
