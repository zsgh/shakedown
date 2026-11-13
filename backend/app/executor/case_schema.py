"""统一用例 JSON Schema —— 执行器只认这一种格式。

设计要点（对应 todo.md 2.3）：
- 前置条件 / 步骤 / 断言 / 清理 四段式；
- 每条断言必须带 `evidence`（依据来源）；`status=pending` 的断言标为「待确认」，
  执行时只记录不判失败；
- 没有依据的猜测不生成断言，改为 `contract_questions`（待确认问题，不进入执行链）；
- 断言类型：status_code / json_path / db_state / cross_api / timing / text_contains；
- 步骤类型：request（HTTP）/ browser（Playwright）/ db（直接查库）/ command（CLI）/ parallel（并发请求）。
"""
from __future__ import annotations

import json
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"

Dimension = Literal["functional", "boundary", "security", "performance"]
CaseType = Literal["api", "web"]
AssertionKind = Literal["status_code", "json_path", "db_state", "cross_api", "timing", "text_contains", "exit_code", "stdout_contains", "stderr_contains"]
EvidenceStatus = Literal["confirmed", "pending"]

EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_type": {
            "type": "string",
            "enum": ["doc", "openapi", "code", "policy", "manual", "none"],
            "description": "policy 表示通用测试策略（见 app/policies.py），不需要项目契约即可确认",
        },
        "ref": {"type": "string"},
        "quote": {"type": "string"},
        "source_id": {"type": "integer"},
        "chunk_id": {"type": "integer"},
        "endpoint_id": {"type": "integer"},
        "note": {"type": "string"},
    },
    "additionalProperties": True,
}

ASSERTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["kind"],
    "properties": {
        "kind": {"type": "string", "enum": list(AssertionKind.__args__)},
        "target": {"type": "string", "description": "json_path 的路径或 db 查询名"},
        "operator": {
            "type": "string",
            "enum": ["eq", "ne", "in", "not_in", "lt", "le", "gt", "ge", "contains", "not_contains", "exists", "not_exists", "matches", "empty"],
        },
        "expected": {},
        "threshold_ms": {"type": "number"},
        "status": {"type": "string", "enum": list(EvidenceStatus.__args__)},
        "evidence": EVIDENCE_SCHEMA,
        "description": {"type": "string"},
        "step_index": {"type": "integer"},
    },
    "additionalProperties": True,
}

CONTRACT_QUESTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["question"],
    "properties": {
        "question": {"type": "string", "description": "需要被测方或测试负责人确认的问题，不进入执行链"},
        "missing_contract": {"type": "string", "description": "缺少哪一部分契约（响应 schema / 鉴权语义 / 参数约束…）"},
        "owner_kind": {"type": "string", "enum": ["sut-contract", "test-side", "environment"]},
        "step_index": {"type": "integer"},
        "source": {"type": "string", "description": "generation / migration / manual"},
    },
    "additionalProperties": True,
}

STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["action"],
    "properties": {
        "action": {"type": "string", "enum": ["request", "browser", "db", "sleep", "command", "parallel"]},
        "name": {"type": "string"},
        "method": {"type": "string"},
        "path": {"type": "string"},
        "base_url": {"type": "string"},
        "headers": {"type": "object"},
        "query": {"type": "object"},
        "body": {},
        "cookies": {"type": "object"},
        "auth": {"type": "object"},
        "timeout_ms": {"type": "number"},
        "samples": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000,
            "description": "同一请求重复采样次数；timing 断言用多次采样的 P95 判定",
        },
        "browser": {"type": "object"},
        "sql": {"type": "string"},
        "params": {"type": "object"},
        "seconds": {"type": "number"},
        "save": {"type": "object", "description": "{变量名: $.json.path}"},
        "parallel": {
            "type": "object",
            "description": "并发发出同一请求，用于竞态 / 幂等验证",
            "properties": {
                "count": {"type": "integer", "minimum": 2, "maximum": 10},
                "request": {"type": "object"},
            },
        },
        "assert": {"type": "array", "items": ASSERTION_SCHEMA},
    },
    "additionalProperties": True,
}

CASE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Shakedown 测试用例",
    "type": "object",
    "required": ["title", "dimension", "type", "steps"],
    "properties": {
        "schema_version": {"type": "string", "default": SCHEMA_VERSION},
        "id": {"type": "string"},
        "title": {"type": "string", "minLength": 1},
        "dimension": {"type": "string", "enum": list(Dimension.__args__)},
        "type": {"type": "string", "enum": list(CaseType.__args__)},
        "feature": {"type": "object"},
        "description": {"type": "string"},
        "preconditions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["kind"],
                "properties": {
                    "kind": {"type": "string", "enum": ["http_login", "db_fixture", "manual", "none"]},
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "account_ref": {"type": "string"},
                    "sql": {"type": "string"},
                    "note": {"type": "string"},
                },
                "additionalProperties": True,
            },
        },
        "variables": {"type": "object"},
        "steps": {"type": "array", "items": STEP_SCHEMA},
        "assertions": {"type": "array", "items": ASSERTION_SCHEMA},
        "cleanup": {"type": "array", "items": STEP_SCHEMA},
        "evidence": {"type": "array", "items": EVIDENCE_SCHEMA},
        "tags": {"type": "array", "items": {"type": "string"}},
        "missing_input": {"type": "string"},
        "contract_questions": {
            "type": "array",
            "items": CONTRACT_QUESTION_SCHEMA,
            "description": "没有契约依据、需要人工/被测方确认的问题；不是断言，不判失败",
        },
        "probe": {
            "type": "boolean",
            "description": "探针用例：只记录响应供归因，不产出硬断言失败",
        },
    },
    "additionalProperties": True,
}

