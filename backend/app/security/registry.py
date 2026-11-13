"""攻击用例包：可注册、可扩展的安全测试模式。

新增一类攻击只需要写一个 ``AttackPattern`` 子类并 ``@register``，不用改
生成器、API 或界面：

    @register
    class MyPattern(AttackPattern):
        id = "my-pattern"
        title = "一句话说明"
        category = "authz"          # redirect / session / authz / csrf / traversal / supply-chain
        severity = "medium"
        description = "为什么这是风险"

        def applicable(self, ctx):
            return bool(ctx.find("GET", "/admin")), "需要管理页面"

        def build(self, ctx):
            return [CaseDraft(title=..., spec=..., tags=[...])]

模式必须声明 ``attack_key``（同一目标只生成一条）和 ``policy_ref``
（policies.py 里的策略号或 SEC-* 约定），失败才能被归因器判成缺陷。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CATEGORIES = {
    "redirect": "开放重定向",
    "session": "会话与凭据",
    "authz": "越权 / 访问控制",
    "csrf": "跨站请求伪造",
    "traversal": "路径穿越",
    "injection": "注入",
    "supply-chain": "供应链 / 依赖",
    "transport": "传输与响应头",
    "logic": "业务逻辑",
}


@dataclass
class CaseDraft:
    """一条待生成的攻击用例。"""

    title: str
    spec: dict[str, Any]
    tags: list[str] = field(default_factory=list)
    priority: int = 40
    dimension: str = "security"
    attack_key: str = ""
    policy_ref: str = ""

    def __post_init__(self) -> None:
        if not self.attack_key:
            raise ValueError("攻击用例必须声明 attack_key，用于幂等生成")


@dataclass
class FindingDraft:
    """供应链类结果直接落缺陷，而不是生成一条永远无法修好的用例。"""

    key: str
    title: str
    description: str
    severity: str = "medium"
    risk_type: str = "供应链依赖"
    evidence: list[dict[str, Any]] = field(default_factory=list)


class AttackPattern:
    id = ""
    title = ""
    category = "logic"
    severity = "medium"
    description = ""
    reference = ""
    version = "1"

    def applicable(self, ctx: Any) -> tuple[bool, str]:
        return True, ""

    def build(self, ctx: Any) -> list[CaseDraft]:
        return []

    def findings(self, ctx: Any) -> list[FindingDraft]:
        return []


PATTERNS: dict[str, AttackPattern] = {}


def register(cls: type[AttackPattern]) -> type[AttackPattern]:
    instance = cls()
    if not instance.id:
        raise ValueError(f"{cls.__name__} 缺少 id")
    if instance.category not in CATEGORIES:
        raise ValueError(f"{instance.id} 的 category 未登记：{instance.category}")
    PATTERNS[instance.id] = instance
    return cls


def all_patterns() -> list[AttackPattern]:
    return sorted(PATTERNS.values(), key=lambda item: (item.category, item.id))


def get_pattern(pattern_id: str) -> AttackPattern | None:
    return PATTERNS.get(pattern_id)


def load_builtin_patterns() -> None:
    """导入内置模式模块，触发注册。"""
    from app.security import patterns  # noqa: F401


__all__ = [
    "CATEGORIES",
    "AttackPattern",
    "CaseDraft",
    "FindingDraft",
    "PATTERNS",
    "all_patterns",
    "get_pattern",
    "load_builtin_patterns",
    "register",
]
