"""任务处理器：每个 kind 对应一个状态机步骤。"""
from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.agents import (
    attribute_failure,
    enrich_suggested_case,
    extract_features,
    generate_cases,
    whitebox_review,
)
from app.agent.triage import triage_findings
from app.agent.providers import get_provider
from app.config import get_settings
from app.settings_store import effective_settings
from app import failure_attribution
from app.enums import (
    AttributionCategory,
    CaseResultStatus,
    CaseStatus,
    FindingSource,
    RunStatus,
    RunTrigger,
    Severity,
    TaskKind,
)
from app.exceptions import record_exception
from app.spec_repair import repair_case_spec
from app.executor.environment import cleanup_environment, prepare_environment
from app.executor.normalizer import load_endpoint_schemas
from app.executor.runner import build_curl, persist_results, run_cases_in_sandbox, select_cases
from app.fixes import (
    FIX_CLAIMED,
    FIX_NEEDS_HUMAN,
    FIX_REOPENED,
    mark_needs_external,
    mark_needs_human,
)
from app.logging import get_logger
from app.models import CaseResult, Feature, Finding, KnowledgeSource, Project, Run, Task, TestCase
from app.verification_evidence import record_replay_evidence
from app.worker.queue import enqueue, has_active_task, heartbeat

logger = get_logger("shakedown.worker.handlers")


def _project(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"项目不存在：{project_id}")
    return project