_VALIDATOR = Draft202012Validator(CASE_SCHEMA)


class Evidence(BaseModel):
    source_type: str = "none"
    ref: str = ""
    quote: str = ""
    source_id: int | None = None
    chunk_id: int | None = None
    endpoint_id: int | None = None
    note: str = ""


class Assertion(BaseModel):
    kind: str
    target: str = ""
    operator: str = "eq"
    expected: Any = None
    threshold_ms: float | None = None
    status: str = "pending"
    evidence: Evidence = Field(default_factory=Evidence)
    description: str = ""
    step_index: int | None = None


class Step(BaseModel):
    action: str
    name: str = ""
    method: str = ""
    path: str = ""
    base_url: str = ""
    headers: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    body: Any = None
    auth: dict[str, Any] = Field(default_factory=dict)
    browser: dict[str, Any] = Field(default_factory=dict)
    sql: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    seconds: float = 0.0
    save: dict[str, str] = Field(default_factory=dict)
    assert_: list[Assertion] = Field(default_factory=list, alias="assert")

    model_config = {"populate_by_name": True}


class Precondition(BaseModel):
    kind: str = "none"
    name: str = ""
    role: str = ""
    account_ref: str = ""
    sql: str = ""
    note: str = ""


class TestCaseSpec(BaseModel):
    """统一用例的结构化视图。"""

    schema_version: str = SCHEMA_VERSION
    id: str = ""
    title: str
    dimension: str = "functional"
    type: str = "api"
    feature: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    preconditions: list[Precondition] = Field(default_factory=list)
    variables: dict[str, Any] = Field(default_factory=dict)
    steps: list[Step] = Field(default_factory=list)
    assertions: list[Assertion] = Field(default_factory=list)
    cleanup: list[Step] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    missing_input: str = ""
    contract_questions: list[dict[str, Any]] = Field(default_factory=list)
    probe: bool = False

    model_config = {"populate_by_name": True}


def validate_case_spec(spec: dict[str, Any]) -> list[str]:
    """返回错误列表（空列表表示合法）。"""
    errors = []
    for err in sorted(_VALIDATOR.iter_errors(spec), key=lambda e: list(e.path)):
        location = "/".join(str(p) for p in err.path) or "(root)"
        errors.append(f"{location}: {err.message}")
    return errors


def is_valid(spec: dict[str, Any]) -> bool:
    return not validate_case_spec(spec)


def _assertion_key(assertion: dict[str, Any]) -> str:
    """断言语义指纹：用于去掉「顶层 + 步骤内联」重复写同一检查的情况。"""
    semantic = {key: assertion.get(key) for key in ("kind", "target", "operator", "expected")}
    return json.dumps(semantic, sort_keys=True, ensure_ascii=False, default=str)


