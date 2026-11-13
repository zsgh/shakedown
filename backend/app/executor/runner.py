"""执行编排：编译用例 → 沙箱执行 → 收集结果 → 失败重跑 → 生成复现命令。"""
from __future__ import annotations

import copy
import json
import shlex
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.enums import CaseResultStatus, CaseStatus
from app.executor.compiler import collect_results, compile_cases
from app.executor.environment import Environment
from app.executor.normalizer import normalize_spec
from app.logging import get_logger
from app.models import CaseResult, Project, Run, TestCase
from app.redaction import redact, redact_text
from app.sandbox import SandboxRequest, get_sandbox

logger = get_logger("shakedown.executor.runner")


@dataclass
class ExecutionOutcome:
    results: list[dict[str, Any]]
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    sandbox_task_id: str = ""


def _env_for_run(env: Environment, run: Run, artifacts_dir: str, attempt: int = 0) -> dict[str, str]:
    # 自动重跑沿用同一个 Run，但测试数据必须换一批：
    # 否则像「创建环境变量」这种非幂等步骤，第二次会因同名数据已存在而 400，
    # 把第一次的真实失败原因（例如断言问题）掩盖掉。
    run_token = str(run.id) if attempt <= 0 else f"{run.id}-r{attempt}"
    return {
        "SHAKEDOWN_BASE_URL": env.base_url,
        "SHAKEDOWN_TARGET_DSN": env.target_dsn,
        "SHAKEDOWN_PROJECT_ID": str(getattr(run, "project_id", "") or ""),
        "SHAKEDOWN_ACCOUNTS": json.dumps(env.accounts, ensure_ascii=False),
        "SHAKEDOWN_RUN_ID": run_token,
        "SHAKEDOWN_RESULTS_DIR": "results",
        "SHAKEDOWN_ARTIFACTS_DIR": "artifacts",
        "SHAKEDOWN_ARTIFACTS_HOST_DIR": artifacts_dir,
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def run_cases_in_sandbox(
    project: Project,
    run: Run,
    cases: list[TestCase],
    env: Environment,
    *,
    artifacts_dir: str,
    sandbox_mode: str | None = None,
    attempt: int = 0,
    endpoint_schemas: dict[str, dict[str, Any]] | None = None,
    should_cancel: Any = None,
) -> ExecutionOutcome:
    """把一批用例放进沙箱执行一次。``attempt`` > 0 表示同一 Run 内的自动重跑。"""
    specs = []
    for case in cases:
        spec = copy.deepcopy(case.spec or {})
        if endpoint_schemas:
            normalize_spec(spec, endpoint_schemas)
        spec["id"] = str(case.id)
        spec.setdefault("title", case.title)
        specs.append(spec)

    files = compile_cases(specs)
    request = SandboxRequest(
        files=files,
        env=_env_for_run(env, run, artifacts_dir, attempt),
        timeout=600,
        task_id=f"shakedown-{project.id}-{run.id}-{uuid.uuid4().hex[:8]}",
        image="runner",
        should_cancel=should_cancel,
    )
    sandbox = get_sandbox(sandbox_mode)
    result = sandbox.run(request)

    raw_results = collect_results(result.stdout)
    if not raw_results and result.raw.get("workdir"):
        # 本地模式：直接从工作目录读结果文件
        from app.sandbox.local import LocalSandbox

        if isinstance(sandbox, LocalSandbox):
            raw_results = sandbox.collect_results(result.raw["workdir"])

    # 把截图 / trace 等产物从临时工作目录搬到平台产物目录，供看板展示
    workdir = result.raw.get("workdir") if isinstance(result.raw, dict) else None
    if workdir:
        root = Path(artifacts_dir)
        for sub in ("artifacts", "results"):
            source = Path(str(workdir)) / sub
            if source.exists():
                target = root / sub
                target.mkdir(parents=True, exist_ok=True)
                for item in source.rglob("*"):
                    if not item.is_file():
                        continue
                    rel = item.relative_to(source)
                    destination = target / rel
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(item, destination)
                    except OSError:
                        continue
        run_prefix = f"run-{run.id}/"
        for raw in raw_results:
            for key in ("screenshot_path", "trace_path"):
                value = raw.get(key)
                if isinstance(value, str) and value and not value.startswith("/"):
                    raw[key] = run_prefix + value

    return ExecutionOutcome(
        results=raw_results,
        stdout=result.stdout[-20000:],
        stderr=result.stderr[-8000:],
        error=result.error,
        sandbox_task_id=result.task_id,
    )


_SENSITIVE_KEYS = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "private-token",
    "authorization",
    "cookie",
    "set-cookie",
}


