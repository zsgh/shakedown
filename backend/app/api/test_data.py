"""测试数据资产 API：数据集、行、用例绑定、项目文件。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import ConflictError, NotFoundError, ValidationError
from app.models import CaseDataBinding, Project, TestCase, TestDataset, TestFile
from app.schemas.test_data import (
    CaseDataBindingCreate,
    TestDatasetCreate,
    TestDatasetUpdate,
)
from app.test_data_service import (
    bind_case_dataset,
    binding_payload,
    create_dataset,
    create_test_file,
    dataset_payload,
    delete_dataset,
    delete_test_file,
    file_payload,
    get_dataset,
    list_case_bindings,
    list_datasets,
    cleanup_test_data,
    test_data_health,
    update_dataset,
)

router = APIRouter(tags=["test-data"])


def _project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{project_id}")
    return project


def _dataset_or_404(db: Session, project_id: int, dataset_id: int) -> TestDataset:
    dataset = get_dataset(db, project_id, dataset_id)
    if dataset is None:
        raise NotFoundError(f"数据集不存在：{dataset_id}")
    return dataset


@router.get("/projects/{project_id}/test-datasets")
def list_project_datasets(
    project_id: int,
    status: str | None = None,
    mask_pii: bool = False,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project = _project(db, project_id)
    rows = list_datasets(db, project.id, status=status)
    return {
        "project_id": project.id,
        "count": len(rows),
        "items": [dataset_payload(row, mask_pii=mask_pii) for row in rows],
    }


@router.post("/projects/{project_id}/test-datasets", status_code=201)
def create_project_dataset(
    project_id: int, payload: TestDatasetCreate, db: Session = Depends(get_db)
) -> dict[str, Any]:
    project = _project(db, project_id)
    existing = db.scalar(
        select(TestDataset).where(TestDataset.project_id == project.id, TestDataset.name == payload.name)
    )
    if existing is not None:
        raise ConflictError(f"数据集名称已存在：{payload.name}")
    try:
        dataset = create_dataset(db, project, payload)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    db.commit()
    db.refresh(dataset)
    return dataset_payload(dataset)


@router.get("/projects/{project_id}/test-datasets/{dataset_id}")
def get_project_dataset(
    project_id: int, dataset_id: int, mask_pii: bool = False, db: Session = Depends(get_db)
) -> dict[str, Any]:
    project = _project(db, project_id)
    dataset = _dataset_or_404(db, project.id, dataset_id)
    return dataset_payload(dataset, mask_pii=mask_pii)


@router.patch("/projects/{project_id}/test-datasets/{dataset_id}")
def update_project_dataset(
    project_id: int, dataset_id: int, payload: TestDatasetUpdate, db: Session = Depends(get_db)
) -> dict[str, Any]:
    project = _project(db, project_id)
    dataset = _dataset_or_404(db, project.id, dataset_id)
    if payload.name and payload.name != dataset.name:
        existing = db.scalar(
            select(TestDataset).where(TestDataset.project_id == project.id, TestDataset.name == payload.name)
        )
        if existing is not None:
            raise ConflictError(f"数据集名称已存在：{payload.name}")
    update_dataset(db, project, dataset, payload)
    db.commit()
    db.refresh(dataset)
    return dataset_payload(dataset)


@router.delete("/projects/{project_id}/test-datasets/{dataset_id}", status_code=204)
def delete_project_dataset(project_id: int, dataset_id: int, db: Session = Depends(get_db)) -> None:
    project = _project(db, project_id)
    dataset = _dataset_or_404(db, project.id, dataset_id)
    delete_dataset(db, dataset)
    db.commit()


@router.get("/projects/{project_id}/test-data/health")
def project_test_data_health(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    project = _project(db, project_id)
    return test_data_health(db, project)


@router.post("/projects/{project_id}/test-data/cleanup")
def cleanup_project_test_data(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    project = _project(db, project_id)
    result = cleanup_test_data(db, project)
    db.commit()
    return {"project_id": project.id, **result}


@router.get("/test-cases/{case_id}/data-bindings")
def get_case_data_bindings(case_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    case = db.get(TestCase, case_id)
    if case is None:
        raise NotFoundError(f"用例不存在：{case_id}")
    bindings = list_case_bindings(db, case.id)
    return {
        "case_id": case.id,
        "count": len(bindings),
        "items": [binding_payload(db, binding) for binding in bindings],
    }


@router.post("/test-cases/{case_id}/data-bindings", status_code=201)
def bind_case_data(case_id: int, payload: CaseDataBindingCreate, db: Session = Depends(get_db)) -> dict[str, Any]:
    case = db.get(TestCase, case_id)
    if case is None:
        raise NotFoundError(f"用例不存在：{case_id}")
    project = db.get(Project, case.project_id)
    if project is None:
        raise NotFoundError(f"项目不存在：{case.project_id}")
    try:
        binding = bind_case_dataset(db, project, case, payload)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    db.commit()
    db.refresh(binding)
    return binding_payload(db, binding)


@router.delete("/data-bindings/{binding_id}", status_code=204)
def unbind_case_data(binding_id: int, db: Session = Depends(get_db)) -> None:
    binding = db.get(CaseDataBinding, binding_id)
    if binding is None:
        raise NotFoundError(f"数据绑定不存在：{binding_id}")
    db.delete(binding)
    db.commit()


@router.get("/projects/{project_id}/test-files")
def list_project_files(project_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    project = _project(db, project_id)
    rows = list(
        db.scalars(
            select(TestFile).where(TestFile.project_id == project.id).order_by(TestFile.created_at.desc())
        ).all()
    )
    return {"project_id": project.id, "count": len(rows), "items": [file_payload(row) for row in rows]}


@router.post("/projects/{project_id}/test-files", status_code=201)
async def upload_project_file(
    project_id: int,
    file: UploadFile = File(...),
    name: str = Form(default=""),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    project = _project(db, project_id)
    content = await file.read()
    if not content:
        raise ValidationError("文件为空")
    if len(content) > 10 * 1024 * 1024:
        raise ValidationError("文件超过 10MB 限制")
    filename = file.filename or "file.bin"
    file_name = (name or filename).strip()[:200]
    if not file_name:
        raise ValidationError("缺少文件名称")
    test_file = create_test_file(
        db,
        project,
        name=file_name,
        filename=filename,
        content=content,
        content_type=file.content_type or "application/octet-stream",
    )
    db.commit()
    db.refresh(test_file)
    return file_payload(test_file)


@router.delete("/test-files/{file_id}", status_code=204)
def delete_project_file(file_id: int, db: Session = Depends(get_db)) -> None:
    test_file = db.get(TestFile, file_id)
    if test_file is None:
        raise NotFoundError(f"文件不存在：{file_id}")
    delete_test_file(db, test_file)
    db.commit()