def assertion_refs(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """按 ``collect_assertions`` 的顺序返回**原始断言对象**，用于就地修改。

    顶层断言如果与步骤内联断言重复会被跳过，与 ``collect_assertions`` 的
    去重规则保持一致，因此下标可以直接对应。
    """
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for step in spec.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for raw in step.get("assert") or []:
            if not isinstance(raw, dict):
                continue
            refs.append(raw)
            seen.add(_assertion_key(raw))
    for raw in spec.get("assertions") or []:
        if not isinstance(raw, dict):
            continue
        if _assertion_key(raw) in seen:
            continue
        refs.append(raw)
    return refs


def collect_assertions(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """汇总一条用例里的所有断言（步骤内联 + 顶层），并补齐 ``step_index``。

    约定：断言默认写在它要校验的步骤的 ``assert`` 里（这样能准确绑定该步骤
    的响应）；顶层 ``assertions`` 保留给跨步骤 / 用例级断言，可自带
    ``step_index``。生成器历史上两种都写过，这里统一成一份视图，供 API/UI
    展示与审核使用。

    顶层断言如果与某个步骤断言的 (kind, target, operator, expected) 完全相同，
    只保留步骤内联的那条，避免 UI 重复计数、执行器重复判定。
    """
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, step in enumerate(spec.get("steps") or []):
        if not isinstance(step, dict):
            continue
        for raw in step.get("assert") or []:
            if not isinstance(raw, dict):
                continue
            assertion = dict(raw)
            # 步骤内联断言的 step_index 以它所在的步骤为准
            assertion["step_index"] = index
            collected.append(assertion)
            seen.add(_assertion_key(assertion))
    for raw in spec.get("assertions") or []:
        if not isinstance(raw, dict):
            continue
        if _assertion_key(raw) in seen:
            continue
        collected.append(dict(raw))
    return collected


def pending_assertions(spec: dict[str, Any]) -> list[Assertion]:
    """待确认断言（无依据）——执行时只记录，不判失败。"""
    out: list[Assertion] = []
    for raw in collect_assertions(spec):
        assertion = Assertion.model_validate(raw)
        if assertion.status != "confirmed" or assertion.evidence.source_type == "none":
            out.append(assertion)
    return out


_BACKED_SOURCES = {"openapi", "doc", "code", "policy", "project_config"}


def evidence_is_backed(assertion: dict[str, Any]) -> bool:
    """断言是否有可引用依据；policy、项目配置与说明理由的 manual 假设都算有依据。"""
    evidence = assertion.get("evidence")
    if not isinstance(evidence, dict):
        return False
    source = str(evidence.get("source_type") or "")
    if source in _BACKED_SOURCES:
        return bool(str(evidence.get("ref") or "").strip())
    if source == "manual":
        note = str(evidence.get("note") or "").strip()
        author = str(evidence.get("author") or "").strip()
        # manual 必须写清「谁 / 为什么 / 待确认点」，只写「无文档」这类缺失说明不算依据。
        return bool(note or author)
    return False


def collect_contract_questions(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """待确认问题：没有 expected，不进入断言统计与执行链。"""
    questions: list[dict[str, Any]] = []
    for raw in spec.get("contract_questions") or []:
        if not isinstance(raw, dict):
            continue
        question = str(raw.get("question") or "").strip()
        if not question:
            continue
        item = dict(raw)
        item["question"] = question
        item["missing_contract"] = str(raw.get("missing_contract") or "").strip()
        owner = str(raw.get("owner_kind") or "")
        item["owner_kind"] = owner if owner in {"sut-contract", "test-side", "environment"} else "sut-contract"
        item.setdefault("source", "generation")
        questions.append(item)
    return questions


def assertion_to_contract_question(
    assertion: dict[str, Any], *, step_index: int | None = None, source: str = "migration"
) -> dict[str, Any]:
    """把无依据断言降级成问题：保留「想验证什么」，但不保留 expected。"""
    kind = str(assertion.get("kind") or "")
    target = str(assertion.get("target") or "")
    description = str(assertion.get("description") or "").strip()
    evidence = assertion.get("evidence") if isinstance(assertion.get("evidence"), dict) else {}
    subject = description or f"{kind} {target}".strip()
    owner = "test-side" if kind == "db_state" else "sut-contract"
    question = {
        "question": f"请确认：{subject} 的期望结果是什么？" if subject else "请确认该用例的期望结果是什么？",
        "missing_contract": str(evidence.get("note") or "").strip()
        or "OpenAPI / 文档 / 代码都没有给出可引用依据，无法推导期望值",
        "owner_kind": owner,
        "source": source,
    }
    if step_index is not None:
        question["step_index"] = step_index
    elif isinstance(assertion.get("step_index"), int):
        question["step_index"] = assertion["step_index"]
    return question


def split_unbacked_assertions(spec: dict[str, Any], *, source: str = "generation") -> list[dict[str, Any]]:
    """就地移除无依据断言并转成 contract_questions，返回被移动的断言。

    这是「待确认的猜测不是断言，是问题」的落地：执行链与 pending 统计只保留
    有 openapi / doc / code / policy 依据或带理由的 manual 假设。
    """
    questions = collect_contract_questions(spec)
    seen = {item["question"] for item in questions}
    moved: list[dict[str, Any]] = []

    def _handle(raw: dict[str, Any], step_index: int | None) -> bool:
        if evidence_is_backed(raw):
            return True
        question = assertion_to_contract_question(raw, step_index=step_index, source=source)
        if question["question"] not in seen:
            questions.append(question)
            seen.add(question["question"])
        moved.append(
            {
                "kind": raw.get("kind") or "",
                "description": raw.get("description") or "",
                "expected": raw.get("expected"),
                "step_index": step_index if step_index is not None else raw.get("step_index"),
                "question": question["question"],
            }
        )
        return False

    for index, step in enumerate(spec.get("steps") or []):
        if not isinstance(step, dict):
            continue
        step["assert"] = [
            raw
            for raw in step.get("assert") or []
            if isinstance(raw, dict) and _handle(raw, index)
        ]
    spec["assertions"] = [
        raw
        for raw in spec.get("assertions") or []
        if isinstance(raw, dict) and _handle(raw, None)
    ]
    spec["contract_questions"] = questions
    return moved