def _redact_curl(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("***" if str(key).lower() in _SENSITIVE_KEYS else _redact_curl(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_curl(item) for item in value]
    return value


def build_curl(request_payload: dict[str, Any]) -> str:
    """把请求还原成可复制的 curl 命令（凭据一律脱敏）。"""
    if not request_payload:
        return ""
    method = request_payload.get("method", "GET")
    url = request_payload.get("url", "")
    if not url:
        return ""
    parts = ["curl", "-i", "-X", method]
    for key, value in (request_payload.get("headers") or {}).items():
        if str(key).lower() in _SENSITIVE_KEYS:
            value = "***"
        parts.extend(["-H", f"{key}: {value}"])
    for key, value in (request_payload.get("query") or {}).items():
        parts.extend(["--data-urlencode", f"{key}={value}"])
    body = request_payload.get("body")
    if body is not None:
        parts.extend(["-d", json.dumps(_redact_curl(body), ensure_ascii=False)])
    parts.append(shlex.quote(url))
    return " ".join(parts)


def _strip_nuls(value: Any) -> Any:
    """Postgres 的 JSON/文本不接受 \u0000；日志和响应里可能带二进制残留。"""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_strip_nuls(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _strip_nuls(item) for key, item in value.items()}
    return value


def persist_results(
    session: Session,
    project: Project,
    run: Run,
    cases: list[TestCase],
    outcome: ExecutionOutcome,
    *,
    rerun: bool = False,
) -> list[CaseResult]:
    """把沙箱返回的结果写入 case_results。"""
    by_id = {str(case.id): case for case in cases}
    redact_enabled = bool((project.settings_json or {}).get("redact_pii"))
    rows: list[CaseResult] = []
    seq = 0
    for raw in outcome.results:
        case_key = str(raw.get("case_id", ""))
        case = by_id.get(case_key)
        status = raw.get("status", "error")
        if status not in {s.value for s in CaseResultStatus}:
            status = CaseResultStatus.ERROR.value
        request_payload = _strip_nuls(raw.get("request") or {})
        response_payload = _strip_nuls(raw.get("response") or {})
        logs = str(raw.get("logs") or "").replace("\x00", "")[:20000]
        output = _strip_nuls(raw.get("output") or {})
        if redact_enabled:
            request_payload = redact(request_payload if isinstance(request_payload, dict) else {})
            response_payload = redact(response_payload)
            logs = redact_text(logs)
            output = redact(output)
        row = CaseResult(
            run_id=run.id,
            case_id=case.id if case else 0,
            project_id=project.id,
            status=status,
            request=request_payload if isinstance(request_payload, dict) else {},
            response=response_payload,
            duration_ms=float(raw.get("duration_ms") or 0.0),
            logs=logs,
            screenshot_path=raw.get("screenshot_path"),
            output=output if isinstance(output, dict) else {},
            rerun=rerun,
            seq=seq,
        )
        if raw.get("error"):
            row.logs = (row.logs + "\n" + str(raw["error"])).strip()
        session.add(row)
        rows.append(row)
        seq += 1

    # 没有结果回传的用例：记为 error（环境问题），不静默丢弃
    reported = {str(raw.get("case_id", "")) for raw in outcome.results}
    for case in cases:
        if str(case.id) in reported:
            continue
        row = CaseResult(
            run_id=run.id,
            project_id=project.id,
            case_id=case.id,
            status=CaseResultStatus.ERROR.value,
            logs=outcome.error or "沙箱未返回该用例的结果（可能被提前中断）",
            output={"errors": [outcome.error or "无结果"]},
            rerun=rerun,
            seq=seq,
        )
        session.add(row)
        rows.append(row)
        seq += 1
    session.flush()
    return rows


def select_cases(session: Session, project: Project, run: Run) -> list[TestCase]:
    """按 run.case_ids 筛选参与执行的用例。

    只跑 enabled 的；``missing_input`` 是「待补全说明」，不阻止有步骤的用例执行；
    没有步骤的用例无法执行，直接排除。
    """
    from sqlalchemy import func, select

    policy = (run.stats or {}).get("policy") if isinstance(run.stats, dict) else {}
    policy = policy if isinstance(policy, dict) else {}
    if policy.get("validation"):
        # 试运行：只跑本批选中的待确认用例，不能混入 enabled 全量
        statuses = [CaseStatus.PENDING.value]
    else:
        statuses = [CaseStatus.ENABLED.value]
        if policy.get("allow_needs_review"):
            statuses.append(CaseStatus.NEEDS_REVIEW.value)
    stmt = (
        select(TestCase)
        .where(TestCase.project_id == project.id)
        .where(TestCase.status.in_(statuses))
        .where(func.jsonb_array_length(TestCase.steps) > 0)
    )
    # case_ids 为 None 才表示「不过滤」；空列表表示显式选择了 0 条，
    # 不能回退成全量执行（否则单条用例因缺少输入被过滤后会误跑整个项目）。
    if run.case_ids is not None:
        stmt = stmt.where(TestCase.id.in_(list(run.case_ids)))
    return list(session.scalars(stmt.order_by(TestCase.priority, TestCase.id)).all())
