"""用例管理 API：审核队列、覆盖树、启用 / 停用。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.case_enrich import enqueue_case_enrichment, preview_case_enrichment
from app.case_lifecycle import collect_retirement_candidates, run_case_retirement
from app.case_validation import create_validation_run, validation_preview
from app.cli_contract import extract_contract, generate_from_contract
from app.case_state import (
    EXECUTION_STATE_PATTERN,
    apply_execution_state_filter,
    executed_case_ids,
    latest_source_parsed_at,
    states_for_case,
)
from app.case_verification import case_verification_history, record_case_verification
from app.db import get_db
from app.enums import CaseStatus, Dimension, FeedbackAction, FeedbackTarget
from app.errors import ConflictError, NotFoundError
from app.executor.case_schema import collect_assertions, validate_case_spec
from app.executor.risk import high_risk_reasons
from app.models import CaseResult, Feature, Finding, HumanFeedback, Project, Run, TestCase
from app.observed_schema import observed_schema_drift
from app.oracle_coverage import oracle_coverage
from app.probe_generator import create_probes, preview_probes
from app.risk_coverage import risk_coverage
from app.schemas import (
    BatchStatusUpdate,
    CaseEnrichBatchRequest,
    CaseStatusUpdate,
    CaseValidationRequest,
    CaseRepairRequest,
    CliGenerateRequest,
    CaseVerificationCreate,
    CaseVerificationOut,
    ProbeGenerateRequest,
    TestCaseCreate,
    TestCaseOut,
    TestCaseUpdate,
)
from app.worker.queue import enqueue

router = APIRouter(tags=["cases"])


def _case_out(
    db: Session,
    case: TestCase,
    feature_name: str | None = None,
    finding_ids: list[int] | None = None,
    *,
    execution_context: tuple[set[int], datetime | None] | None = None,
) -> TestCaseOut:
    # 断言是步骤级概念：历史数据只把顶层 assertions 写进 expected，
    # 这里统一汇总「步骤内联 + 顶层」，让 UI 的断言数与执行器实际判定一致。
    stored_expected = case.expected if isinstance(case.expected, dict) else {}
    expected = {**stored_expected, "assertions": collect_assertions(case.spec or {})}
    if execution_context is None:
        executed = executed_case_ids(db, case.project_id, [case.id])
        latest_parsed_at = latest_source_parsed_at(db, case.project_id)
    else:
        executed, latest_parsed_at = execution_context
    execution_states = states_for_case(
        case,
        executed=executed,
        latest_parsed_at=latest_parsed_at,
    )
    return TestCaseOut(
        id=case.id,
        project_id=case.project_id,
        feature_id=case.feature_id,
        title=case.title,
        dimension=case.dimension,
        case_type=case.case_type,
        status=case.status,
        generation=case.generation,
        priority=case.priority,
        tags=case.tags or [],
        preconditions=case.preconditions or {},
        steps=case.steps or [],
        expected=expected,
        evidence=case.evidence or [],
        spec=case.spec or {},
        missing_input=case.missing_input,
        verification_mode=case.verification_mode or "api",
        verification_status=case.verification_status or "unverified",
        verification=case.verification or {},
        verified_at=case.verified_at,
        verified_by=case.verified_by,
        feature_name=feature_name,
        finding_ids=finding_ids or [],
        execution_states=execution_states,
        created_at=case.created_at,
        updated_at=case.updated_at,
    )


def _finding_ids_map(db: Session, project_id: int, case_ids: list[int]) -> dict[int, list[int]]:
    if not case_ids:
        return {}
    rows = db.execute(
        select(Finding.id, Finding.case_id).where(
            Finding.project_id == project_id,
            Finding.case_id.in_(case_ids),
        )
    ).all()
    result: dict[int, list[int]] = {}
    for finding_id, case_id in rows:
        if case_id is not None:
            result.setdefault(case_id, []).append(finding_id)
    return result


def _record_feedback(
    db: Session,
    project_id: int,
    target_type: str,
    target_id: int,
    action: str,
    before: dict[str, Any],
    after: dict[str, Any],
    note: str = "",
) -> None:
    db.add(
        HumanFeedback(
            project_id=project_id,
            target_type=target_type,
            target_id=target_id,
            action=action,
            before=before,
            after=after,
            note=note,
        )
    )


@router.get("/projects/{project_id}/cases", response_model=list[TestCaseOut])
def list_cases(
    project_id: int,
    status: str | None = None,
    dimension: str | None = None,
    feature_id: int | None = None,
    q: str | None = None,
    execution_state: str | None = Query(
        default=None,
        pattern=EXECUTION_STATE_PATTERN,
        description="never_executed / enabled_no_result / pending_not_enabled / stale_after_source / executed",
    ),
    limit: int = Query(default=200, ge=0, le=5000, description="返回条数上限；全部用例视图需要传大值"),
    db: Session = Depends(get_db),
) -> list[TestCaseOut]:
    stmt = select(TestCase).where(TestCase.project_id == project_id)
    stmt = apply_execution_state_filter(stmt, db, project_id, execution_state)
    if status:
        stmt = stmt.where(TestCase.status == status)
    if dimension:
        stmt = stmt.where(TestCase.dimension == dimension)
    if feature_id:
        stmt = stmt.where(TestCase.feature_id == feature_id)
    if q:
        term = q.strip()
        raw_id = term.lstrip("#")
        if raw_id.isdigit():
            item_id = int(raw_id)
            linked_case_ids = select(Finding.case_id).where(
                Finding.project_id == project_id,
                Finding.id == item_id,
                Finding.case_id.is_not(None),
            )
            stmt = stmt.where(
                or_(
                    TestCase.id == item_id,
                    TestCase.id.in_(linked_case_ids),
                    TestCase.title.ilike(f"%{q}%"),
                )
            )
        else:
            stmt = stmt.where(TestCase.title.ilike(f"%{q}%"))
    cases = db.scalars(stmt.order_by(TestCase.feature_id, TestCase.dimension, TestCase.id).limit(limit)).all()
    names = _feature_names(db, project_id)
    finding_ids = _finding_ids_map(db, project_id, [case.id for case in cases])
    execution_context = (
        executed_case_ids(db, project_id, [case.id for case in cases]),
        latest_source_parsed_at(db, project_id),
    )
    return [
        _case_out(
            db,
            case,
            names.get(case.feature_id),
            finding_ids.get(case.id),
            execution_context=execution_context,
        )
        for case in cases
    ]


@router.get("/projects/{project_id}/cases/page")
def list_cases_page(
    project_id: int,
    status: str | None = None,
    dimension: str | None = None,
    feature_id: int | None = None,
    feature_ids: str | None = Query(
        default=None,
        description="逗号分隔的功能点 ID；与 feature_id 二选一，优先支持多选",
    ),
    module: str | None = None,
    q: str | None = None,
    execution_status: str | None = Query(
        default=None,
        pattern=EXECUTION_STATE_PATTERN,
        description="never/executed / never_executed / enabled_no_result / pending_not_enabled / stale_after_source",
    ),
    sort: str = Query(default="priority", pattern="^(priority|updated|created|id)$"),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """分页用例列表（审核工作台用）：返回条目、总数与各状态计数。"""
    from sqlalchemy import func

    base = select(TestCase).where(TestCase.project_id == project_id)
    if status:
        statuses = [item.strip() for item in status.split(',') if item.strip()]
        if len(statuses) == 1:
            base = base.where(TestCase.status == statuses[0])
        elif statuses:
            base = base.where(TestCase.status.in_(statuses))
    if dimension:
        base = base.where(TestCase.dimension == dimension)
    if feature_id:
        base = base.where(TestCase.feature_id == feature_id)
    if feature_ids:
        ids = [int(item) for item in feature_ids.split(",") if item.strip().isdigit()]
        if ids:
            base = base.where(TestCase.feature_id.in_(ids))
    if module:
        base = base.where(
            TestCase.feature_id.in_(select(Feature.id).where(Feature.project_id == project_id, Feature.module == module))
        )
    if q:
        term = q.strip()
        raw_id = term.lstrip("#")
        if raw_id.isdigit():
            item_id = int(raw_id)
            linked_case_ids = select(Finding.case_id).where(
                Finding.project_id == project_id,
                Finding.id == item_id,
                Finding.case_id.is_not(None),
            )
            base = base.where(
                or_(
                    TestCase.id == item_id,
                    TestCase.id.in_(linked_case_ids),
                    TestCase.title.ilike(f"%{q}%"),
                )
            )
        else:
            base = base.where(TestCase.title.ilike(f"%{q}%"))
    base = apply_execution_state_filter(base, db, project_id, execution_status)

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    sort_column = {
        "priority": TestCase.priority,
        "updated": TestCase.updated_at,
        "created": TestCase.created_at,
        "id": TestCase.id,
    }[sort]
    order_by = sort_column.desc() if order == "desc" else sort_column.asc()
    rows = db.scalars(
        base.order_by(
            TestCase.missing_input.is_(None).desc(),
            order_by,
            TestCase.id,
        )
        .offset(offset)
        .limit(limit)
    ).all()

    counts = {row[0]: row[1] for row in db.execute(
        select(TestCase.status, func.count()).where(TestCase.project_id == project_id).group_by(TestCase.status)
    ).all()}
    names = _feature_names(db, project_id)
    finding_ids = _finding_ids_map(db, project_id, [case.id for case in rows])
    execution_context = (
        executed_case_ids(db, project_id, [case.id for case in rows]),
        latest_source_parsed_at(db, project_id),
    )
    return {
        "items": [
            _case_out(
                db,
                case,
                names.get(case.feature_id),
                finding_ids.get(case.id),
                execution_context=execution_context,
            )
            for case in rows
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
        "counts": counts,
    }


@router.post("/projects/{project_id}/cases", response_model=TestCaseOut, status_code=201)
def create_case(project_id: int, payload: TestCaseCreate, db: Session = Depends(get_db)) -> TestCaseOut:
    spec = dict(payload.spec or {})
    spec.setdefault("title", payload.title)
    spec.setdefault("dimension", payload.dimension)
    spec.setdefault("type", payload.case_type)
    spec.setdefault("steps", [])
    spec.setdefault("assertions", [])
    errors = validate_case_spec(spec)
    if errors:
        raise NotFoundError(f"用例格式不合法：{errors[:3]}")
    case = TestCase(
        project_id=project_id,
        feature_id=payload.feature_id,
        title=payload.title,
        dimension=payload.dimension,
        case_type=payload.case_type,
        preconditions={"items": spec.get("preconditions") or []},
        steps=spec.get("steps") or [],
        expected={"assertions": collect_assertions(spec)},
        evidence=spec.get("evidence") or [],
        spec=spec,
        generation=payload.generation,
        status=payload.status,
        priority=payload.priority,
        tags=payload.tags,
        verification_mode=payload.verification_mode or ("frontend" if payload.case_type == "web" else "api"),
    )
    db.add(case)
    db.commit()
    db.refresh(case)
    _record_feedback(db, project_id, FeedbackTarget.CASE.value, case.id, FeedbackAction.CREATE.value, {}, {"title": case.title})
    db.commit()
    return _case_out(db, case)


@router.get("/cases/{case_id}", response_model=TestCaseOut)
def get_case(case_id: int, db: Session = Depends(get_db)) -> TestCaseOut:
    case = db.get(TestCase, case_id)
    if case is None:
        raise NotFoundError(f"用例不存在：{case_id}")
    names = _feature_names(db, case.project_id)
    finding_ids = _finding_ids_map(db, case.project_id, [case.id])
    return _case_out(db, case, names.get(case.feature_id), finding_ids.get(case.id))


@router.post("/cases/{case_id}/enrich")
def enrich_case(
    case_id: int,
    repair: bool = Query(default=False, description="按最近失败日志修复，修复后自动重新验证"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """把白盒建议用例补全为可执行用例（后台任务；repair=True 时自动重新验证）。"""
    case = db.get(TestCase, case_id)
    if case is None:
        raise NotFoundError(f"用例不存在：{case_id}")
    task = enqueue(
        db,
        "enrich_case",
        {"project_id": case.project_id, "case_id": case.id, "repair": repair},
        project_id=case.project_id,
    )
    db.commit()
    return {"task_id": task.id, "status": task.status}


@router.post("/cases/{case_id}/verification")
def report_case_verification(
    case_id: int,
    payload: CaseVerificationCreate,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """记录构建产物 / 进程 / 浏览器 / 权限等外部验证结果，并回写关联 finding。"""
    case = db.get(TestCase, case_id)
    if case is None:
        raise NotFoundError(f"用例不存在：{case_id}")
    try:
        record = record_case_verification(
            db,
            case,
            payload,
            actor=payload.actor or "manual",
            source="manual",
        )
    except ValueError as exc:
        raise ConflictError(str(exc)) from exc
    db.commit()
    db.refresh(case)
    db.refresh(record)
    return {
        "case": _case_out(db, case),
        "verification": CaseVerificationOut.model_validate(record).model_dump(mode="json"),
    }


@router.get("/cases/{case_id}/verification-history", response_model=list[CaseVerificationOut])
def get_case_verification_history(case_id: int, db: Session = Depends(get_db)) -> list[Any]:
    case = db.get(TestCase, case_id)
    if case is None:
        raise NotFoundError(f"用例不存在：{case_id}")
    return case_verification_history(db, case_id)


@router.get("/cases/{case_id}/latest-result")
def latest_case_result(case_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """某条用例最近一次执行结果（白盒建议用例补全/执行后用于回链）。"""
    case = db.get(TestCase, case_id)
    if case is None:
        raise NotFoundError(f"用例不存在：{case_id}")
    result = db.scalar(
        select(CaseResult)
        .where(CaseResult.case_id == case_id)
        .order_by(CaseResult.created_at.desc(), CaseResult.id.desc())
        .limit(1)
    )
    if result is None:
        return {"result": None, "run": None}
    run = db.get(Run, result.run_id)
    return {
        "result": {
            "id": result.id,
            "run_id": result.run_id,
            "case_id": result.case_id,
            "status": result.status,
            "created_at": result.created_at,
            "duration_ms": result.duration_ms,
            "attribution": result.attribution or {},
        },
        "run": (
            {"id": run.id, "status": run.status, "created_at": run.created_at}
            if run is not None
            else None
        ),
    }


@router.patch("/cases/{case_id}", response_model=TestCaseOut)
def update_case(case_id: int, payload: TestCaseUpdate, db: Session = Depends(get_db)) -> TestCaseOut:
    case = db.get(TestCase, case_id)
    if case is None:
        raise NotFoundError(f"用例不存在：{case_id}")
    before = {"title": case.title, "status": case.status, "spec": case.spec}
    data = payload.model_dump(exclude_unset=True)
    if "spec" in data and data["spec"] is not None:
        errors = validate_case_spec(data["spec"])
        if errors:
            raise NotFoundError(f"用例格式不合法：{errors[:3]}")
        case.spec = data["spec"]
        case.steps = data["spec"].get("steps") or []
        case.expected = {"assertions": collect_assertions(data["spec"])}
        case.evidence = data["spec"].get("evidence") or []
        case.preconditions = {"items": data["spec"].get("preconditions") or []}
        case.missing_input = data["spec"].get("missing_input") or None
    for key in ("title", "dimension", "feature_id", "status", "priority", "tags", "verification_mode"):
        if key in data and data[key] is not None:
            setattr(case, key, data[key])
    db.commit()
    db.refresh(case)
    _record_feedback(
        db,
        case.project_id,
        FeedbackTarget.CASE.value,
        case.id,
        FeedbackAction.MODIFY.value,
        before,
        {"title": case.title, "status": case.status, "spec": case.spec},
    )
    db.commit()
    return _case_out(db, case)


@router.delete("/cases/{case_id}", status_code=204)
def delete_case(case_id: int, db: Session = Depends(get_db)) -> None:
    case = db.get(TestCase, case_id)
    if case is None:
        raise NotFoundError(f"用例不存在：{case_id}")
    db.delete(case)
    db.commit()


def _clear_change_impact(case: TestCase) -> None:
    """人工启用视为已复核变更影响，清掉待复核标记，避免永久卡在 needs_review。"""
    spec = dict(case.spec or {})
    if not spec.get("change_impact") and not spec.get("change_candidates"):
        return
    spec.pop("change_impact", None)
    spec.pop("change_candidates", None)
    case.spec = spec
    case.tags = [tag for tag in (case.tags or []) if tag != "change-impact"]


@router.post("/cases/{case_id}/status", response_model=TestCaseOut)
def set_case_status(case_id: int, payload: CaseStatusUpdate, db: Session = Depends(get_db)) -> TestCaseOut:
    """启用 / 停用 / 标记待确认（停用不删除，修改记入 human_feedback）。"""
    case = db.get(TestCase, case_id)
    if case is None:
        raise NotFoundError(f"用例不存在：{case_id}")
    if payload.status == CaseStatus.ENABLED.value and not payload.confirm_risk:
        project = db.get(Project, case.project_id)
        reasons = high_risk_reasons(case.spec or {}, (project.settings_json or {}) if project else {})
        if reasons:
            raise ConflictError(
                "高风险操作需要人工确认才能启用：" + "；".join(reasons) + "（确认后请带 confirm_risk=true）",
                details={"reasons": reasons},
            )
    before = {"status": case.status}
    case.status = payload.status
    if payload.status == CaseStatus.ENABLED.value:
        _clear_change_impact(case)
    db.commit()
    db.refresh(case)
    action = {
        CaseStatus.ENABLED.value: FeedbackAction.ENABLE.value,
        CaseStatus.DISABLED.value: FeedbackAction.DISABLE.value,
    }.get(payload.status, FeedbackAction.MODIFY.value)
    _record_feedback(db, case.project_id, FeedbackTarget.CASE.value, case.id, action, before, {"status": case.status}, payload.note)
    db.commit()
    names = _feature_names(db, case.project_id)
    return _case_out(db, case, names.get(case.feature_id))


@router.post("/projects/{project_id}/cases/batch-status")
def batch_status(project_id: int, payload: BatchStatusUpdate, db: Session = Depends(get_db)) -> dict[str, Any]:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    cases = db.scalars(select(TestCase).where(TestCase.project_id == project_id, TestCase.id.in_(payload.case_ids))).all()
    if payload.status == CaseStatus.ENABLED.value and not payload.confirm_risk:
        risky: dict[int, list[str]] = {}
        for case in cases:
            reasons = high_risk_reasons(case.spec or {}, project.settings_json or {})
            if reasons:
                risky[case.id] = reasons
        if risky:
            raise ConflictError(
                "高风险操作需要人工确认才能启用：" + "；".join(f"#{cid} {r[0]}" for cid, r in risky.items()),
                details={"risky": risky},
            )
    action = {
        CaseStatus.ENABLED.value: FeedbackAction.ENABLE.value,
        CaseStatus.DISABLED.value: FeedbackAction.DISABLE.value,
    }.get(payload.status, FeedbackAction.MODIFY.value)
    for case in cases:
        before = {"status": case.status}
        case.status = payload.status
        if payload.status == CaseStatus.ENABLED.value:
            _clear_change_impact(case)
        _record_feedback(db, project_id, FeedbackTarget.CASE.value, case.id, action, before, {"status": payload.status}, payload.note)
    db.commit()
    return {"updated": len(cases)}


@router.get("/projects/{project_id}/cases/validation/preview")
def preview_validation(
    project_id: int,
    limit: int = Query(default=2000, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """有多少 pending 用例可以自动试运行，以及其余用例被什么条件挡住。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return validation_preview(db, project, limit=limit)


