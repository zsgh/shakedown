"""重新抽取功能点时的数据保护：不能静默清空已有用例的 feature 关联。"""
from __future__ import annotations

import pytest

from app.agent.agents import extract_features
from app.models import Feature, TestCase as CaseModel


def test_extract_features_refuses_to_orphan_existing_cases(session, sample_project):
    feature = Feature(project_id=sample_project.id, name="创建用户", module="user")
    session.add(feature)
    session.flush()

    session.add(
        CaseModel(
            project_id=sample_project.id,
            feature_id=feature.id,
            title="创建用户",
            dimension="functional",
            case_type="api",
            spec={"title": "创建用户", "dimension": "functional", "type": "api", "steps": []},
            steps=[],
            expected={"assertions": []},
            evidence=[],
            status="enabled",
        )
    )
    session.commit()

    with pytest.raises(ValueError, match="已有 1 条用例"):
        extract_features(session, sample_project, provider=None)  # type: ignore[arg-type]
