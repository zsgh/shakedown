"""测试数据自动补全：从用例的 {{data.*}} 占位符反推字段并建数据集。

目标是让用户不再手工建数据集 / 手工绑定：只要用例写了 {{data.email}}，
run 前就自动生成一个 `auto-case-{id}` 数据集并绑定。用户之后仍可在
「测试数据」页把它改成更多行 / 更真实的分布。
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import CaseDataBinding, Endpoint, Feature, Project, TestCase, TestDataRow, TestDataset
from app.schemas.test_data import TestDatasetCreate
from app.test_data import generate_rows, pii_fields, schema_for_endpoint
from app.test_data_service import create_dataset

_DATA_PLACEHOLDER = re.compile(r"\{\{\s*data\.([a-zA-Z0-9_.-]+)\s*\}\}")
_ANY_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")
_DYNAMIC = re.compile(r"\{[^}]+\}")
_RESERVED = {
    "run_id", "uuid", "timestamp", "project_id", "base_url", "auth_token", "access_token", "row_index",
    "token", "password", "username", "secret", "authorization", "cookie",
}


def extract_data_fields(spec: dict[str, Any] | None) -> list[str]:
    """从用例 JSON 里抽需要自动生成的数据字段。

    优先识别显式 `{{data.x}}`；同时兼容旧生成器写的 `{{email}}` 这类
    业务字段占位符（排除 run_id / save 变量 / account.*）。
    """
    if not isinstance(spec, dict):
        return []
    names: set[str] = set(_DATA_PLACEHOLDER.findall(json.dumps(spec, ensure_ascii=False, default=str)))

    saved: set[str] = set()
    all_steps: list[dict[str, Any]] = []
    for step in list(spec.get("steps") or []) + list(spec.get("cleanup") or []):
        if isinstance(step, dict):
            all_steps.append(step)
    for fixture in spec.get("fixtures") or []:
        if isinstance(fixture, dict):
            for step in list(fixture.get("steps") or []) + list(fixture.get("cleanup") or []):
                if isinstance(step, dict):
                    all_steps.append(step)
    for step in all_steps:
        save = step.get("save")
        if isinstance(save, dict):
            saved.update(str(key) for key in save)

    for step in all_steps:
        for key in ("body", "query"):
            value = step.get(key)
            if value is None:
                continue
            for match in _ANY_PLACEHOLDER.findall(json.dumps(value, ensure_ascii=False, default=str)):
                if match.startswith(("account.", "data.")):
                    continue
                if match in _RESERVED or match in saved or "." in match:
                    continue
                names.add(match)
    return sorted(name for name in names if name)


def _path_template(path: str) -> str:
    path = re.sub(r"\{\{[^}]+\}\}", "{}", path or "")
    path = _DYNAMIC.sub("{}", path)
    return path.rstrip("/") or "/"


def _case_endpoint(session: Session, case: TestCase) -> Endpoint | None:
    if case.feature_id:
        feature = session.get(Feature, case.feature_id)
        if feature is not None:
            for endpoint_id in feature.endpoint_ids or []:
                endpoint = session.get(Endpoint, int(endpoint_id))
                if endpoint is not None and endpoint.project_id == case.project_id:
                    return endpoint
    for step in (case.spec or {}).get("steps") or []:
        if not isinstance(step, dict) or step.get("action") != "request":
            continue
        method = str(step.get("method") or "GET").upper()
        path = _path_template(str(step.get("path") or ""))
        for endpoint in session.scalars(
            select(Endpoint).where(Endpoint.project_id == case.project_id, Endpoint.method == method)
        ).all():
            if _path_template(endpoint.path) == path:
                return endpoint
            if path.startswith(_path_template(endpoint.path).rstrip("/") + "/"):
                return endpoint
    return None


def _infer_field(name: str) -> dict[str, Any]:
    lowered = name.lower()
    field: dict[str, Any] = {"name": name, "type": "string"}
    if "email" in lowered:
        field.update({"type": "string", "format": "email", "unique": True})
    elif "phone" in lowered or "mobile" in lowered:
        field.update({"type": "string", "format": "phone"})
    elif "amount" in lowered or "price" in lowered or "total" in lowered:
        field.update({"type": "number", "minimum": 1, "maximum": 10000})
    elif lowered in {"id", "user_id", "project_id"} or lowered.endswith("_id"):
        field.update({"type": "integer", "minimum": 1, "maximum": 100000})
    elif "date" in lowered or lowered.endswith("_at") or "time" in lowered:
        field.update({"type": "string", "format": "date-time", "relative_days": 0})
    elif "status" in lowered or "state" in lowered:
        field.update({"type": "string", "enum": ["active", "pending", "disabled"]})
    elif "count" in lowered or "page" in lowered or "size" in lowered:
        field.update({"type": "integer", "minimum": 1, "maximum": 100})
    elif "name" in lowered or "title" in lowered:
        field.update({"type": "string", "minLength": 3})
    return field


def infer_fields(session: Session, project: Project, case: TestCase, names: list[str]) -> list[dict[str, Any]]:
    endpoint = _case_endpoint(session, case)
    endpoint_fields: dict[str, dict[str, Any]] = {}
    if endpoint is not None:
        for field in schema_for_endpoint(endpoint).get("fields") or []:
            if isinstance(field, dict) and field.get("name"):
                endpoint_fields[str(field["name"])] = field
    fields: list[dict[str, Any]] = []
    for name in names:
        field = endpoint_fields.get(name) or _infer_field(name)
        fields.append({**field, "name": name})
    return fields


def _auto_count(project: Project) -> int:
    settings = project.settings_json or {}
    raw = settings.get("test_data_auto_rows", settings.get("auto_data_rows", 1))
    try:
        return max(1, min(int(raw), 50))
    except (TypeError, ValueError):
        return 1


def _replace_rows(session: Session, dataset: TestDataset, rows: list[dict[str, Any]]) -> None:
    session.execute(delete(TestDataRow).where(TestDataRow.dataset_id == dataset.id))
    for index, row in enumerate(rows):
        session.add(
            TestDataRow(
                project_id=dataset.project_id,
                dataset_id=dataset.id,
                row_index=index,
                data=row,
            )
        )
    dataset.version = int(dataset.version or 0) + 1


def ensure_auto_dataset(
    session: Session,
    project: Project,
    case: TestCase,
    *,
    force: bool = False,
) -> TestDataset | None:
    """如果用例有 data 占位符且没有绑定，自动建数据集并绑定。"""
    existing_binding = session.scalar(
        select(CaseDataBinding).where(CaseDataBinding.case_id == case.id, CaseDataBinding.enabled.is_(True))
    )
    if existing_binding is not None:
        return session.get(TestDataset, existing_binding.dataset_id)

    names = extract_data_fields(case.spec)
    if not names:
        return None
    fields = infer_fields(session, project, case, names)
    signature = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    count = _auto_count(project)
    mode = "iterate" if count > 1 else "single"
    name = f"auto-case-{case.id}"

    dataset = session.scalar(
        select(TestDataset).where(TestDataset.project_id == project.id, TestDataset.name == name)
    )
    if dataset is None:
        dataset = create_dataset(
            session,
            project,
            TestDatasetCreate(
                name=name,
                description=f"由用例 #{case.id} 的 data 占位符自动创建",
                kind="generated",
                fields=fields,
                count=count,
                seed=42,
            ),
        )
        dataset.meta_json = {**(dataset.meta_json or {}), "auto": True, "auto_signature": signature}
    else:
        old_signature = (dataset.meta_json or {}).get("auto_signature")
        dataset.status = "active"
        dataset.schema_json = {**(dataset.schema_json or {}), "fields": fields, "source": "auto"}
        dataset.meta_json = {**(dataset.meta_json or {}), "auto": True, "auto_signature": signature}
        if force or old_signature != signature or len(dataset.rows or []) < count:
            rows = generate_rows(
                project,
                fields=fields,
                schema=dataset.schema_json,
                count=count,
                seed=dataset.seed,
            )
            _replace_rows(session, dataset, rows)
        else:
            dataset.meta_json = {**(dataset.meta_json or {}), "pii_fields": pii_fields(fields)}

    binding = session.scalar(
        select(CaseDataBinding).where(
            CaseDataBinding.case_id == case.id,
            CaseDataBinding.dataset_id == dataset.id,
        )
    )
    if binding is None:
        binding = CaseDataBinding(
            project_id=project.id,
            case_id=case.id,
            dataset_id=dataset.id,
            mode=mode,
            enabled=True,
        )
        session.add(binding)
    else:
        binding.enabled = True
        binding.mode = binding.mode or mode
    session.flush()
    return dataset


def autofill_cases(
    session: Session,
    project: Project,
    cases: Iterable[TestCase] | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    if cases is None:
        cases = session.scalars(
            select(TestCase).where(TestCase.project_id == project.id).order_by(TestCase.id)
        ).all()
    created: list[dict[str, Any]] = []
    skipped = 0
    for case in cases:
        before = session.scalar(
            select(CaseDataBinding.id).where(CaseDataBinding.case_id == case.id, CaseDataBinding.enabled.is_(True))
        )
        dataset = ensure_auto_dataset(session, project, case, force=force)
        if dataset is None:
            skipped += 1
            continue
        from sqlalchemy import func

        row_count = session.scalar(
            select(func.count()).select_from(TestDataRow).where(TestDataRow.dataset_id == dataset.id)
        ) or 0
        created.append(
            {
                "case_id": case.id,
                "dataset_id": dataset.id,
                "dataset_name": dataset.name,
                "row_count": row_count,
                "mode": "iterate" if _auto_count(project) > 1 else "single",
                "created_binding": before is None,
            }
        )
    return {"cases": len(created), "skipped": skipped, "items": created}


def ensure_auto_datasets(session: Session, project: Project, cases: Iterable[TestCase]) -> dict[str, Any]:
    """执行前调用：补齐缺失的数据集绑定，但不重复生成已存在且字段一致的数据。"""
    return autofill_cases(session, project, cases)


__all__ = ["autofill_cases", "ensure_auto_dataset", "ensure_auto_datasets", "extract_data_fields", "infer_fields"]