@router.post("/projects/{project_id}/cli-contract")
def build_cli_contract(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """探测 CLI help，生成命令目录（存 settings.cli_contract）。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    contract = extract_contract(project)
    db.commit()
    return {
        "project_id": project_id,
        "binary": contract.get("binary"),
        "commands": contract.get("commands"),
        "flags": len(contract.get("flags") or []),
        "error": contract.get("error"),
    }


@router.post("/projects/{project_id}/cases/cli-from-contract")
def cli_from_contract(
    project_id: int,
    payload: CliGenerateRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """只从 CLI 契约目录 + 固定模板生成用例，不允许自由拼 shell。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    result = generate_from_contract(
        db,
        project,
        limit=payload.limit,
        case_ids=payload.case_ids,
        dry_run=payload.dry_run,
    )
    if not payload.dry_run and result.get("case_ids"):
        run, _created = create_validation_run(
            db, project, case_ids=result["case_ids"], limit=len(result["case_ids"])
        )
        result["validation_run_id"] = run.id if run is not None else None
    db.commit()
    return result


@router.post("/projects/{project_id}/cases/cli-generate")
def cli_generate(
    project_id: int,
    payload: CliGenerateRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """用 onboarding profile + 已跑通的 CLI 示例，给无步骤用例生成 command 步骤。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    stmt = select(TestCase).where(
        TestCase.project_id == project_id,
        TestCase.status == CaseStatus.PENDING.value,
    )
    if payload.case_ids:
        stmt = stmt.where(TestCase.id.in_(payload.case_ids))
    cases = list(db.scalars(stmt.order_by(TestCase.priority, TestCase.id)).all())
    candidates = [case for case in cases if not case.steps][: payload.limit]
    if payload.dry_run:
        return {
            "project_id": project_id,
            "count": len(candidates),
            "items": [{"case_id": case.id, "title": case.title} for case in candidates],
        }
    task_ids: list[int] = []
    for case in candidates:
        task = enqueue(
            db,
            "enrich_case",
            {"project_id": project_id, "case_id": case.id, "cli_mode": True, "repair": True},
            project_id=project_id,
        )
        task_ids.append(task.id)
    db.commit()
    return {"project_id": project_id, "queued": len(task_ids), "task_ids": task_ids}


@router.post("/projects/{project_id}/cases/repair")
def repair_cases(
    project_id: int,
    payload: CaseRepairRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """对 needs_review 且有失败记录的用例做 AI 修复，修复后自动重新验证。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    stmt = select(TestCase).where(
        TestCase.project_id == project_id,
        TestCase.status == CaseStatus.NEEDS_REVIEW.value,
    )
    if payload.case_ids:
        stmt = stmt.where(TestCase.id.in_(payload.case_ids))
    cases = list(db.scalars(stmt.order_by(TestCase.priority, TestCase.id).limit(payload.limit)).all())
    candidates: list[tuple[TestCase, CaseResult]] = []
    for case in cases:
        failure = db.scalar(
            select(CaseResult)
            .where(
                CaseResult.case_id == case.id,
                CaseResult.status.in_(["fail", "error", "flaky"]),
                CaseResult.rerun.is_(False),
            )
            .order_by(CaseResult.created_at.desc())
            .limit(1)
        )
        if failure is not None:
            candidates.append((case, failure))
    if payload.dry_run:
        return {
            "project_id": project_id,
            "count": len(candidates),
            "items": [
                {
                    "case_id": case.id,
                    "title": case.title,
                    "last_status": failure.status,
                    "last_error": (failure.logs or "")[-300:],
                }
                for case, failure in candidates
            ],
        }
    task_ids: list[int] = []
    for case, _failure in candidates:
        cli_mode = any(
            isinstance(step, dict) and step.get("action") == "command"
            for step in (case.steps or [])
        )
        task = enqueue(
            db,
            "enrich_case",
            {"project_id": project_id, "case_id": case.id, "repair": True, "cli_mode": cli_mode},
            project_id=project_id,
        )
        task_ids.append(task.id)
    db.commit()
    return {"project_id": project_id, "queued": len(task_ids), "task_ids": task_ids}


@router.post("/projects/{project_id}/cases/validation/run")
def run_validation(
    project_id: int,
    payload: CaseValidationRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """创建一批试运行：有步骤 + 有依据断言的 pending 用例先跑一次。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    run, created = create_validation_run(db, project, case_ids=payload.case_ids, limit=payload.limit)
    db.commit()
    return {
        "run_id": run.id if run is not None else None,
        "created": created,
        "cases": len(run.case_ids or []) if run is not None else 0,
        "status": run.status if run is not None else None,
    }


@router.post("/projects/{project_id}/cases/enrich")
def batch_enrich_cases(
    project_id: int,
    payload: CaseEnrichBatchRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """按缺口/执行状态批量重新评估用例；默认只预览，不直接改用例。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    filters = payload.filters.model_dump()
    if payload.dry_run:
        return {
            "dry_run": True,
            **preview_case_enrichment(
                db,
                project,
                filters=filters,
                case_ids=payload.case_ids,
                limit=payload.limit,
            ),
        }
    result = enqueue_case_enrichment(
        db,
        project,
        filters=filters,
        case_ids=payload.case_ids,
        limit=payload.limit,
        actor=payload.actor,
        note=payload.note,
    )
    db.commit()
    return {"dry_run": False, **result}


@router.get("/projects/{project_id}/cases/retire")
def preview_case_retirement(
    project_id: int,
    stale_days: int = Query(default=30, ge=1, le=3650),
    include_missing_input: bool = Query(default=False),
    include_duplicates: bool = Query(default=True),
    include_enabled: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """预览过期 / 重复用例退役范围（不写库）。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    candidates = collect_retirement_candidates(
        db,
        project,
        stale_days=stale_days,
        include_missing_input=include_missing_input,
        include_duplicates=include_duplicates,
        include_enabled=include_enabled,
    )
    return {"dry_run": True, "count": len(candidates), "cases": candidates[:200]}


@router.post("/projects/{project_id}/cases/retire")
def retire_cases(
    project_id: int,
    dry_run: bool = Query(default=True),
    stale_days: int = Query(default=30, ge=1, le=3650),
    include_missing_input: bool = Query(default=False),
    include_duplicates: bool = Query(default=True),
    include_enabled: bool = Query(default=False),
    actor: str = Query(default="local"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """过期 / 重复用例退役；默认 dry_run=true，只预览。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    result = run_case_retirement(
        db,
        project,
        dry_run=dry_run,
        stale_days=stale_days,
        include_missing_input=include_missing_input,
        include_duplicates=include_duplicates,
        include_enabled=include_enabled,
        actor=actor,
    )
    if not dry_run:
        db.commit()
    return result


@router.get("/projects/{project_id}/cases/review-queue")
def review_queue(
    project_id: int,
    limit: int = Query(default=50, ge=0, le=200),
    db: Session = Depends(get_db),
) -> list[TestCaseOut]:
    """待审核队列：Agent 生成的用例默认 pending，人工启用后才参与执行。"""
    cases = db.scalars(
        select(TestCase)
        .where(TestCase.project_id == project_id, TestCase.status.in_([CaseStatus.PENDING.value, CaseStatus.NEEDS_REVIEW.value]))
        .order_by(TestCase.priority, TestCase.id)
        .limit(limit)
    ).all()
    names = _feature_names(db, project_id)
    execution_context = (
        executed_case_ids(db, project_id, [case.id for case in cases]),
        latest_source_parsed_at(db, project_id),
    )
    return [
        _case_out(db, case, names.get(case.feature_id), execution_context=execution_context)
        for case in cases
    ]


@router.get("/projects/{project_id}/coverage")
def coverage_tree(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """功能覆盖树：模块 → 功能点 → 用例（按维度统计），无覆盖标红。"""
    features = db.scalars(select(Feature).where(Feature.project_id == project_id).order_by(Feature.module, Feature.id)).all()
    cases = db.scalars(select(TestCase).where(TestCase.project_id == project_id)).all()
    by_feature: dict[int, list[TestCase]] = {}
    for case in cases:
        by_feature.setdefault(case.feature_id or 0, []).append(case)

    modules: dict[str, dict[str, Any]] = {}
    total_features = 0
    covered_features = 0
    for feature in features:
        module = feature.module or "未分组"
        bucket = modules.setdefault(module, {"module": module, "features": [], "total": 0, "covered": 0})
        feature_cases = by_feature.get(feature.id, [])
        dimensions: dict[str, int] = {d.value: 0 for d in Dimension}
        for case in feature_cases:
            dimensions[case.dimension] = dimensions.get(case.dimension, 0) + 1
        has_normal = dimensions.get(Dimension.FUNCTIONAL.value, 0) > 0
        total_features += 1
        covered = has_normal
        if covered:
            covered_features += 1
        bucket["total"] += 1
        bucket["covered"] += int(covered)
        bucket["features"].append(
            {
                "id": feature.id,
                "name": feature.name,
                "description": feature.description,
                "unmapped": feature.unmapped,
                "source_refs": feature.source_refs,
                "endpoint_ids": feature.endpoint_ids,
                "dimensions": dimensions,
                "total_cases": len(feature_cases),
                "enabled_cases": sum(1 for c in feature_cases if c.status == CaseStatus.ENABLED.value),
                "covered": covered,
                "missing_input": next((c.missing_input for c in feature_cases if c.missing_input), None),
            }
        )
    return {
        "project_id": project_id,
        "total_features": total_features,
        "covered_features": covered_features,
        "coverage_rate": round(covered_features / total_features, 4) if total_features else 0.0,
        "modules": list(modules.values()),
        # 契约覆盖只统计文档行为；风险覆盖单独统计鉴权 / 注入 / 幂等 / 隔离等类别。
        "risk": risk_coverage(cases),
        # 断言依据分层：契约 / 策略 / 人工 / 无依据，避免用 pending 断言虚高覆盖。
        "oracles": oracle_coverage(cases),
    }


@router.get("/projects/{project_id}/observed/schema-drift")
def observed_schema(
    project_id: int,
    min_samples: int = Query(default=2, ge=2, le=20),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """observed 层：真实响应结构相对历史基线的漂移，只作风险线索，不直接判缺陷。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目 {project_id} 不存在")
    return observed_schema_drift(db, project, min_samples=min_samples)


@router.post("/projects/{project_id}/probes/generate")
def generate_probes(
    project_id: int,
    payload: ProbeGenerateRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """通用 probe：文档缺失也能发起越权 / 注入 / 分页等高风险输入观测。"""
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目 {project_id} 不存在")
    if payload.dry_run:
        return {"dry_run": True, **preview_probes(db, project, endpoint_ids=payload.endpoint_ids, limit=payload.limit)}
    return {"dry_run": False, **create_probes(db, project, endpoint_ids=payload.endpoint_ids, limit=payload.limit)}


@router.post("/projects/{project_id}/cases/generate")
def generate_cases(
    project_id: int,
    feature_id: int | None = None,
    dimensions: list[str] | None = Query(default=None),
    only_missing: bool = Query(default=False, description="只给没有任何用例的功能点生成"),
    only_missing_dimensions: bool = Query(
        default=False,
        description="只生成功能点缺失的维度，避免已有维度重复生成",
    ),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    task = enqueue(
        db,
        "generate_cases",
        {
            "project_id": project_id,
            "feature_id": feature_id,
            "dimensions": dimensions,
            "only_missing": only_missing,
            "only_missing_dimensions": only_missing_dimensions,
        },
        project_id=project_id,
    )
    db.commit()
    return {"task_id": task.id, "status": task.status}


@router.get("/cases/{case_id}/feedback")
def case_feedback(case_id: int, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(HumanFeedback)
        .where(HumanFeedback.target_type == FeedbackTarget.CASE.value, HumanFeedback.target_id == case_id)
        .order_by(HumanFeedback.created_at.desc())
    ).all()
    return [
        {
            "id": r.id,
            "action": r.action,
            "note": r.note,
            "before": r.before,
            "after": r.after,
            "created_at": r.created_at,
        }
        for r in rows
    ]


def _feature_names(db: Session, project_id: int) -> dict[int, str]:
    rows = db.execute(
        select(Feature.id, Feature.name).where(Feature.project_id == project_id)
    ).all()
    return {row[0]: row[1] for row in rows}
