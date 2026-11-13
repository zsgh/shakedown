"""通用测试策略包（policy）——不依赖项目契约也能稳定判定的硬性要求。

契约断言（openapi / doc / code）回答「按约定应该是什么」；策略断言回答
「无论约定如何都不应该发生什么」。策略必须显式版本化、可审计，且只包含
可以直接判失败的低误报规则；启发式 / 猜测不属于策略，只能作为 probe。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

POLICY_EVIDENCE_SOURCE = "policy"


@dataclass(frozen=True)
class Policy:
    id: str
    version: str
    title: str
    rationale: str
    applies_to: tuple[str, ...]
    assertion: dict[str, Any]
    keywords: tuple[str, ...] = field(default_factory=tuple)
    # probe_only 的策略依赖特定请求上下文（例如「未携带凭证」），
    # 不能出现在生成提示词里，否则 LLM 可能挂到带凭证的用例上造成误报。
    probe_only: bool = False


POLICIES: tuple[Policy, ...] = (
    Policy(
        id="POLICY-REL-001",
        version="1",
        title="服务端不得返回 5xx",
        rationale="任何业务输入都不应导致未处理的服务端异常；5xx 属于实现缺陷而不是产品语义。",
        applies_to=("api",),
        assertion={
            "kind": "status_code",
            "operator": "not_in",
            "expected": [500, 501, 502, 503, 504, 505, 506, 507, 508, 510, 511],
            "description": "策略：服务端不得返回 5xx",
        },
        keywords=("5xx", "500", "502", "503", "服务端错误", "内部错误", "未处理异常"),
    ),
    Policy(
        id="POLICY-SEC-001",
        version="1",
        title="错误响应不得泄露堆栈信息",
        rationale="堆栈会暴露内部实现、路径和依赖版本，属于信息泄露。",
        applies_to=("api", "web"),
        assertion={
            "kind": "text_contains",
            "operator": "not_contains",
            "expected": "Traceback",
            "description": "策略：响应不得包含堆栈（Traceback）",
        },
        keywords=("堆栈", "traceback", "stack trace", "调用栈", "异常详情"),
    ),
    Policy(
        id="POLICY-AUTH-001",
        version="1",
        title="声明鉴权的接口在未携带凭证时不得返回成功状态码",
        rationale="接口声明了鉴权却允许匿名访问成功，说明鉴权缺失或配置错误，属于越权风险。",
        applies_to=("api",),
        assertion={
            "kind": "status_code",
            "operator": "not_in",
            "expected": [200, 201, 202, 203, 204, 205, 206, 207, 208, 226],
            "description": "策略：未携带鉴权时不得返回成功状态码",
        },
        # 故意不配 keywords：存量迁移不能让「期望 403 的业务断言」被降级成鉴权策略检查。
        keywords=(),
        probe_only=True,
    ),
    Policy(
        id="POLICY-SEC-002",
        version="1",
        title="错误响应不得泄露 SQL / 数据库细节",
        rationale="数据库错误会泄露表结构、SQL 与驱动信息，属于信息泄露。",
        applies_to=("api",),
        assertion={
            "kind": "text_contains",
            "operator": "not_contains",
            "expected": "SQLSTATE",
            "description": "策略：响应不得包含数据库错误码（SQLSTATE）",
        },
        keywords=("sql", "sqlstate", "数据库错误", "db error", "查询失败"),
    ),
)

_BY_ID = {policy.id: policy for policy in POLICIES}


def policy_catalog() -> list[dict[str, Any]]:
    """给生成提示词用的策略清单（只暴露必要字段）。"""
    return [
        {
            "id": policy.id,
            "version": policy.version,
            "title": policy.title,
            "applies_to": list(policy.applies_to),
            "keywords": list(policy.keywords),
        }
        for policy in POLICIES
        if not policy.probe_only
    ]


def get_policy(policy_id: str) -> Policy | None:
    return _BY_ID.get(policy_id)


def match_policy(text: Any) -> Policy | None:
    """按自由文本命中策略；只用于存量迁移和无依据断言的兜底归类。"""
    value = str(text or "").strip().lower()
    if not value:
        return None
    for policy in POLICIES:
        if any(keyword.lower() in value for keyword in policy.keywords):
            return policy
    return None


def policy_assertion(policy_id: str, *, step_index: int | None = None) -> dict[str, Any]:
    """构造一条带 policy 依据的断言；执行器会把它当作已确认断言。"""
    policy = get_policy(policy_id)
    if policy is None:
        raise ValueError(f"未知策略：{policy_id}")
    assertion = dict(policy.assertion)
    assertion["status"] = "confirmed"
    assertion["evidence"] = {
        "source_type": POLICY_EVIDENCE_SOURCE,
        "ref": f"{policy.id}@v{policy.version}",
        "quote": policy.title,
        "note": policy.rationale,
    }
    if step_index is not None:
        assertion["step_index"] = step_index
    return assertion
