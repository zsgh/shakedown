"""基础 WCAG / 可访问性规则库：把浏览器 a11y 指标转成可判定 violations。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CaseResult, Project, TestCase

RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "image-alt",
        "wcag": "1.1.1",
        "level": "serious",
        "field": "images_without_alt",
        "message": "图片缺少 alt 文本",
    },
    {
        "id": "button-name",
        "wcag": "4.1.2",
        "level": "serious",
        "field": "buttons_without_label",
        "message": "按钮缺少可访问名称",
    },
    {
        "id": "label",
        "wcag": "1.3.1",
        "level": "critical",
        "field": "inputs_without_label",
        "message": "表单控件缺少 label",
    },
    {
        "id": "link-name",
        "wcag": "2.4.4",
        "level": "serious",
        "field": "links_without_text",
        "message": "链接缺少可访问文本",
    },
    {
        "id": "duplicate-id",
        "wcag": "4.1.1",
        "level": "moderate",
        "field": "duplicate_ids",
        "message": "页面存在重复 id",
    },
    {
        "id": "heading-order",
        "wcag": "1.3.1",
        "level": "moderate",
        "field": "heading_jumps",
        "message": "标题层级跳跃",
    },
)


def audit_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for rule in RULES:
        field = rule["field"]
        value = metrics.get(field)
        passed = (value == 0) if isinstance(value, int) else not bool(value)
        check = {
            "id": rule["id"],
            "wcag": rule["wcag"],
            "level": rule["level"],
            "field": field,
            "value": value,
            "passed": passed,
            "message": rule["message"],
        }
        checks.append(check)
        if not passed:
            violations.append(check)
    if metrics.get("has_title") is False:
        item = {
            "id": "document-title",
            "wcag": "2.4.2",
            "level": "serious",
            "field": "has_title",
            "value": False,
            "passed": False,
            "message": "页面缺少 title",
        }
        checks.append(item)
        violations.append(item)
    if not str(metrics.get("html_lang") or "").strip():
        item = {
            "id": "html-lang",
            "wcag": "3.1.1",
            "level": "serious",
            "field": "html_lang",
            "value": metrics.get("html_lang"),
            "passed": False,
            "message": "html 缺少 lang",
        }
        checks.append(item)
        violations.append(item)
    if metrics.get("has_main_landmark") is False:
        item = {
            "id": "main-landmark",
            "wcag": "1.3.1",
            "level": "moderate",
            "field": "has_main_landmark",
            "value": False,
            "passed": False,
            "message": "缺少 main landmark",
        }
        checks.append(item)
        violations.append(item)
    return {"passed": not violations, "checks": checks, "violations": violations}


def latest_a11y(db: Session, project: Project, case_id: int) -> dict[str, Any] | None:
    case = db.get(TestCase, case_id)
    if case is None or case.project_id != project.id:
        raise ValueError(f"用例不存在或不属于当前项目：{case_id}")
    result = db.scalar(
        select(CaseResult)
        .where(
            CaseResult.project_id == project.id,
            CaseResult.case_id == case_id,
            CaseResult.output["a11y"].astext.isnot(None),
        )
        .order_by(CaseResult.id.desc())
        .limit(1)
    )
    if result is None:
        return None
    output = result.output if isinstance(result.output, dict) else {}
    metrics = output.get("a11y") if isinstance(output.get("a11y"), dict) else {}
    return {
        "case_id": case_id,
        "result_id": result.id,
        "metrics": metrics,
        **audit_metrics(metrics),
    }
