"""生成后试运行（validation）：新用例先跑一次，通过再启用。

定位：
- 只验证「可判定的用例」——有步骤、至少一条有依据的 confirmed 断言、
  无 missing_input、非高风险；
- ``trigger=validation`` 的 run 只执行一次：不重跑、不做 LLM 归因、
  不生成 finding、不进 digest；
- 通过 → enabled；失败 / 错误 / 不稳定 → needs_review + 例外；
  状态一旦变化就不会被再次选中；
- 一批最多 ``MAX_CASES_PER_RUN`` 条，跑完自动接下一批，
  避免一次性冲击被测系统，也避免无人值守时任务空转。
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import failure_attribution
from app.enums import CaseResultStatus, CaseStatus, RunStatus, RunTrigger, TaskKind
from app.exceptions import record_exception
from app.executor.case_schema import collect_assertions
from app.executor.risk import high_risk_reasons
from app.logging import get_logger
from app.models import AutomationException, CaseResult, Finding, Project, Run, TestCase
from app.executor.runner import build_curl
from app.spec_repair import repair_case_spec
from app.worker.queue import enqueue

logger = get_logger("shakedown.case_validation")

MAX_CASES_PER_RUN = 50
_ACTIVE_RUN_STATUSES = {
    RunStatus.QUEUED.value,
    RunStatus.PREPARING.value,
    RunStatus.RUNNING.value,
    RunStatus.ANALYZING.value,
}
_OPEN_EXCEPTION_STATUSES = {"open", "acked"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _automation(project: Project) -> dict[str, Any]:
    value = (project.settings_json or {}).get("automation")
    return value if isinstance(value, dict) else {}


def validation_enabled(project: Project) -> bool:
    """生成后是否自动试运行。默认开启：没人跑过的用例不算资产。"""
    return bool(_automation(project).get("auto_validate_generated", True))


def auto_enable_after_validation(project: Project) -> bool:
    """试运行通过后是否自动启用。默认开启，可由项目显式关闭。"""
    return bool(_automation(project).get("auto_enable_after_validation", True))


def confirmed_assertions(spec: dict[str, Any] | None) -> list[dict[str, Any]]:
    """有依据、且已确认的断言；只有 pending 断言的用例跑起来会「空转通过」，不算可判定。"""
    result: list[dict[str, Any]] = []
    for assertion in collect_assertions(spec or {}):
        if str(assertion.get("status") or "") != "confirmed":
            continue
        evidence = assertion.get("evidence") or {}
        source = str(evidence.get("source_type") or "")
        if source in {"", "none"}:
            continue
        result.append(assertion)
    return result


def _already_passed(case: TestCase) -> bool:
    state = (case.verification or {}).get("auto_validation")
    return isinstance(state, dict) and str(state.get("status") or "") == "pass"


def validation_eligible(case: TestCase, project: Project) -> tuple[bool, str]:
    """返回 ``(是否可试运行, 原因)``；原因用于预览和排查。"""
    if case.status != CaseStatus.PENDING.value:
        return False, "not_pending"
    if not case.steps:
        return False, "no_steps"
    # missing_input 只是「说明待补全」，不阻止执行：有步骤 + 有依据断言就可以先跑一次
    if not confirmed_assertions(case.spec or {}):
        return False, "no_confirmed_assertion"
    automation = _automation(project)
    if not automation.get("allow_high_risk_validation") and high_risk_reasons(
        case.spec or {}, project.settings_json or {}
    ):
        return False, "high_risk"
    if _already_passed(case):
        return False, "already_validated"
    return True, ""


def eligible_cases(
    db: Session,
    project: Project,
    *,
    case_ids: Iterable[int] | None = None,
    limit: int = MAX_CASES_PER_RUN,
) -> list[TestCase]:
    stmt = select(TestCase).where(
        TestCase.project_id == project.id,
        TestCase.status == CaseStatus.PENDING.value,
    )
    if case_ids is not None:
        ids = [int(item) for item in case_ids]
        if not ids:
            return []
        stmt = stmt.where(TestCase.id.in_(ids))
    stmt = stmt.order_by(TestCase.priority, TestCase.id)
    if case_ids is None:
        # 先粗筛一批，再按可判定条件过滤，避免把整个项目的 pending 都拉进内存
        stmt = stmt.limit(max(limit * 8, 200))
    selected: list[TestCase] = []
    for case in db.scalars(stmt).all():
        ok, _reason = validation_eligible(case, project)
        if not ok:
            continue
        selected.append(case)
        if len(selected) >= limit:
            break
    return selected


def active_validation_run(
    db: Session,
    project: Project,
    *,
    exclude_run_id: int | None = None,
) -> Run | None:
    stmt = (
        select(Run)
        .where(
            Run.project_id == project.id,
            Run.trigger == RunTrigger.VALIDATION.value,
            Run.status.in_(_ACTIVE_RUN_STATUSES),
        )
        .order_by(Run.id.desc())
        .limit(1)
    )
    if exclude_run_id is not None:
        stmt = stmt.where(Run.id != int(exclude_run_id))
    return db.scalar(stmt)


def create_validation_run(
    db: Session,
    project: Project,
    *,
    case_ids: Iterable[int] | None = None,
    limit: int = MAX_CASES_PER_RUN,
    exclude_run_id: int | None = None,
) -> tuple[Run | None, bool]:
    """创建一批试运行。返回 ``(run, created)``。

    - 已有排队 / 执行中的试运行时直接返回它，避免并发堆积；
    - 没有可判定的用例时返回 ``(None, False)``。
    """
    if not validation_enabled(project):
        return None, False
    active = active_validation_run(db, project, exclude_run_id=exclude_run_id)
    if active is not None:
        return active, False
    cases = eligible_cases(db, project, case_ids=case_ids, limit=limit)
    if not cases:
        return None, False
    run = Run(
        project_id=project.id,
        trigger=RunTrigger.VALIDATION.value,
        status=RunStatus.QUEUED.value,
        case_ids=[case.id for case in cases],
        env_snapshot={"base_url": project.base_url},
        stats={
            "total": len(cases),
            "policy": {"validation": True, "allow_pending": True},
            "validation": True,
        },
    )
    db.add(run)
    db.flush()
    task = enqueue(
        db,
        TaskKind.EXECUTE_RUN.value,
        {"run_id": run.id},
        project_id=project.id,
        run_id=run.id,
    )
    run.task_id = task.id
    db.flush()
    logger.info("试运行入队：project=%s run=%s cases=%s", project.id, run.id, len(cases))
    return run, True


def validation_preview(db: Session, project: Project, *, limit: int = 2000) -> dict[str, Any]:
    """预览有多少 pending 用例可以试运行，以及其余用例被什么条件挡住。"""
    cases = list(
        db.scalars(
            select(TestCase)
            .where(
                TestCase.project_id == project.id,
                TestCase.status == CaseStatus.PENDING.value,
            )
            .order_by(TestCase.priority, TestCase.id)
            .limit(max(1, min(limit, 5000)))
        ).all()
    )
    reasons: Counter[str] = Counter()
    dimensions: Counter[str] = Counter()
    eligible = 0
    for case in cases:
        ok, reason = validation_eligible(case, project)
        if ok:
            eligible += 1
            dimensions[case.dimension] += 1
        else:
            reasons[reason] += 1
    active = active_validation_run(db, project)
    return {
        "project_id": project.id,
        "pending_scanned": len(cases),
        "eligible": eligible,
        "reasons": dict(reasons),
        "by_dimension": dict(dimensions),
        "active_run_id": active.id if active else None,
        "auto_validate_generated": validation_enabled(project),
        "auto_enable_after_validation": auto_enable_after_validation(project),
    }


_VALIDATION_EXCEPTION_KINDS = (
    "case_validation_failed",
    "case_validation_environment",
    "platform_capability_gap",
    "suspect_defect",
)


def _resolve_validation_exception(db: Session, project: Project, case: TestCase) -> None:
    rows = db.scalars(
        select(AutomationException).where(
            AutomationException.project_id == project.id,
            AutomationException.kind.in_(_VALIDATION_EXCEPTION_KINDS),
            AutomationException.target_type == "case",
            AutomationException.target_id == case.id,
            AutomationException.status.in_(_OPEN_EXCEPTION_STATUSES),
        )
    ).all()
    for row in rows:
        row.status = "resolved"


def _add_tag(case: TestCase, tag: str) -> None:
    tags = [str(item) for item in (case.tags or [])]
    if tag not in tags:
        case.tags = [*tags, tag]


def _repair_attempts(case: TestCase) -> int:
    verification = case.verification or {}
    try:
        return int(verification.get("auto_repair_attempts") or 0)
    except (TypeError, ValueError):
        return 0


def _record_validation_defect(
    db: Session,
    project: Project,
    case: TestCase,
    run: Run,
    row: CaseResult,
    attribution: dict[str, Any],
) -> Finding:
    """试运行抓到真实缺陷：落一条缺陷，并把用例留作回归（而不是降级）。"""
    confirmed = bool(attribution.get("auto_confirm"))
    severity = "high" if case.dimension == "security" else "medium"
    if {"attack:csrf", "attack:rate-limit"} & set(case.tags or []):
        severity = "medium"
    if "attack:fuzz-json" in (case.tags or []):
        severity = "low"
    reason = str(attribution.get("reason") or "")[:2000]
    requests = (row.output or {}).get("requests") or []
    chain = " → ".join(
        f"{item.get('method', '')} {item.get('url', '')}".strip()
        for item in requests
        if isinstance(item, dict) and item.get("url")
    )
    description = reason
    if chain and "→" in chain:
        description = f"{reason}\n\n复现顺序：{chain}"[:4000]
    existing = db.scalar(
        select(Finding)
        .where(
            Finding.project_id == project.id,
            Finding.case_id == case.id,
            Finding.source == "execution",
            Finding.status.in_(["pending", "confirmed"]),
        )
        .order_by(Finding.id.desc())
        .limit(1)
    )
    evidence = [
        {
            "type": "assertion",
            "detail": (row.output or {}).get("assertions", []),
            "case_id": case.id,
            "run_id": run.id,
        }
    ]
    if existing is not None:
        existing.result_id = row.id
        existing.description = description or existing.description
        existing.evidence = evidence
        existing.repro = build_curl(row.request or {}) or existing.repro
        if confirmed and existing.status == "pending":
            existing.status = "confirmed"
            existing.confirmed_by = "auto:attack-pack"
            existing.confirmed_at = _now()
        return existing
    clean_title = case.title[len("[攻击]"):].strip() if case.title.startswith("[攻击]") else case.title
    finding = Finding(
        project_id=project.id,
        source="execution",
        title=f"[攻击用例] {clean_title}"[:400],
        description=description,
        severity=severity,
        evidence=evidence,
        result_id=row.id,
        case_id=case.id,
        module="攻击用例包" if "attack-pack" in (case.tags or []) else "试运行",
        risk_type={
            "security": "鉴权 / 越权",
            "boundary": "输入校验",
            "performance": "性能",
            "functional": "功能",
        }.get(case.dimension, "功能"),
        repro=build_curl(row.request or {}),
        status="confirmed" if confirmed else "pending",
        confirmed_by="auto:attack-pack" if confirmed else None,
        confirmed_at=_now() if confirmed else None,
    )
    db.add(finding)
    db.flush()
    return finding


def _record_validation_failure(
    db: Session,
    project: Project,
    case: TestCase,
    run: Run,
    row: CaseResult,
    attribution: dict[str, Any],
) -> None:
    detail = (row.logs or "").strip()[-800:]
    category = str(attribution.get("category") or "")
    if category == failure_attribution.CATEGORY_PLATFORM:
        kind = "platform_capability_gap"
        prefix = "平台无法正确执行该用例"
        severity = "medium"
    elif category == failure_attribution.CATEGORY_DEFECT:
        kind = "suspect_defect"
        prefix = "试运行发现疑似产品缺陷"
        severity = "high"
    elif category == failure_attribution.CATEGORY_ENVIRONMENT:
        kind = "case_validation_environment"
        prefix = "试运行未真正到达被测系统"
        severity = "medium"
    else:
        kind = "case_validation_failed"
        prefix = "新用例试运行未通过"
        severity = "low"
    reason = str(attribution.get("reason") or "")
    summary = reason[:220] or "无归因说明"
    if detail and detail[:80] not in summary:
        summary = f"{summary}；现场：{detail[:400]}"
    record_exception(
        db,
        project,
        kind=kind,
        target_type="case",
        target_id=case.id,
        severity=severity,
        note=f"{prefix}（{row.status}/{attribution.get('reason_label') or category}）：{summary}"[:1000],
        payload={
            "run_id": run.id,
            "case_id": case.id,
            "status": row.status,
            "reason_code": attribution.get("reason_code"),
            "reason": reason,
            "logs": detail,
        },
    )


def apply_validation_outcome(db: Session, project: Project, run: Run) -> dict[str, Any]:
    """把试运行结果落到用例状态上，并自动接下一批。"""
    rows = list(
        db.scalars(
            select(CaseResult).where(
                CaseResult.run_id == run.id,
                CaseResult.rerun.is_(False),
            )
        ).all()
    )
    # 整批都 error 通常是环境 / 沙箱问题，不是用例写错：保持 pending，等环境恢复后重试。
    if rows and all(str(row.status) == CaseResultStatus.ERROR.value for row in rows):
        sandbox_error = str(((run.stats or {}).get("sandbox") or {}).get("error") or "")
        record_exception(
            db,
            project,
            kind="case_validation_environment",
            target_type="run",
            target_id=run.id,
            severity="medium",
            note="试运行时全部用例报错，按环境问题处理，用例保持待确认",
            payload={"run_id": run.id, "sandbox_error": sandbox_error[:800]},
        )
        db.flush()
        logger.warning("试运行整批报错，按环境问题处理：project=%s run=%s", project.id, run.id)
        return {
            "validation": True,
            "validation_applied": 0,
            "promoted": 0,
            "demoted": 0,
            "next_run_id": None,
            "environment_error": True,
        }
    promote = auto_enable_after_validation(project)
    promoted = 0
    demoted = 0
    applied = 0
    environment_failures = 0
    platform_failures = 0
    auto_repaired = 0
    defects = 0
    for row in rows:
        case = db.get(TestCase, row.case_id)
        if case is None:
            continue
        # 执行期间人工已经改过状态：不覆盖人的决定
        if case.status != CaseStatus.PENDING.value:
            continue
        applied += 1
        outcome = str(row.status)
        if outcome == CaseResultStatus.PASS.value:
            if promote:
                case.status = CaseStatus.ENABLED.value
                promoted += 1
            _resolve_validation_exception(db, project, case)
            attribution: dict[str, Any] = {}
        elif outcome in {
            CaseResultStatus.FAIL.value,
            CaseResultStatus.ERROR.value,
            CaseResultStatus.FLAKY.value,
        }:
            attribution = failure_attribution.classify(case, row)
            row.attribution = attribution
            category = str(attribution.get("category") or "")
            retry = bool(attribution.get("retry_after_fix"))
            if category == failure_attribution.CATEGORY_ENVIRONMENT:
                # 环境没通，用例没被真正验证：保持 pending，不降级、不继续排队，
                # 避免环境故障期间把整批用例刷成「需要复核」。
                environment_failures += 1
                case.verification = {
                    **(case.verification or {}),
                    "auto_validation": {
                        "run_id": run.id,
                        "status": outcome,
                        "at": _now().isoformat(),
                        "logs": (row.logs or "").strip()[-500:],
                        "attribution": attribution,
                    },
                }
                _record_validation_failure(db, project, case, run, row, attribution)
                continue
            if category == failure_attribution.CATEGORY_DEFECT and float(attribution.get("confidence") or 0) >= 0.9:
                _record_validation_defect(db, project, case, run, row, attribution)
                _add_tag(case, "suspect-defect")
                defects += 1
                if promote:
                    case.status = CaseStatus.ENABLED.value
                else:
                    case.status = CaseStatus.PENDING.value
                _resolve_validation_exception(db, project, case)
                case.verification = {
                    **(case.verification or {}),
                    "auto_validation": {
                        "run_id": run.id,
                        "status": outcome,
                        "at": _now().isoformat(),
                        "logs": (row.logs or "").strip()[-500:],
                        "attribution": attribution,
                    },
                }
                continue
            if category == failure_attribution.CATEGORY_PLATFORM:
                platform_failures += 1
            if retry and _repair_attempts(case) < 1 and case.status == CaseStatus.PENDING.value:
                changes = repair_case_spec(case, (row.output or {}).get("commands"))
                case.verification = {
                    **(case.verification or {}),
                    "auto_repair_attempts": _repair_attempts(case) + 1,
                    "auto_repair_changes": changes,
                }
                _add_tag(case, "auto-repaired")
                auto_repaired += 1
                logger.info("试运行归因为平台缺口，已自动修复并重试：case=%s changes=%s", case.id, changes)
                # 保持 pending，下一批试运行会再跑一次；第二次失败就进 needs_review。
                case.verification = {
                    **(case.verification or {}),
                    "auto_validation": {
                        "run_id": run.id,
                        "status": "auto_repaired",
                        "at": _now().isoformat(),
                        "logs": (row.logs or "").strip()[-500:],
                        "attribution": attribution,
                    },
                }
                continue
            case.status = CaseStatus.NEEDS_REVIEW.value
            demoted += 1
            if category == failure_attribution.CATEGORY_DEFECT:
                _add_tag(case, "suspect-defect")
            elif category == failure_attribution.CATEGORY_PLATFORM:
                _add_tag(case, "platform-gap")
            _record_validation_failure(db, project, case, run, row, attribution)
        else:
            # skipped：保留 pending，等下一轮
            continue
        case.verification = {
            **(case.verification or {}),
            "auto_validation": {
                "run_id": run.id,
                "status": outcome,
                "at": _now().isoformat(),
                "logs": (row.logs or "").strip()[-500:],
                **({"attribution": attribution} if attribution else {}),
            },
        }
    db.flush()
    next_run: Run | None = None
    # 有环境失败时不再自动接下一批：环境没恢复，继续跑只会把用例反复刷成失败。
    if applied and not environment_failures:
        next_run, _created = create_validation_run(db, project, exclude_run_id=run.id)
    result = {
        "validation": True,
        "validation_applied": applied,
        "promoted": promoted,
        "demoted": demoted,
        "environment_failures": environment_failures,
        "platform_failures": platform_failures,
        "auto_repaired": auto_repaired,
        "defects": defects,
        "next_run_id": next_run.id if next_run is not None else None,
    }
    logger.info(
        "试运行完成：project=%s run=%s applied=%s promoted=%s demoted=%s next=%s",
        project.id,
        run.id,
        applied,
        promoted,
        demoted,
        result["next_run_id"],
    )
    return result


__all__ = [
    "MAX_CASES_PER_RUN",
    "active_validation_run",
    "apply_validation_outcome",
    "auto_enable_after_validation",
    "confirmed_assertions",
    "create_validation_run",
    "eligible_cases",
    "validation_eligible",
    "validation_enabled",
    "validation_preview",
]