# ── 功能点抽取 ──────────────────────────────────────────────
def handle_plan_features(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    project = _project(session, int(payload["project_id"]))
    provider = get_provider()
    features = extract_features(
        session,
        project,
        provider,
        allow_orphan_cases=bool(payload.get("allow_orphan_cases", False)),
    )
    session.commit()
    result: dict[str, Any] = {
        "features": len(features),
        "unmapped": sum(1 for f in features if f.unmapped),
    }
    # 新项目 bootstrap：抽完功能点直接接生成，避免人停在「抽完但没用例」这一步。
    # 只在项目还没有任何用例时触发；重新解析已有项目的功能点仍走 change_impact。
    automation = (project.settings_json or {}).get("automation")
    automation = automation if isinstance(automation, dict) else {}
    case_count = session.scalar(
        select(func.count()).select_from(TestCase).where(TestCase.project_id == project.id)
    ) or 0
    if (
        features
        and case_count == 0
        and automation.get("auto_generate_on_bootstrap", True)
        and not has_active_task(session, project_id=project.id, kind=TaskKind.GENERATE_CASES.value)
    ):
        task = enqueue(
            session,
            TaskKind.GENERATE_CASES.value,
            {"project_id": project.id, "only_missing": True},
            project_id=project.id,
        )
        session.commit()
        result["generate"] = {"task_id": task.id, "status": task.status}
        logger.info("功能点抽取完成，自动入队生成用例：project=%s task=%s", project.id, task.id)
    return result


# ── 用例生成 ────────────────────────────────────────────────
def handle_generate_cases(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    project = _project(session, int(payload["project_id"]))
    provider = get_provider()
    feature = None
    if payload.get("feature_id"):
        feature = session.get(Feature, int(payload["feature_id"]))
    dimensions = payload.get("dimensions") or None
    only_missing = bool(payload.get("only_missing"))
    selected_features: list[Feature] | None = None
    if only_missing:
        # 只补“没有任何用例”的功能点，避免给已有用例的功能点重复生成
        missing_stmt = (
            select(Feature)
            .where(Feature.project_id == project.id)
            .where(~select(TestCase.id).where(TestCase.feature_id == Feature.id).exists())
            .order_by(Feature.id)
        )
        if feature is not None:
            missing_stmt = missing_stmt.where(Feature.id == feature.id)
        selected_features = list(session.scalars(missing_stmt).all())
        feature = None
    task_id = payload.get("_task_id")
    settings = get_settings()

    checkpoint = {}
    if task_id:
        task = session.get(Task, int(task_id))
        if task is not None:
            checkpoint = dict(task.checkpoint or {})
    previous = dict(checkpoint.get("generate") or {})
    done_keys = set(previous.get("done") or [])
    failed_keys = set(previous.get("failed") or [])
    state: dict[str, Any] = {
        "total": int(previous.get("total") or 0),
        "done": len(done_keys),
        "failed": len(failed_keys),
        "cases": 0,
    }

    def should_cancel() -> bool:
        if not task_id:
            return False
        status = session.scalar(select(Task.status).where(Task.id == int(task_id)))
        return status == "cancelled"

    def on_progress(event: dict[str, Any]) -> None:
        state["total"] = int(event.get("total") or state["total"] or 0)
        state["cases"] = int(event.get("cases") or state["cases"] or 0)
        state["module"] = event.get("module") or state.get("module", "")
        if event.get("keys") is not None:
            # 功能点/批次可能变化，清掉 checkpoint 里已经不存在的旧 key
            current_keys = set(event["keys"])
            state["keys"] = current_keys
            done_keys.intersection_update(current_keys)
            failed_keys.intersection_update(current_keys)
        key = event.get("key")
        if key:
            if event.get("ok"):
                done_keys.add(key)
                failed_keys.discard(key)
            else:
                failed_keys.add(key)
        state["done"] = len(done_keys)
        state["failed"] = len(failed_keys)
        if not task_id:
            return
        task = session.get(Task, int(task_id))
        if task is not None:
            task.checkpoint = {
                **(task.checkpoint or {}),
                "generate": {
                    "total": state["total"],
                    "done": sorted(done_keys),
                    "failed": sorted(failed_keys),
                    "done_count": len(done_keys),
                    "failed_count": len(failed_keys),
                    "cases": state["cases"],
                    "module": state.get("module", ""),
                    "cancelled": bool(event.get("cancelled")),
                },
            }
            # 长任务持续续租，避免 900s 租约过期后被其它 worker 重复领取
            heartbeat(session, task)

    cases = generate_cases(
        session,
        project,
        provider,
        feature=feature,
        features=selected_features,
        dimensions=dimensions,
        completed_keys=done_keys,
        should_cancel=should_cancel,
        on_progress=on_progress,
        job_timeout=float(settings.llm_job_timeout),
        concurrency=settings.llm_concurrency,
        trace_task_id=int(task_id) if task_id else None,
        only_missing_dimensions=bool(payload.get("only_missing_dimensions")),
    )
    session.commit()
    # 新用例能判定的先跑一次：通过即启用，失败进 needs_review + 例外。
    # 只对本次新生成的用例生效；已有 pending 的补齐走 validate-pending API。
    validation: dict[str, Any] = {}
    if cases:
        from app.case_validation import create_validation_run

        try:
            run, created = create_validation_run(session, project, case_ids=[case.id for case in cases])
            session.commit()
            validation = {
                "run_id": run.id if run is not None else None,
                "created": created,
                "cases": len(run.case_ids or []) if run is not None else 0,
            }
        except Exception:  # noqa: BLE001 —— 试运行入队失败不能把已生成的用例标成失败
            session.rollback()
            logger.exception("试运行入队失败：project=%s", project.id)
            validation = {"error": "试运行入队失败"}
    cancelled = should_cancel()
    if not cancelled and failed_keys and state.get("keys"):
        # 不要静默成功：把失败批次留给 worker 自动重试，已完成批次继续跳过
        raise RuntimeError(
            f"{len(failed_keys)}/{state['total']} 个生成批次失败"
            f"（已完成 {len(done_keys)}），任务将自动重试失败批次"
        )
    return {
        "cases": len(cases),
        "batches_done": len(done_keys),
        "batches_failed": len(failed_keys),
        "batches_total": state["total"],
        "cancelled": cancelled,
        "validation": validation,
    }


# ── 知识源重解析 ────────────────────────────────────────────
def handle_reparse_source(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    from app.ingest.service import ingest_source

    source = session.get(KnowledgeSource, int(payload["source_id"]))
    if source is None:
        raise ValueError("知识源不存在")
    project = session.get(Project, source.project_id)
    meta = ingest_source(session, project, source)
    return {
        "version": meta.get("version", ""),
        "chunks": meta.get("chunks", 0),
        "change_impact": meta.get("change_impact") or {},
        "reevaluate_candidates": meta.get("reevaluate_candidates", 0),
    }


# ── 白盒走查 ────────────────────────────────────────────────
def handle_whitebox(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    project = _project(session, int(payload["project_id"]))
    provider = get_provider()
    task_id = payload.get("_task_id")

    previous: dict[str, Any] = {}
    if task_id:
        task = session.get(Task, int(task_id))
        if task is not None:
            previous = dict((task.checkpoint or {}).get("whitebox") or {})
    resume_checkpoint = dict(payload.get("resume_checkpoint") or {})
    completed_modules = set(previous.get("done_modules") or resume_checkpoint.get("done_modules") or [])
    state = {"done": len(completed_modules)}

    def should_cancel() -> bool:
        if not task_id:
            return False
        status = session.scalar(select(Task.status).where(Task.id == int(task_id)))
        return status == "cancelled"

    def progress(done: int, total: int, module: str, done_modules: set[str]) -> None:
        state["done"] = done
        if not task_id:
            return
        task = session.get(Task, int(task_id))
        if task is None:
            return
        task.checkpoint = {
            **(task.checkpoint or {}),
            "whitebox": {
                "done": done,
                "total": total,
                "module": module,
                "done_modules": sorted(done_modules),
            },
        }
        # 每个模块结束都落库并续租，停止后已完成模块不会丢
        heartbeat(session, task)

    findings = whitebox_review(
        session,
        project,
        provider,
        on_progress=progress,
        completed_modules=completed_modules,
        should_cancel=should_cancel,
    )
    session.commit()
    cancelled = should_cancel()
    return {
        "findings": len(findings),
        "modules_done": state["done"],
        "cancelled": cancelled,
    }


def handle_enrich_case(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    project = _project(session, int(payload["project_id"]))
    case = session.get(TestCase, int(payload["case_id"]))
    if case is None:
        raise ValueError(f"用例不存在：{payload['case_id']}")
    provider = get_provider()
    failure = session.scalar(
        select(CaseResult)
        .where(
            CaseResult.case_id == case.id,
            CaseResult.status.in_(["fail", "error", "flaky"]),
            CaseResult.rerun.is_(False),
        )
        .order_by(CaseResult.created_at.desc())
        .limit(1)
    )
    case = enrich_suggested_case(
        session,
        project,
        provider,
        case,
        failure=failure,
        cli_mode=bool(payload.get("cli_mode")),
    )
    auto_enabled = False
    automation = (project.settings_json or {}).get("automation")
    if isinstance(automation, dict) and automation.get("auto_enable"):
        priority_max = int(automation.get("auto_enable_priority_max", 100))
        if (
            case.status == "pending"
            and case.steps
            and not case.missing_input
            and int(case.priority or 100) <= priority_max
        ):
            from app.executor.risk import high_risk_reasons

            if not high_risk_reasons(case.spec or {}, project.settings_json or {}):
                case.status = "enabled"
                auto_enabled = True
    validation_run_id = None
    if payload.get("repair") or payload.get("cli_mode"):
        from app.case_validation import confirmed_assertions, create_validation_run

        if case.steps and confirmed_assertions(case.spec or {}):
            # 修复后立刻重新验证：通过就自动启用，失败再次进 needs_review
            case.status = "pending"
            session.flush()
            run, _created = create_validation_run(session, project, case_ids=[case.id], limit=1)
            if run is not None and case.id in (run.case_ids or []):
                validation_run_id = run.id
    session.commit()
    return {
        "case_id": case.id,
        "steps": len(case.steps or []),
        "assertions": len((case.expected or {}).get("assertions") or []),
        "missing_input": case.missing_input,
        "auto_enabled": auto_enabled,
        "failure_used": failure is not None,
        "cli_mode": bool(payload.get("cli_mode")),
        "validation_run_id": validation_run_id,
    }


def handle_performance_run(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    from app.performance import persist_performance, run_performance

    project = _project(session, int(payload["project_id"]))
    targets = payload.get("targets") if isinstance(payload.get("targets"), list) else []
    results: list[dict[str, Any]] = []
    for target in targets[:20]:
        if not isinstance(target, dict) or not target.get("path"):
            continue
        try:
            result = run_performance(
                project,
                method=str(target.get("method") or "GET"),
                path=str(target["path"]),
                concurrency=int(target.get("concurrency", 5)),
                iterations=int(target.get("iterations", 20)),
                duration_seconds=target.get("duration_seconds"),
                headers=target.get("headers") or {},
                query=target.get("query") or {},
                body=target.get("body"),
                timeout=float(target.get("timeout", 10)),
            )
            row = persist_performance(session, project, result)
            results.append({"id": row.id, "path": result["path"], "slo": result.get("slo")})
        except Exception as exc:  # noqa: BLE001
            results.append({"path": target.get("path"), "error": str(exc)})
    session.commit()
    if results:
        try:
            from app.push import queue_event

            queue_event(session, project, "performance.completed", {"results": results})
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
    return {"project_id": project.id, "targets": len(results), "results": results}


def handle_triage_findings(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    project = _project(session, int(payload["project_id"]))
    provider = get_provider()
    task_id = payload.get("_task_id")
    source = str(payload.get("source") or "whitebox")

    def progress(done: int, total: int, module: str) -> None:
        if not task_id:
            return
        task = session.get(Task, int(task_id))
        if task is None:
            return
        task.checkpoint = {
            **(task.checkpoint or {}),
            "triage": {"done": done, "total": total, "module": module},
        }
        session.commit()

    result = triage_findings(session, project, provider, source=source, on_progress=progress)
    from app.triage_policy import apply_triage_policy

    result.update(apply_triage_policy(session, project))
    session.commit()
    return result


def handle_verify_fixes(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    """自动验证已有修复声明的 finding。

    只信 Shakedown 自己的执行结果：开发 Agent 的 external report 只入证据库，
    不参与 verified/reopened 判定。没有可执行用例时转人工/外部验证。
    """
    from app.fixes import verify_fix as apply_verify_fix
    from app.schemas import FindingFixVerify

    project = _project(session, int(payload["project_id"]))
    finding_ids = payload.get("finding_ids") or []
    stmt = select(Finding).where(
        Finding.project_id == project.id,
        Finding.fix_status.in_([FIX_CLAIMED, FIX_REOPENED, FIX_NEEDS_HUMAN, "needs_external_verification"]),
    )
    if finding_ids:
        stmt = stmt.where(Finding.id.in_(finding_ids))
    findings = list(session.scalars(stmt).all())
    if not findings:
        return {"verified": 0, "reopened": 0, "needs_human": 0}

    cases = {
        case.id: case
        for case in session.scalars(
            select(TestCase).where(TestCase.id.in_([f.case_id for f in findings if f.case_id]))
        ).all()
    }
    executable = [
        case
        for case in cases.values()
        if (
            case.status == "enabled"
            and (case.steps or [])
            and case.case_type in {"api", "web"}
            and (case.verification_mode or "api") in {"api", "frontend"}
        )
    ]
    results: dict[int, str] = {}
    run_id = None
    if executable:
        run = Run(
            project_id=project.id,
            trigger="verify",
            status="queued",
            case_ids=[case.id for case in executable],
            env_snapshot={},
        )
        session.add(run)
        session.flush()
        session.commit()
        run_id = run.id
        # 直接复用执行状态机，跑完再读取结果。
        handle_execute_run(session, {"run_id": run.id})
        rows = session.scalars(
            select(CaseResult).where(CaseResult.run_id == run.id, CaseResult.rerun.is_(False))
        ).all()
        results = {}
        for row in rows:
            status_value = row.status
            code = (row.response or {}).get("status_code") if isinstance(row.response, dict) else None
            # 重放时即使没有断言，5xx 也不能算通过（环境/实现已明确失败）
            if status_value == CaseResultStatus.PASS.value and isinstance(code, int) and code >= 500:
                status_value = CaseResultStatus.ERROR.value
            results[row.case_id] = status_value

    verified = reopened = needs_human = needs_external = 0
    for finding in findings:
        case = cases.get(finding.case_id or 0)
        result = results.get(case.id) if case else None
        if result == CaseResultStatus.PASS.value:
            evidence = record_replay_evidence(
                session, case, finding, passed=True, result="PASS"
            )
            apply_verify_fix(
                session,
                finding,
                FindingFixVerify(
                    verified=True,
                    note="Shakedown 独立重放通过",
                    test_result="PASS",
                    verification_id=evidence.id,
                ),
                actor="auto",
                source="auto",
            )
            verified += 1
        elif result in {CaseResultStatus.FAIL.value, CaseResultStatus.ERROR.value}:
            evidence = record_replay_evidence(
                session, case, finding, passed=False, result=result
            )
            apply_verify_fix(
                session,
                finding,
                FindingFixVerify(
                    verified=False,
                    note=f"Shakedown 独立重放未通过：{result}",
                    test_result=result,
                    verification_id=evidence.id,
                ),
                actor="auto",
                source="auto",
            )
            reopened += 1
        elif case and (case.verification_mode or "api") == "manual":
            mark_needs_human(
                session,
                finding,
                note="该用例被标记为只能人工验证。",
                actor="auto",
                source="auto",
            )
            needs_human += 1
        else:
            mode = (case.verification_mode if case else "api") or "api"
            mark_needs_external(
                session,
                finding,
                note=f"当前验证方式为 {mode}，需要外部 Agent 执行构建/进程/产物/浏览器验证。",
                mode=mode,
                actor="auto",
                source="auto",
            )
            needs_external += 1
    session.commit()
    return {
        "run_id": run_id,
        "verified": verified,
        "reopened": reopened,
        "needs_human": needs_human,
        "needs_external": needs_external,
    }


# ── 执行（状态机）───────────────────────────────────────────
def handle_execute_run(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    run = session.get(Run, int(payload["run_id"]))
    if run is None:
        raise ValueError("执行记录不存在")
    if run.status == RunStatus.CANCELLED.value:
        return {"run_id": run.id, "cancelled": True}
    project = session.get(Project, run.project_id)
    settings = effective_settings()
    artifacts_dir = str(settings.artifacts_dir / f"run-{run.id}")
    checkpoint = dict(run.__dict__.get("_checkpoint") or {}) if False else {}
    is_validation = run.trigger == RunTrigger.VALIDATION.value

    # ── 1. 准备环境 ──────────────────────────────────────
    if run.status in {RunStatus.QUEUED.value, RunStatus.PREPARING.value}:
        run.status = RunStatus.PREPARING.value
        run.started_at = run.started_at or _utcnow()
        session.commit()
        env = prepare_environment(session, project, run)
        run.env_snapshot = {**(run.env_snapshot or {}), **env.as_dict(), "base_url": env.base_url}
        run.berth_task_id = env.berth_task_id or run.berth_task_id
        session.commit()
        checkpoint = {"env": env.as_dict(), "dsn": env.target_dsn, "accounts": env.accounts}
    else:
        from app.executor.environment import Environment

        snapshot = run.env_snapshot or {}
        env = Environment(
            base_url=snapshot.get("base_url", ""),
            target_dsn=snapshot.get("dsn", "") or (run.env_snapshot or {}).get("target_dsn", ""),
            accounts=snapshot.get("accounts", {}) or {},
            db_service=snapshot.get("db_service", ""),
        )

    # ── 2. 执行 ──────────────────────────────────────────
    if run.status in {RunStatus.PREPARING.value, RunStatus.RUNNING.value}:
        run.status = RunStatus.RUNNING.value
        session.commit()
        cases = select_cases(session, project, run)
        if not cases:
            run.status = RunStatus.COMPLETED.value
            run.stats = {"total": 0, "note": "没有启用的用例"}
            run.finished_at = _utcnow()
            session.commit()
            return {"executed": 0}
        endpoint_schemas = load_endpoint_schemas(session, project.id)

        def should_cancel() -> bool:
            from app.db import SessionLocal

            with SessionLocal() as probe:
                current = probe.get(Run, run.id)
                return current is not None and current.status == RunStatus.CANCELLED.value

        outcome = run_cases_in_sandbox(
            project,
            run,
            cases,
            env,
            artifacts_dir=artifacts_dir,
            sandbox_mode=settings.sandbox_mode,
            endpoint_schemas=endpoint_schemas,
            should_cancel=should_cancel,
        )
        rows = persist_results(session, project, run, cases, outcome)
        session.commit()
        # 用户在沙箱执行期间点了取消：保留已收集结果，但不再重跑 / 归因
        session.refresh(run)
        if run.status == RunStatus.CANCELLED.value:
            cleanup_environment(project, env)
            return {"run_id": run.id, "cancelled": True, "executed": len(rows)}
        sandbox_info = {
            "task_id": outcome.sandbox_task_id,
            "error": outcome.error,
            "stderr_tail": (outcome.stderr or "")[-600:],
        }

        # 试运行只跑一次（用户语义：跑一次看能不能用），不做失败重跑
        flaky: list[int] = []
        if not is_validation:
            flaky = _rerun_failures(
                session,
                project,
                run,
                cases,
                env,
                artifacts_dir,
                settings,
                endpoint_schemas,
                should_cancel=should_cancel,
            )
        session.commit()
        session.refresh(run)
        if run.status == RunStatus.CANCELLED.value:
            cleanup_environment(project, env)
            return {"run_id": run.id, "cancelled": True, "executed": len(rows)}
        run.stats = {**_compute_stats(session, run), "sandbox": sandbox_info}
        session.commit()
        checkpoint = {**checkpoint, "executed": len(rows), "flaky": flaky}

    # ── 3. 分析 ──────────────────────────────────────────
    if run.status in {RunStatus.RUNNING.value, RunStatus.ANALYZING.value}:
        session.refresh(run)
        if run.status == RunStatus.CANCELLED.value:
            cleanup_environment(project, env)
            return {"run_id": run.id, "cancelled": True}
        run.status = RunStatus.ANALYZING.value
        session.commit()
        if is_validation:
            # 试运行验证的是「用例能不能用」，不是「产品有没有 bug」：
            # 不跑 LLM 归因、不生成 finding，只把结果落到用例状态。
            from app.case_validation import apply_validation_outcome

            outcome = apply_validation_outcome(session, project, run)
            run.stats = {**(run.stats or {}), **outcome}
        else:
            analysis = analyze_run(session, project, run)
            run.stats = {**(run.stats or {}), **analysis}
        session.commit()

    # ── 4. 收尾 ──────────────────────────────────────────
    cleanup_environment(project, env)
    run.status = RunStatus.COMPLETED.value
    run.finished_at = _utcnow()
    session.commit()
    target = project.target_start if isinstance(project.target_start, dict) else {}
    if run.trigger == "schedule" and target.get("auto_stop"):
        try:
            from app.environment_manager import stop_environment

            stop_environment(project)
        except Exception:  # noqa: BLE001 —— 环境停止失败不影响 run 结果
            logger.warning("计划任务结束后停止环境失败：project=%s", project.id)
    if not is_validation:
        try:
            from app.push import queue_digest

            queue_digest(session, project)
            session.commit()
        except Exception:  # noqa: BLE001 - 推送失败不能影响执行主流程
            session.rollback()
    return {"run_id": run.id, "stats": run.stats}


def _rerun_failures(
    session: Session,
    project: Project,
    run: Run,
    cases: list[TestCase],
    env: Any,
    artifacts_dir: str,
    settings: Any,
    endpoint_schemas: dict[str, dict[str, Any]] | None = None,
    should_cancel: Any = None,
) -> list[int]:
    first = {
        row.case_id: row
        for row in session.scalars(select(CaseResult).where(CaseResult.run_id == run.id, CaseResult.rerun.is_(False))).all()
    }
    failing = [c for c in cases if first.get(c.id) and first[c.id].status in {CaseResultStatus.FAIL.value, CaseResultStatus.ERROR.value}]
    if not failing:
        return []
    outcome = run_cases_in_sandbox(
        project,
        run,
        failing,
        env,
        artifacts_dir=artifacts_dir,
        sandbox_mode=settings.sandbox_mode,
        attempt=1,
        endpoint_schemas=endpoint_schemas,
        should_cancel=should_cancel,
    )
    rows = persist_results(session, project, run, failing, outcome, rerun=True)
    flaky: list[int] = []
    by_case = {case.id: case for case in cases}
    for row in rows:
        original = first.get(row.case_id)
        if original is None:
            continue
        if row.status != original.status:
            # 两次不一致 → flaky，不报缺陷；按配置累计并隔离
            original.status = CaseResultStatus.FLAKY.value
            original.output = {**(original.output or {}), "flaky": True, "rerun_status": row.status}
            case = by_case.get(row.case_id)
            if case is not None:
                from app.flaky import record_flaky_result

                record_flaky_result(session, project, case, rerun_status=row.status)
            flaky.append(row.case_id)
    return flaky


def _compute_stats(session: Session, run: Run) -> dict[str, Any]:
    rows = session.scalars(select(CaseResult).where(CaseResult.run_id == run.id, CaseResult.rerun.is_(False))).all()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    total = len(rows)
    passed = counts.get(CaseResultStatus.PASS.value, 0)
    return {
        "total": total,
        "passed": passed,
        "failed": counts.get(CaseResultStatus.FAIL.value, 0),
        "flaky": counts.get(CaseResultStatus.FLAKY.value, 0),
        "errored": counts.get(CaseResultStatus.ERROR.value, 0),
        "skipped": counts.get(CaseResultStatus.SKIPPED.value, 0),
        "pass_rate": round(passed / total, 4) if total else 0.0,
    }


# ── 失败归因 + 缺陷生成 ────────────────────────────────────
def analyze_run(session: Session, project: Project, run: Run) -> dict[str, Any]:
    provider = get_provider()
    rows = session.scalars(
        select(CaseResult).where(CaseResult.run_id == run.id, CaseResult.rerun.is_(False)).order_by(CaseResult.seq)
    ).all()
    defects = 0
    updated_findings = 0
    auto_confirmed = 0
    repaired = 0
    attributed: list[dict[str, Any]] = []
    for row in rows:
        session.refresh(run)
        if run.status == RunStatus.CANCELLED.value:
            break
        if row.status not in {CaseResultStatus.FAIL.value, CaseResultStatus.ERROR.value}:
            continue
        case = session.get(TestCase, row.case_id)
        if case is None:
            continue
        attribution = attribute_failure(session, project, provider, case, row)
        category = str(attribution.get("category") or "")
        if category == AttributionCategory.PLATFORM.value and attribution.get("fixable"):
            changes = repair_case_spec(case, (row.output or {}).get("commands"))
            if changes:
                attribution = {
                    **attribution,
                    "auto_repaired": True,
                    "repair_changes": changes,
                    "reason": f"{attribution.get('reason') or ''}（已自动修正断言类型，下次执行生效）",
                }
                repaired += 1
        row.attribution = attribution
        attributed.append(attribution)
        if category == AttributionCategory.DEFECT.value:
            try:
                finding, created = _create_finding(session, project, run, case, row, attribution)
                defects += int(created)
                if not created:
                    updated_findings += 1
                if attribution.get("auto_confirm") and finding.status == "confirmed":
                    auto_confirmed += 1
            except Exception as exc:  # noqa: BLE001 - 单条缺陷写入失败不应中断整次归因
                logger.exception("生成缺陷失败：result=%s", row.id)
                row.attribution = {**attribution, "finding_error": str(exc)[:500]}
        elif category == AttributionCategory.CASE.value:
            _reconcile_case_finding(session, project, case, attribution)
            if float(attribution.get("confidence") or 0) >= 0.85:
                case.status = CaseStatus.NEEDS_REVIEW.value
        elif category == AttributionCategory.PLATFORM.value:
            record_exception(
                session,
                project,
                kind="platform_capability_gap",
                target_type="case",
                target_id=case.id,
                severity="medium",
                note=f"平台无法正确执行该用例（{attribution.get('reason_label') or ''}）：{attribution.get('reason') or ''}"[:1000],
                payload={
                    "run_id": run.id,
                    "result_id": row.id,
                    "reason_code": attribution.get("reason_code"),
                    "reason": attribution.get("reason"),
                    "fixable": bool(attribution.get("fixable")),
                    "auto_repaired": bool(attribution.get("auto_repaired")),
                },
            )
        elif category == AttributionCategory.ENVIRONMENT.value:
            record_exception(
                session,
                project,
                kind="environment_issue",
                target_type="run",
                target_id=run.id,
                severity="medium",
                note=f"本次执行存在环境问题（{attribution.get('reason_label') or ''}）：{attribution.get('reason') or ''}"[:1000],
                payload={"run_id": run.id, "result_id": row.id, "case_id": case.id},
            )
    session.flush()
    return {
        "findings_created": defects,
        "findings_updated": updated_findings,
        "findings_confirmed": auto_confirmed,
        "auto_repaired": repaired,
        "analyzed": len(rows),
        "attribution": failure_attribution.summarize(attributed),
    }


def _reconcile_case_finding(
    session: Session,
    project: Project,
    case: TestCase,
    attribution: dict[str, Any],
) -> None:
    """最新归因明确判为用例问题时，关闭同用例的旧 pending finding。

    之前 pending finding 会一直挂在列表里，和新 run 的结论不一致。
    只处理同用例、同来源的 pending 记录，人工确认过的记录不覆盖。
    """
    existing = session.scalar(
        select(Finding)
        .where(
            Finding.project_id == project.id,
            Finding.case_id == case.id,
            Finding.source == FindingSource.EXECUTION.value,
            Finding.status == "pending",
        )
        .order_by(Finding.id.desc())
        .limit(1)
    )
    if existing is None:
        return
    note = str(attribution.get("reason") or "最新执行归因判定为用例问题")[:2000]
    existing.status = "false_positive"
    existing.confirmed_by = "auto:reconcile"
    existing.confirmed_at = _utcnow()
    existing.description = (
        (existing.description or "")
        + "\n\n【自动复核】最新执行归因判定该失败为用例/预期问题，已关闭此前的疑似缺陷。\n"
        + note
    )[:4000]


def _request_chain(row: CaseResult) -> str:
    """把失败前的请求序列拼成一行，单条 curl 之外还能看到前置步骤。"""
    requests = (row.output or {}).get("requests") or []
    steps = [
        f"{item.get('method', '')} {item.get('url', '')}".strip()
        for item in requests
        if isinstance(item, dict) and item.get("url")
    ]
    return " → ".join(steps)


def _create_finding(
    session: Session,
    project: Project,
    run: Run,
    case: TestCase,
    row: CaseResult,
    attribution: dict[str, Any],
) -> tuple[Finding, bool]:
    """同一个用例已有未关闭的缺陷时，更新证据而不是再生成一条。"""
    auto_confirm = bool(attribution.get("auto_confirm"))
    repro = build_curl(row.request or {})
    chain = _request_chain(row)
    description = str(attribution.get("reason") or "")
    if chain and chain.count("→") >= 1:
        description = f"{description}\n\n复现顺序：{chain}"[:4000]
    title = f"[疑似缺陷] {case.title}"
    existing = session.scalar(
        select(Finding)
        .where(
            Finding.project_id == project.id,
            Finding.case_id == case.id,
            Finding.source == FindingSource.EXECUTION.value,
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
        },
        *[
            (
                {"type": "expected", **item}
                if isinstance(item, dict)
                else {"type": "expected", "detail": str(item)}
            )
            for item in (attribution.get("evidence") or [])
        ],
    ]
    if existing is not None:
        old_evidence = [
            item
            for item in (existing.evidence or [])
            if not (item.get("type") == "assertion" and item.get("run_id") == run.id)
        ]
        existing.evidence = (evidence + old_evidence)[:8]
        existing.result_id = row.id
        existing.repro = repro or existing.repro
        existing.description = description or existing.description
        feature = session.get(Feature, case.feature_id) if case.feature_id else None
        existing.module = feature.name if feature else existing.module
        existing.risk_type = {
            "security": "鉴权 / 越权",
            "boundary": "输入校验",
            "performance": "性能",
            "functional": "功能",
        }.get(case.dimension, existing.risk_type)
        if auto_confirm and existing.status == "pending":
            existing.status = "confirmed"
            existing.confirmed_by = "auto:attribution"
            existing.confirmed_at = _utcnow()
        return existing, False

    feature = session.get(Feature, case.feature_id) if case.feature_id else None
    risk_by_dimension = {
        "security": "鉴权 / 越权",
        "boundary": "输入校验",
        "performance": "性能",
        "functional": "功能",
    }
    finding = Finding(
        project_id=project.id,
        source=FindingSource.EXECUTION.value,
        title=title[:400],
        description=description,
        severity=_severity_from_case(case),
        evidence=evidence,
        code_location={},
        result_id=row.id,
        case_id=case.id,
        module=feature.name if feature else "执行用例",
        risk_type=risk_by_dimension.get(case.dimension, case.dimension),
        repro=repro,
        status="confirmed" if auto_confirm else "pending",
        confirmed_by="auto:attribution" if auto_confirm else None,
        confirmed_at=_utcnow() if auto_confirm else None,
    )
    session.add(finding)
    return finding, True


def _severity_from_case(case: TestCase) -> str:
    if case.dimension == "security":
        return Severity.HIGH.value
    if case.dimension == "functional":
        return Severity.MEDIUM.value
    return Severity.LOW.value


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def handle_deliver_webhook(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    from app.push import deliver_webhook

    return deliver_webhook(session, int(payload["delivery_id"]))


HANDLERS: dict[str, Callable[[Session, dict[str, Any]], dict[str, Any]]] = {
    "plan_features": handle_plan_features,
    "generate_cases": handle_generate_cases,
    "reparse_source": handle_reparse_source,
    "whitebox_review": handle_whitebox,
    "enrich_case": handle_enrich_case,
    "performance_run": handle_performance_run,
    "verify_fixes": handle_verify_fixes,
    "triage_findings": handle_triage_findings,
    "execute_run": handle_execute_run,
    "deliver_webhook": handle_deliver_webhook,
}
