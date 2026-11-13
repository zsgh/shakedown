"""测试数据资产服务：数据集 CRUD、行生成、用例绑定与执行期展开。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    CaseDataBinding,
    Endpoint,
    Project,
    TestCase,
    TestDataRow,
    TestDataset,
    TestFile,
)
from app.schemas.test_data import (
    CaseDataBindingCreate,
    TestDatasetCreate,
    TestDatasetUpdate,
)
from app.test_data import generate_rows, pii_fields, schema_for_endpoint


def _dataset_schema(dataset: TestDataset) -> dict[str, Any]:
    return dataset.schema_json if isinstance(dataset.schema_json, dict) else {}


def _rows_payload(dataset: TestDataset, *, limit: int | None = None) -> list[dict[str, Any]]:
    rows = sorted(dataset.rows or [], key=lambda item: item.row_index)
    if limit is not None:
        rows = rows[: max(0, limit)]
    return [{"id": row.id, "row_index": row.row_index, "data": row.data or {}} for row in rows]


def dataset_payload(
    dataset: TestDataset,
    *,
    include_rows: bool = True,
    row_limit: int = 50,
    mask_pii: bool = False,
) -> dict[str, Any]:
    schema = _dataset_schema(dataset)
    fields = schema.get("fields") if isinstance(schema.get("fields"), list) else []
    sensitive = set(pii_fields(fields))
    rows = _rows_payload(dataset, limit=row_limit) if include_rows else []
    if mask_pii and sensitive:
        from app.redaction import redact_text

        masked_rows = []
        for row in rows:
            data = dict(row.get("data") or {})
            for name in sensitive:
                if name in data and isinstance(data[name], str):
                    data[name] = redact_text(data[name])
            masked_rows.append({**row, "data": data})
        rows = masked_rows
    return {
        "id": dataset.id,
        "project_id": dataset.project_id,
        "name": dataset.name,
        "description": dataset.description,
        "kind": dataset.kind,
        "schema": schema,
        "seed": dataset.seed,
        "version": dataset.version,
        "status": dataset.status,
        "row_count": len(dataset.rows or []),
        "pii_fields": sorted(sensitive),
        "meta": dataset.meta_json or {},
        "rows": rows,
        "created_at": dataset.created_at,
        "updated_at": dataset.updated_at,
    }


def _replace_rows(session: Session, dataset: TestDataset, rows: list[dict[str, Any]]) -> None:
    session.execute(delete(TestDataRow).where(TestDataRow.dataset_id == dataset.id))
    for index, row in enumerate(rows):
        session.add(
            TestDataRow(
                project_id=dataset.project_id,
                dataset_id=dataset.id,
                row_index=index,
                data=row if isinstance(row, dict) else {"value": row},
            )
        )
    dataset.version = int(dataset.version or 0) + 1


def _generate_dataset_rows(
    project: Project,
    *,
    schema: dict[str, Any],
    seed: int,
    count: int,
    unicode: bool = False,
    distribution: str = "uniform",
    negative: bool = False,
) -> list[dict[str, Any]]:
    fields = schema.get("fields") if isinstance(schema.get("fields"), list) else []
    return generate_rows(
        project,
        fields=fields,
        schema=schema,
        count=count,
        seed=seed,
        unicode=unicode,
        distribution=distribution,
        negative=negative,
    )


def create_dataset(session: Session, project: Project, payload: TestDatasetCreate) -> TestDataset:
    schema = dict(payload.definition or {})
    if payload.endpoint_id:
        endpoint = session.get(Endpoint, payload.endpoint_id)
        if endpoint is None or endpoint.project_id != project.id:
            raise ValueError(f"接口不属于当前项目：{payload.endpoint_id}")
        schema = {**schema_for_endpoint(endpoint), **schema}
    if payload.fields:
        schema["fields"] = payload.fields
    schema.setdefault("fields", [])
    if payload.negative:
        schema["negative"] = True

    fields = schema.get("fields") if isinstance(schema.get("fields"), list) else []
    dataset = TestDataset(
        project_id=project.id,
        name=payload.name,
        description=payload.description,
        kind=payload.kind,
        schema_json=schema,
        seed=payload.seed,
        status=payload.status,
        meta_json={"pii_fields": pii_fields(fields), "negative": bool(payload.negative)},
    )
    session.add(dataset)
    session.flush()

    if payload.rows:
        rows = payload.rows
    elif payload.generate:
        rows = _generate_dataset_rows(
            project,
            schema=schema,
            seed=payload.seed,
            count=payload.count,
            unicode=payload.unicode,
            distribution=payload.distribution,
            negative=payload.negative,
        )
    else:
        rows = []
    _replace_rows(session, dataset, rows)
    session.flush()
    return dataset


def update_dataset(session: Session, project: Project, dataset: TestDataset, payload: TestDatasetUpdate) -> TestDataset:
    if payload.name is not None:
        dataset.name = payload.name
    if payload.description is not None:
        dataset.description = payload.description
    if payload.status is not None:
        dataset.status = payload.status
    schema = _dataset_schema(dataset)
    if payload.definition is not None:
        schema = dict(payload.definition)
    if payload.fields is not None:
        schema["fields"] = payload.fields
    if payload.negative is not None:
        schema["negative"] = payload.negative
    dataset.schema_json = schema
    fields = schema.get("fields") if isinstance(schema.get("fields"), list) else []
    dataset.meta_json = {**(dataset.meta_json or {}), "pii_fields": pii_fields(fields)}
    if payload.seed is not None:
        dataset.seed = payload.seed
    session.flush()

    if payload.regenerate:
        fields = schema.get("fields") if isinstance(schema.get("fields"), list) else []
        rows = generate_rows(
            project,
            fields=fields,
            schema=schema,
            count=payload.count or len(dataset.rows or []) or 10,
            seed=dataset.seed,
            unicode=bool(payload.unicode),
            distribution=payload.distribution or "uniform",
            negative=bool(payload.negative),
        )
        _replace_rows(session, dataset, rows)
    session.flush()
    return dataset


def get_dataset(session: Session, project_id: int, dataset_id: int) -> TestDataset | None:
    dataset = session.get(TestDataset, dataset_id)
    if dataset is None or dataset.project_id != project_id:
        return None
    return dataset


def list_datasets(session: Session, project_id: int, *, status: str | None = None) -> list[TestDataset]:
    stmt = select(TestDataset).where(TestDataset.project_id == project_id)
    if status:
        stmt = stmt.where(TestDataset.status == status)
    return list(session.scalars(stmt.order_by(TestDataset.created_at.desc(), TestDataset.id.desc())).all())


def delete_dataset(session: Session, dataset: TestDataset) -> None:
    session.delete(dataset)


def bind_case_dataset(
    session: Session, project: Project, case: TestCase, payload: CaseDataBindingCreate
) -> CaseDataBinding:
    dataset = get_dataset(session, project.id, payload.dataset_id)
    if dataset is None:
        raise ValueError(f"数据集不存在：{payload.dataset_id}")
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
        )
        session.add(binding)
    binding.mode = payload.mode
    binding.row_index = payload.row_index
    binding.overrides = payload.overrides or {}
    binding.enabled = payload.enabled
    return binding


def list_case_bindings(session: Session, case_id: int) -> list[CaseDataBinding]:
    return list(
        session.scalars(
            select(CaseDataBinding)
            .where(CaseDataBinding.case_id == case_id)
            .order_by(CaseDataBinding.id)
        ).all()
    )


def binding_payload(session: Session, binding: CaseDataBinding) -> dict[str, Any]:
    dataset = session.get(TestDataset, binding.dataset_id)
    return {
        "id": binding.id,
        "project_id": binding.project_id,
        "case_id": binding.case_id,
        "dataset_id": binding.dataset_id,
        "dataset_name": dataset.name if dataset else "",
        "mode": binding.mode,
        "row_index": binding.row_index,
        "overrides": binding.overrides or {},
        "enabled": binding.enabled,
        "dataset_row_count": len(dataset.rows or []) if dataset else 0,
        "created_at": binding.created_at,
    }


def _rows_for_binding(
    session: Session,
    binding: CaseDataBinding,
    *,
    inline_rows: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    overrides = binding.overrides if isinstance(binding.overrides, dict) else {}
    if inline_rows is not None:
        rows = [dict(row) for row in inline_rows if isinstance(row, dict)]
    else:
        dataset = session.get(TestDataset, binding.dataset_id)
        if dataset is None or dataset.status != "active":
            return []
        rows = [
            dict(row.data or {})
            for row in sorted(dataset.rows or [], key=lambda item: item.row_index)
            if isinstance(row.data, dict)
        ]
    if binding.mode == "single":
        if binding.row_index is not None and 0 <= binding.row_index < len(rows):
            rows = [rows[binding.row_index]]
        else:
            rows = rows[:1]
    if overrides:
        rows = [{**row, **overrides} for row in rows]
    return rows


def case_data_rows(session: Session, case: TestCase) -> list[dict[str, Any]]:
    """返回用例执行时要迭代的数据行（已应用 overrides）。"""
    bindings = [
        binding
        for binding in list_case_bindings(session, case.id)
        if binding.enabled and binding.project_id == case.project_id
    ]
    if bindings:
        rows: list[dict[str, Any]] = []
        for binding in bindings:
            rows.extend(_rows_for_binding(session, binding))
        return rows

    # 向后兼容：data_template 里可以直接放 dataset_id / rows。
    template = case.data_template if isinstance(case.data_template, dict) else {}
    if template.get("dataset_id"):
        dataset = get_dataset(session, case.project_id, int(template["dataset_id"]))
        if dataset is None:
            return []
        binding = CaseDataBinding(
            project_id=case.project_id,
            case_id=case.id,
            dataset_id=dataset.id,
            mode=str(template.get("mode") or "single"),
            row_index=template.get("row_index"),
            overrides=template.get("overrides") or {},
            enabled=True,
        )
        return _rows_for_binding(session, binding)
    inline = template.get("rows")
    if isinstance(inline, list):
        return [dict(row) for row in inline if isinstance(row, dict)]
    return []


def load_case_data(session: Session, cases: Iterable[TestCase]) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = {}
    for case in cases:
        rows = case_data_rows(session, case)
        if rows:
            result[case.id] = rows
    return result


def _file_root(project: Project) -> Path:
    root = get_settings().data_dir / "test_files" / str(project.id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def create_test_file(
    session: Session,
    project: Project,
    *,
    name: str,
    filename: str,
    content: bytes,
    content_type: str = "application/octet-stream",
) -> TestFile:
    digest = hashlib.sha256(content).hexdigest()
    safe_name = "".join(ch for ch in Path(filename).name if ch.isalnum() or ch in "._-") or "file.bin"
    target = _file_root(project) / f"{digest[:16]}-{safe_name}"
    target.write_bytes(content)
    existing = session.scalar(
        select(TestFile).where(TestFile.project_id == project.id, TestFile.name == name)
    )
    if existing is None:
        existing = TestFile(project_id=project.id, name=name, filename=safe_name)
        session.add(existing)
    existing.filename = safe_name
    existing.content_type = content_type or "application/octet-stream"
    existing.size = len(content)
    existing.sha256 = digest
    existing.storage_path = str(target)
    session.flush()
    return existing


def delete_test_file(session: Session, test_file: TestFile) -> None:
    path = Path(test_file.storage_path) if test_file.storage_path else None
    if path and path.is_file():
        try:
            path.unlink()
        except OSError:
            pass
    session.delete(test_file)


def file_payload(test_file: TestFile) -> dict[str, Any]:
    return {
        "id": test_file.id,
        "project_id": test_file.project_id,
        "name": test_file.name,
        "filename": test_file.filename,
        "content_type": test_file.content_type,
        "size": test_file.size,
        "sha256": test_file.sha256,
        "created_at": test_file.created_at,
    }


def file_blobs_for_specs(
    session: Session, project: Project, specs: Iterable[dict[str, Any]]
) -> dict[int, dict[str, Any]]:
    """把用例里引用的 test_file 读成 base64，随 spec 注入沙箱。

    返回 ``{file_id: {"name","filename","content_type","content_b64","sha256"}}``。
    """
    import base64

    wanted: set[int] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("file_id") is not None:
                try:
                    wanted.add(int(value["file_id"]))
                except (TypeError, ValueError):
                    pass
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for spec in specs:
        walk(spec)
    if not wanted:
        return {}
    rows = session.scalars(
        select(TestFile).where(TestFile.project_id == project.id, TestFile.id.in_(wanted))
    ).all()
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        path = Path(row.storage_path)
        if not path.is_file():
            continue
        result[row.id] = {
            "name": row.name,
            "filename": row.filename,
            "content_type": row.content_type,
            "sha256": row.sha256,
            "content_b64": base64.b64encode(path.read_bytes()).decode("ascii"),
        }
    return result


def test_data_health(session: Session, project: Project) -> dict[str, Any]:
    dataset_count = session.scalar(
        select(func.count()).select_from(TestDataset).where(TestDataset.project_id == project.id)
    ) or 0
    row_count = session.scalar(
        select(func.count()).select_from(TestDataRow).where(TestDataRow.project_id == project.id)
    ) or 0
    binding_count = session.scalar(
        select(func.count()).select_from(CaseDataBinding).where(CaseDataBinding.project_id == project.id)
    ) or 0
    file_count = session.scalar(
        select(func.count()).select_from(TestFile).where(TestFile.project_id == project.id)
    ) or 0
    datasets_without_rows = session.scalar(
        select(func.count())
        .select_from(TestDataset)
        .where(
            TestDataset.project_id == project.id,
            ~TestDataset.rows.any(),
        )
    ) or 0
    cases_without_data = session.scalar(
        select(func.count())
        .select_from(TestCase)
        .where(
            TestCase.project_id == project.id,
            TestCase.status == "enabled",
            ~TestCase.id.in_(
                select(CaseDataBinding.case_id).where(CaseDataBinding.project_id == project.id)
            ),
        )
    ) or 0
    pii_dataset_count = 0
    for dataset in list_datasets(session, project.id):
        fields = _dataset_schema(dataset).get("fields")
        if isinstance(fields, list) and pii_fields(fields):
            pii_dataset_count += 1
    orphan_files = 0
    for test_file in session.scalars(select(TestFile).where(TestFile.project_id == project.id)).all():
        if not test_file.storage_path or not Path(test_file.storage_path).is_file():
            orphan_files += 1
    stale_bindings = session.scalar(
        select(func.count())
        .select_from(CaseDataBinding)
        .join(TestDataset, TestDataset.id == CaseDataBinding.dataset_id)
        .where(CaseDataBinding.project_id == project.id, TestDataset.status != "active")
    ) or 0
    return {
        "project_id": project.id,
        "dataset_count": dataset_count,
        "row_count": row_count,
        "binding_count": binding_count,
        "file_count": file_count,
        "datasets_without_rows": datasets_without_rows,
        "cases_without_data": cases_without_data,
        "orphan_rows": 0,
        "orphan_files": orphan_files,
        "stale_bindings": stale_bindings,
        "pii_dataset_count": pii_dataset_count,
    }


def cleanup_test_data(session: Session, project: Project) -> dict[str, Any]:
    """清理归档数据集的行、失效绑定和丢失存储文件的记录。"""
    archived = list(
        session.scalars(
            select(TestDataset).where(
                TestDataset.project_id == project.id,
                TestDataset.status == "archived",
            )
        ).all()
    )
    rows_deleted = 0
    for dataset in archived:
        rows_deleted += session.query(TestDataRow).filter(TestDataRow.dataset_id == dataset.id).delete()
        session.expunge(dataset)
    stale_bindings = 0
    for binding in session.scalars(
        select(CaseDataBinding).where(CaseDataBinding.project_id == project.id)
    ).all():
        dataset = session.get(TestDataset, binding.dataset_id)
        if dataset is None or dataset.status != "active":
            session.delete(binding)
            stale_bindings += 1
    files_deleted = 0
    referenced: set[Path] = set()
    for test_file in session.scalars(select(TestFile).where(TestFile.project_id == project.id)).all():
        if not test_file.storage_path or not Path(test_file.storage_path).is_file():
            session.delete(test_file)
            files_deleted += 1
        else:
            referenced.add(Path(test_file.storage_path).resolve())
    root = _file_root(project)
    for path in root.rglob("*"):
        if path.is_file() and path.resolve() not in referenced:
            try:
                path.unlink()
                files_deleted += 1
            except OSError:
                continue
    return {
        "rows_deleted": rows_deleted,
        "stale_bindings_deleted": stale_bindings,
        "orphan_files_deleted": files_deleted,
    }


def spec_has_data_placeholders(spec: dict[str, Any]) -> bool:
    text = json.dumps(spec, ensure_ascii=False, default=str)
    return "{{data." in text or "{{data}}" in text


def delete_project_files(project: Project) -> int:
    """项目删除时清理磁盘上的测试文件（DB 行由 FK 级联删除）。"""
    import shutil

    root = _file_root(project)
    count = sum(1 for path in root.rglob("*") if path.is_file())
    try:
        shutil.rmtree(root)
    except OSError:
        return 0
    return count
