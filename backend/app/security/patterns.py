"""内置攻击模式。

每个模式只回答两件事：这个项目适不适用、适用时生成什么。生成器负责幂等、
落库、验证队列；归因器负责把策略断言失败判成缺陷。
"""
from __future__ import annotations

import re
from typing import Any

from app.security.context import AttackContext
from app.security.deps import scan_dependencies
from app.security.registry import AttackPattern, CaseDraft, FindingDraft, register

_SECURITY = "security"


def _policy(ref: str, quote: str) -> dict[str, Any]:
    return {"source_type": "policy", "ref": ref, "quote": quote}


def _code(ref: str, quote: str) -> dict[str, Any]:
    return {"source_type": "code", "ref": ref, "quote": quote}


def _form_headers(ctx: AttackContext) -> dict[str, str]:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if ctx.base:
        headers["Origin"] = ctx.base
    return headers


def _admin_step(ctx: AttackContext, **kwargs: Any) -> dict[str, Any]:
    assertions = kwargs.pop("assertions", None)
    headers = dict(kwargs.pop("headers", {}))
    headers.update(ctx.admin_headers())
    step = {"fresh_client": True, "headers": headers, **kwargs}
    if assertions is not None:
        step["assert"] = assertions
    if not ctx.account("admin").get("token"):
        step["auth"] = ctx.admin_auth()
    return step


def _user_delete_endpoint(ctx: AttackContext):
    for endpoint in ctx.endpoints:
        if (
            endpoint.method.upper() == "POST"
            and endpoint.path.startswith("/admin/users/")
            and endpoint.path.endswith("/delete")
            and "{name}" in endpoint.path
        ):
            return endpoint
    return None


def _case(
    title: str,
    *,
    steps: list[dict[str, Any]],
    key: str,
    policy_ref: str,
    description: str,
    tags: list[str],
    evidence: list[dict[str, Any]],
) -> CaseDraft:
    spec = {
        "schema_version": "1.0",
        "title": title,
        "dimension": _SECURITY,
        "type": "api",
        "description": description,
        "steps": steps,
        "assertions": [],
        "evidence": evidence,
        "tags": tags,
        "attack": {"key": key, "policy": policy_ref},
    }
    return CaseDraft(
        title=title,
        spec=spec,
        tags=["attack-pack", f"attack:{key.split(':')[0]}", _SECURITY],
        priority=30,
        dimension=_SECURITY,
        attack_key=key,
        policy_ref=policy_ref,
    )


@register
class OpenRedirect(AttackPattern):
    id = "open-redirect"
    title = "登录跳转参数可被带到外部站点"
    category = "redirect"
    severity = "medium"
    description = "next / returnUrl / redirect 只判断「以 / 开头」时，//evil.example 会被浏览器解析成外站。"
    reference = "CWE-601"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        if ctx.login_endpoint is None:
            return False, "没有 POST /login"
        if not ctx.account("admin").get("password"):
            return False, "admin 账号没有口令，无法完成登录"
        return True, ""

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        login = ctx.login_endpoint
        assert login is not None
        title = "[攻击] next 参数不得跳转到外部站点"
        steps = [
            {
                "action": "request",
                "name": "next=//evil.example 登录",
                "method": "POST",
                "path": login.path,
                "headers": _form_headers(ctx),
                "body": {
                    "username": "{{account.admin.username}}",
                    "password": "{{account.admin.password}}",
                    "next": "//evil.example/attack-pack",
                },
                "assert": [
                    {
                        "kind": "status_code",
                        "operator": "eq",
                        "expected": 303,
                        "status": "confirmed",
                        "description": "凭据正确时登录成功",
                        "evidence": _code(login.path, "登录成功后 303 跳转"),
                    },
                    {
                        "kind": "text_contains",
                        "target": "headers.location",
                        "operator": "not_contains",
                        "expected": "//",
                        "status": "confirmed",
                        "description": "Location 必须是站内绝对路径；// 或 https:// 会被浏览器带到外部站点",
                        "evidence": _policy("SEC-OPEN-REDIRECT", "登录跳转不得离开本站"),
                    },
                ],
            }
        ]
        return [
            _case(
                title,
                steps=steps,
                key=f"open-redirect:{login.path}",
                policy_ref="SEC-OPEN-REDIRECT",
                description="登录跳转目标未限制为站内绝对路径。",
                tags=["redirect", "login"],
                evidence=[_policy("SEC-OPEN-REDIRECT", "登录跳转不得离开本站")],
            )
        ]


@register
class SessionRevocation(AttackPattern):
    id = "session-revocation"
    title = "改密 / 重置口令后旧会话仍然有效"
    category = "session"
    severity = "high"
    description = "改密不清理服务端会话时，账号被盗后改密无法把攻击者踢下线。"
    reference = "CWE-613"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        if ctx.login_endpoint is None:
            return False, "没有 POST /login"
        if ctx.reset_password_endpoint is None:
            return False, "没有管理员重置口令接口"
        if ctx.admin_create_user_endpoint is None and ctx.find_contains("POST", "/users") is None:
            return False, "没有创建临时用户的接口"
        if not ctx.has_admin:
            return False, "缺少 admin 账号"
        return True, ""

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        login = ctx.login_endpoint
        reset = ctx.reset_password_endpoint
        create = ctx.admin_create_user_endpoint
        assert login is not None and reset is not None and create is not None
        probe = "probe{{run_id}}"
        title = "[攻击] 重置口令后旧会话必须失效"
        steps = [
            _admin_step(
                ctx,
                action="request",
                name="创建一次性用户",
                method="POST",
                path=create.path,
                headers=_form_headers(ctx),
                body={
                    "username": probe,
                    "password": "Probe123!",
                    "displayName": "会话撤销探针",
                },
                assertions=[
                    {
                        "kind": "status_code",
                        "operator": "eq",
                        "expected": 303,
                        "status": "confirmed",
                        "description": "管理员创建临时用户成功",
                        "evidence": _code(create.path, "创建用户后 303 跳转"),
                    }
                ],
            ),
            {
                "action": "request",
                "name": "一次性用户登录",
                "method": "POST",
                "path": login.path,
                "headers": _form_headers(ctx),
                "body": {"username": probe, "password": "Probe123!", "next": "/"},
                "assert": [
                    {
                        "kind": "status_code",
                        "operator": "eq",
                        "expected": 303,
                        "status": "confirmed",
                        "description": "登录成功",
                        "evidence": _code(login.path, "登录成功 303"),
                    }
                ],
            },
            {
                "action": "request",
                "name": "确认旧会话可用",
                "method": "GET",
                "path": "/api/v1/me",
                "assert": [
                    {
                        "kind": "status_code",
                        "operator": "eq",
                        "expected": 200,
                        "status": "confirmed",
                        "description": "登录态可访问 /me",
                        "evidence": _policy("SEC-SESSION-REVOKE", "登录后会话应可用"),
                    }
                ],
            },
            _admin_step(
                ctx,
                action="request",
                name="管理员重置口令（独立会话）",
                method="POST",
                path=reset.path.replace("{name}", probe),
                headers=_form_headers(ctx),
                body={"password": "NewPass456!"},
                assertions=[
                    {
                        "kind": "status_code",
                        "operator": "eq",
                        "expected": 303,
                        "status": "confirmed",
                        "description": "重置口令成功",
                        "evidence": _code(reset.path, "重置口令成功 303"),
                    }
                ],
            ),
            {
                "action": "request",
                "name": "旧会话必须被拒绝",
                "method": "GET",
                "path": "/api/v1/me",
                "assert": [
                    {
                        "kind": "status_code",
                        "operator": "eq",
                        "expected": 401,
                        "status": "confirmed",
                        "description": "口令重置后旧会话立即失效",
                        "evidence": _policy("SEC-SESSION-REVOKE", "旧会话不得继续访问"),
                    }
                ],
            },
        ]
        delete = _user_delete_endpoint(ctx)
        cleanup = []
        if delete is not None:
            cleanup.append(
                _admin_step(
                    ctx,
                    action="request",
                    name="删除一次性用户",
                    method="POST",
                    path=delete.path.replace("{name}", probe),
                )
            )
        spec_extra: dict[str, Any] = {"cleanup": cleanup} if cleanup else {}
        draft = _case(
            title,
            steps=steps,
            key=f"session-revocation:{reset.path}",
            policy_ref="SEC-SESSION-REVOKE",
            description="改密后旧 Cookie 仍可访问受保护接口。",
            tags=["session", "credential-rotation"],
            evidence=[_policy("SEC-SESSION-REVOKE", "口令重置应撤销旧会话")],
        )
        draft.spec.update(spec_extra)
        return [draft]


@register
class AuthzMatrix(AttackPattern):
    id = "authz-matrix"
    title = "管理面 / 敏感接口缺少访问控制"
    category = "authz"
    severity = "high"
    description = "匿名或低权限账号能访问管理接口时，权限模型形同虚设。"
    reference = "CWE-284"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        if not ctx.admin_pages and ctx.find("GET", "/api/v1/me", exact=True) is None:
            return False, "没有发现管理页面或 /api/v1/me"
        return True, ""

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        drafts: list[CaseDraft] = []
        viewer = ctx.low_priv_account()
        for endpoint in ctx.admin_pages[:3]:
            slug = re.sub(r"[^a-z0-9]+", "-", endpoint.path.lower()).strip("-") or "admin"
            # 匿名访问管理页面：允许跳登录 302/303，或直接 401/403
            anon = {
                "action": "request",
                "name": f"匿名访问 {endpoint.path}",
                "method": "GET",
                "path": endpoint.path,
                "fresh_client": True,
                "assert": [
                    {
                        "kind": "status_code",
                        "operator": "in",
                        "expected": [302, 303, 401, 403],
                        "status": "confirmed",
                        "description": "未登录不得看到管理页面内容",
                        "evidence": _policy("SEC-AUTHZ-ANON", "管理页面必须先认证"),
                    }
                ],
            }
            drafts.append(
                _case(
                    f"[攻击] 匿名访问管理页面必须被拦截 - {endpoint.path}",
                    steps=[anon],
                    key=f"authz-anon:{endpoint.path}",
                    policy_ref="SEC-AUTHZ-ANON",
                    description="管理页面未要求认证或未拒绝匿名会话。",
                    tags=["authz", "anonymous"],
                    evidence=[_policy("SEC-AUTHZ-ANON", "管理页面必须先认证")],
                )
            )
            if not ctx.has_low_priv:
                continue
            low = {
                "action": "request",
                "name": f"低权限账号访问 {endpoint.path}",
                "method": "GET",
                "path": endpoint.path,
                "fresh_client": True,
                "auth": {"mode": "basic", "account": viewer},
                "assert": [
                    {
                        "kind": "status_code",
                        "operator": "in",
                        "expected": [401, 403],
                        "status": "confirmed",
                        "description": "非管理员不得进入管理面",
                        "evidence": _policy("SEC-AUTHZ-ROLE", "管理面只允许管理员"),
                    }
                ],
            }
            drafts.append(
                _case(
                    f"[攻击] 低权限账号访问管理页面必须 403 - {endpoint.path}",
                    steps=[low],
                    key=f"authz-role:{viewer}:{endpoint.path}",
                    policy_ref="SEC-AUTHZ-ROLE",
                    description="普通账号可以进入管理员页面。",
                    tags=["authz", "role"],
                    evidence=[_policy("SEC-AUTHZ-ROLE", "管理面只允许管理员")],
                )
            )
        me = ctx.find("GET", "/api/v1/me", exact=True)
        if me is not None:
            drafts.append(
                _case(
                    "[攻击] 匿名访问 /api/v1/me 必须 401",
                    steps=[
                        {
                            "action": "request",
                            "name": "匿名访问 /me",
                            "method": "GET",
                            "path": me.path,
                            "fresh_client": True,
                            "assert": [
                                {
                                    "kind": "status_code",
                                    "operator": "in",
                                    "expected": [401, 403],
                                    "status": "confirmed",
                                    "description": "匿名不得读取当前用户信息",
                                    "evidence": _policy("SEC-AUTHZ-ANON", "身份接口必须认证"),
                                }
                            ],
                        }
                    ],
                    key="authz-anon:/api/v1/me",
                    policy_ref="SEC-AUTHZ-ANON",
                    description="匿名可以读取当前用户信息。",
                    tags=["authz", "anonymous"],
                    evidence=[_policy("SEC-AUTHZ-ANON", "身份接口必须认证")],
                )
            )
        return drafts


@register
class Csrf(AttackPattern):
    id = "csrf"
    title = "管理操作缺少跨站来源校验"
    category = "csrf"
    severity = "high"
    description = "浏览器跨站表单会带 Origin；不校验就可能在已登录状态下被借刀操作。"
    reference = "CWE-352"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        if ctx.admin_create_user_endpoint is None:
            return False, "没有管理端创建用户接口"
        if not ctx.has_admin:
            return False, "缺少 admin 账号"
        return True, ""

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        endpoint = ctx.admin_create_user_endpoint
        assert endpoint is not None
        step = _admin_step(
            ctx,
            action="request",
            name="带外站 Origin 创建用户",
            method="POST",
            path=endpoint.path,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "http://evil.example",
            },
            body={"username": "csrf{{run_id}}", "password": "x", "displayName": "csrf"},
            assertions=[
                {
                    "kind": "status_code",
                    "operator": "eq",
                    "expected": 403,
                    "status": "confirmed",
                    "description": "跨站来源必须被拒绝",
                    "evidence": _policy("SEC-CSRF", "管理写操作必须校验 Origin/Referer"),
                }
            ],
        )
        return [
            _case(
                "[攻击] 管理写操作必须拒绝外站 Origin",
                steps=[step],
                key=f"csrf:{endpoint.path}",
                policy_ref="SEC-CSRF",
                description="管理写操作只依赖会话 Cookie，未校验请求来源。",
                tags=["csrf", "admin"],
                evidence=[_policy("SEC-CSRF", "管理写操作必须校验 Origin/Referer")],
            )
        ]


@register
class PathTraversal(AttackPattern):
    id = "path-traversal"
    title = "下载 / 文件接口存在路径穿越"
    category = "traversal"
    severity = "high"
    description = "文件名直接拼路径时，../ 可以读到备份目录之外的数据。"
    reference = "CWE-22"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        if ctx.admin_download_endpoint is None:
            return False, "没有带 {name} 的下载接口"
        if not ctx.has_admin:
            return False, "缺少 admin 账号"
        return True, ""

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        endpoint = ctx.admin_download_endpoint
        assert endpoint is not None
        payload = "..%2F..%2Fstate%2Fdm-git.json"
        step = _admin_step(
            ctx,
            action="request",
            name="下载接口注入穿越文件名",
            method="GET",
            path=endpoint.path.replace("{name}", payload),
            assertions=[
                {
                    "kind": "status_code",
                    "operator": "in",
                    "expected": [400, 403, 404],
                    "status": "confirmed",
                    "description": "穿越文件名不得被当作合法归档读取",
                    "evidence": _policy("SEC-TRAVERSAL", "文件参数不得跳出允许目录"),
                }
            ],
        )
        return [
            _case(
                "[攻击] 下载接口不得接受 ../ 文件名",
                steps=[step],
                key=f"path-traversal:{endpoint.path}",
                policy_ref="SEC-TRAVERSAL",
                description="下载接口可能把 name 直接拼到目录后读取。",
                tags=["traversal", "download"],
                evidence=[_policy("SEC-TRAVERSAL", "文件参数不得跳出允许目录")],
            )
        ]


@register
class DefaultCredentials(AttackPattern):
    id = "default-credentials"
    title = "默认口令可登录"
    category = "session"
    severity = "critical"
    description = "出厂/示例口令未改，攻击者无需漏洞即可拿到管理员。"
    reference = "CWE-1392"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        if ctx.login_endpoint is None:
            return False, "没有 POST /login"
        return True, ""

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        login = ctx.login_endpoint
        assert login is not None
        steps = []
        for password in ("admin", "123456", "password"):
            steps.append(
                {
                    "action": "request",
                    "name": f"尝试默认口令 {password}",
                    "method": "POST",
                    "path": login.path,
                    "fresh_client": True,
                    "headers": _form_headers(ctx),
                    "body": {"username": "admin", "password": password, "next": "/"},
                    "assert": [
                        {
                            "kind": "status_code",
                            "operator": "in",
                            "expected": [200, 401, 403],
                            "status": "confirmed",
                            "description": "默认口令不得登录成功",
                            "evidence": _policy("SEC-DEFAULT-CRED", "不得保留可登录的默认口令"),
                        }
                    ],
                }
            )
        return [
            _case(
                "[攻击] admin 不得使用默认口令",
                steps=steps,
                key="default-credentials:admin",
                policy_ref="SEC-DEFAULT-CRED",
                description="管理员账号可用常见默认口令登录。",
                tags=["credentials", "default"],
                evidence=[_policy("SEC-DEFAULT-CRED", "不得保留可登录的默认口令")],
            )
        ]


@register
class DependencyScan(AttackPattern):
    id = "dependency-vulns"
    title = "依赖 / 工具链已知漏洞"
    category = "supply-chain"
    severity = "high"
    description = "直接依赖与 Go 工具链版本通过 OSV 查询已知漏洞；命中的直接落缺陷，不生成无法执行的用例。"
    reference = "OSV"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        manifests = [
            path
            for root in ctx.code_roots
            for path in (root / "go.mod", root / "package.json", root / "requirements.txt", root / "pom.xml")
            if path.is_file()
        ]
        if not manifests:
            return False, "代码源里没有依赖清单"
        return True, ""

    def findings(self, ctx: AttackContext) -> list[FindingDraft]:
        report = scan_dependencies(ctx)
        ctx.scratch["dependency_report"] = report
        return report.findings


@register
class IdorProject(AttackPattern):
    id = "idor-project"
    title = "私有项目可被非成员读取（IDOR）"
    category = "authz"
    severity = "high"
    description = "对象 ID 可枚举时，必须同时校验「这个对象属于当前用户」。"
    reference = "CWE-639"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        if ctx.find("POST", "/new", exact=True) is None:
            return False, "没有创建项目的入口"
        if ctx.find_contains("GET", "/p/") is None:
            return False, "没有项目页面路由"
        if not ctx.has_admin or not ctx.has_low_priv:
            return False, "需要 admin 与一个普通账号"
        return True, ""

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        create = ctx.find("POST", "/new", exact=True)
        page = ctx.find_contains("GET", "/p/")
        assert create is not None and page is not None
        admin = ctx.account("admin")
        admin_name = str(admin.get("username") or "admin")
        name = "idor{{run_id}}"
        page_path = page.path.replace("{ns}", admin_name).replace("{name}", name)
        project_api = f"/api/v1/projects/{admin_name}/{name}"
        delete_path = page_path + "/delete"
        title = "[攻击] 非成员不得读取私有项目"
        steps = [
            _admin_step(
                ctx,
                action="request",
                name="管理员创建私有项目",
                method="POST",
                path=create.path,
                headers=_form_headers(ctx),
                body={
                    "namespace": admin_name,
                    "name": name,
                    "description": "idor probe",
                    "private": "on",
                },
                assertions=[
                    {
                        "kind": "status_code",
                        "operator": "eq",
                        "expected": 303,
                        "status": "confirmed",
                        "description": "私有项目创建成功",
                        "evidence": _code(create.path, "创建项目成功后 303"),
                    }
                ],
            ),
            {
                "action": "request",
                "name": "普通账号通过 API 读取私有项目",
                "method": "GET",
                "path": project_api,
                "fresh_client": True,
                "auth": {"mode": "basic", "account": ctx.low_priv_account()},
                "assert": [
                    {
                        "kind": "status_code",
                        "operator": "in",
                        "expected": [401, 403, 404],
                        "status": "confirmed",
                        "description": "非成员不得读取私有项目元数据",
                        "evidence": _policy("SEC-IDOR", "对象访问必须校验归属"),
                    }
                ],
            },
            {
                "action": "request",
                "name": "普通账号通过页面读取私有项目",
                "method": "GET",
                "path": page_path,
                "fresh_client": True,
                "auth": {"mode": "basic", "account": ctx.low_priv_account()},
                "assert": [
                    {
                        "kind": "status_code",
                        "operator": "in",
                        "expected": [401, 403],
                        "status": "confirmed",
                        "description": "非成员不得打开私有项目页面",
                        "evidence": _policy("SEC-IDOR", "对象访问必须校验归属"),
                    }
                ],
            },
        ]
        draft = _case(
            title,
            steps=steps,
            key="idor-project:viewer",
            policy_ref="SEC-IDOR",
            description="普通账号可以直接读取管理员私有项目。",
            tags=["idor", "project"],
            evidence=[_policy("SEC-IDOR", "对象访问必须校验归属")],
        )
        draft.spec["cleanup"] = [
            _admin_step(
                ctx,
                action="request",
                name="删除私有项目",
                method="POST",
                path=delete_path,
            )
        ]
        return [draft]


@register
class DuplicateSubmit(AttackPattern):
    id = "duplicate-submit"
    title = "重复提交创建出重复对象"
    category = "logic"
    severity = "medium"
    description = "重复提交必须被唯一约束或幂等键挡住；第二次仍返回成功就是重复对象。"
    reference = "CWE-837"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        if ctx.find("POST", "/new", exact=True) is None:
            return False, "没有创建项目的入口"
        if not ctx.has_admin:
            return False, "需要 admin 账号"
        return True, ""

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        create = ctx.find("POST", "/new", exact=True)
        page = ctx.find_contains("GET", "/p/")
        assert create is not None and page is not None
        admin_name = str(ctx.account("admin").get("username") or "admin")
        name = "dup{{run_id}}"
        page_path = page.path.replace("{ns}", admin_name).replace("{name}", name)
        title = "[攻击] 重复提交不得创建重复项目"
        steps = [
            _admin_step(
                ctx,
                action="request",
                name="第一次创建项目",
                method="POST",
                path=create.path,
                headers=_form_headers(ctx),
                body={"namespace": admin_name, "name": name, "description": "duplicate probe"},
                assertions=[
                    {
                        "kind": "status_code",
                        "operator": "eq",
                        "expected": 303,
                        "status": "confirmed",
                        "description": "第一次创建成功",
                        "evidence": _code(create.path, "创建项目成功 303"),
                    }
                ],
            ),
            _admin_step(
                ctx,
                action="request",
                name="第二次提交同名项目",
                method="POST",
                path=create.path,
                headers=_form_headers(ctx),
                body={"namespace": admin_name, "name": name, "description": "duplicate probe"},
                assertions=[
                    {
                        "kind": "status_code",
                        "operator": "not_in",
                        "expected": [201, 303],
                        "status": "confirmed",
                        "description": "重复提交不得再返回成功跳转",
                        "evidence": _policy("SEC-DUP-SUBMIT", "同名对象只能创建一次"),
                    }
                ],
            ),
        ]
        draft = _case(
            title,
            steps=steps,
            key="duplicate-submit:new",
            policy_ref="SEC-DUP-SUBMIT",
            description="同一创建请求提交两次仍返回成功。",
            tags=["duplicate", "idempotency"],
            evidence=[_policy("SEC-DUP-SUBMIT", "同名对象只能创建一次")],
        )
        draft.spec["cleanup"] = [
            _admin_step(
                ctx,
                action="request",
                name="删除重复项目",
                method="POST",
                path=page_path + "/delete",
            )
        ]
        return [draft]


_URL_FIELD = re.compile(r"(?i)(url|uri|webhook|callback|remote|target_url|proxy|endpoint)")


@register
class SsrfUrlParam(AttackPattern):
    id = "ssrf"
    title = "URL 参数可被指向内网（SSRF）"
    category = "injection"
    severity = "high"
    description = "服务端代填 URL 时，必须拒绝回环、内网、云元数据地址。"
    reference = "CWE-918"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        for endpoint in ctx.endpoints:
            schema = endpoint.request_body if isinstance(endpoint.request_body, dict) else {}
            props = schema.get("properties") if isinstance(schema.get("properties"), dict) else schema
            if isinstance(props, dict) and any(_URL_FIELD.search(str(key)) for key in props):
                return True, ""
        return False, "接口清单里没有 URL / webhook / 代理类参数"

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        for endpoint in ctx.endpoints:
            schema = endpoint.request_body if isinstance(endpoint.request_body, dict) else {}
            props = schema.get("properties") if isinstance(schema.get("properties"), dict) else schema
            if not isinstance(props, dict):
                continue
            field = next((str(key) for key in props if _URL_FIELD.search(str(key))), "")
            if not field:
                continue
            step = _admin_step(
                ctx,
                action="request",
                name=f"把 {field} 指向回环地址",
                method=endpoint.method,
                path=endpoint.path,
                headers={"Content-Type": "application/json"},
                body={field: "http://127.0.0.1:1/ssrf-canary"},
                assertions=[
                    {
                        "kind": "status_code",
                        "operator": "not_in",
                        "expected": [500, 502, 503, 504],
                        "status": "confirmed",
                        "description": "内网地址不得触发服务端 5xx",
                        "evidence": _policy("SEC-SSRF", "URL 参数必须拒绝内网/回环地址"),
                    }
                ],
            )
            return [
                _case(
                    f"[攻击] {field} 参数不得接受回环地址",
                    steps=[step],
                    key=f"ssrf:{endpoint.path}:{field}",
                    policy_ref="SEC-SSRF",
                    description="URL 参数可能被用来访问内网服务。",
                    tags=["ssrf", "url"],
                    evidence=[_policy("SEC-SSRF", "URL 参数必须拒绝内网/回环地址")],
                )
            ]
        return []


@register
class UploadFilename(AttackPattern):
    id = "upload-filename"
    title = "上传文件名未过滤穿越 / 可执行后缀"
    category = "traversal"
    severity = "high"
    description = "上传文件按原始文件名落盘时，../ 和 .php/.sh 后缀会变成写入或执行。"
    reference = "CWE-434"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        for endpoint in ctx.endpoints:
            text = f"{endpoint.path} {endpoint.summary}".lower()
            if any(hint in text for hint in ("upload", "attachment", "import", "avatar")):
                return True, ""
            schema = endpoint.request_body if isinstance(endpoint.request_body, dict) else {}
            props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            if isinstance(props, dict) and any(key in props for key in ("file", "filename", "attachment")):
                return True, ""
        return False, "没有文件上传接口"

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        for endpoint in ctx.endpoints:
            text = f"{endpoint.path} {endpoint.summary}".lower()
            if not any(hint in text for hint in ("upload", "attachment", "import", "avatar")):
                continue
            step = {
                "action": "request",
                "name": "上传穿越文件名",
                "method": endpoint.method,
                "path": endpoint.path,
                "fresh_client": True,
                "headers": {"Content-Type": "application/json"},
                "body": {"filename": "../../evil.sh", "content": "canary"},
                "assert": [
                    {
                        "kind": "status_code",
                        "operator": "in",
                        "expected": [400, 403, 422],
                        "status": "confirmed",
                        "description": "穿越文件名必须被拒绝",
                        "evidence": _policy("SEC-UPLOAD-NAME", "上传文件名不得包含路径分隔符"),
                    }
                ],
            }
            return [
                _case(
                    "[攻击] 上传接口不得接受 ../ 文件名",
                    steps=[step],
                    key=f"upload-filename:{endpoint.path}",
                    policy_ref="SEC-UPLOAD-NAME",
                    description="上传文件名未过滤路径分隔符。",
                    tags=["upload", "filename"],
                    evidence=[_policy("SEC-UPLOAD-NAME", "上传文件名不得包含路径分隔符")],
                )
            ]
        return []


@register
class RaceProjectCreate(AttackPattern):
    id = "race-condition"
    title = "并发重复提交绕过唯一约束（竞态）"
    category = "logic"
    severity = "high"
    description = "两个请求同时通过「不存在」检查时，会创建出重复对象或覆盖彼此。"
    reference = "CWE-362"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        if ctx.find("POST", "/new", exact=True) is None:
            return False, "没有创建项目的入口"
        if not ctx.account("admin").get("token"):
            return False, "admin 没有令牌，无法在并发请求里认证"
        if ctx.find_contains("GET", "/p/") is None:
            return False, "没有项目页面路由"
        return True, ""

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        create = ctx.find("POST", "/new", exact=True)
        page = ctx.find_contains("GET", "/p/")
        assert create is not None and page is not None
        admin_name = str(ctx.account("admin").get("username") or "admin")
        name = "race{{run_id}}"
        headers = {**_form_headers(ctx), **ctx.admin_headers()}
        step = {
            "action": "parallel",
            "name": "并发提交 5 次同名项目创建",
            "parallel": {
                "count": 5,
                "request": {
                    "method": "POST",
                    "path": create.path,
                    "headers": headers,
                    "body": {
                        "namespace": admin_name,
                        "name": name,
                        "description": "race probe",
                    },
                },
            },
            "assert": [
                {
                    "kind": "json_path",
                    "target": "$.statuses",
                    "operator": "eq",
                    "expected": [200, 200, 200, 200, 303],
                    "status": "confirmed",
                    "description": "5 个并发请求只能有 1 个成功跳转，其余必须被冲突拒绝",
                    "evidence": _policy("SEC-RACE", "并发创建同名对象只能成功一次"),
                }
            ],
        }
        page_path = page.path.replace("{ns}", admin_name).replace("{name}", name)
        draft = _case(
            "[攻击] 并发创建同名项目只能成功一次",
            steps=[step],
            key="race-condition:new",
            policy_ref="SEC-RACE",
            description="并发提交同名创建请求绕过了唯一性检查。",
            tags=["race", "duplicate"],
            evidence=[_policy("SEC-RACE", "并发创建同名对象只能成功一次")],
        )
        draft.spec["cleanup"] = [
            _admin_step(
                ctx,
                action="request",
                name="删除竞态测试项目",
                method="POST",
                path=page_path + "/delete",
            )
        ]
        return [draft]


_API_RESOURCE = re.compile(r'seg\[0\]\s*==\s*"([A-Za-z0-9-]+)"')


def _api_resources(ctx: AttackContext) -> list[str]:
    """从 Go 的 /api/v1/ 动态分发里抽资源名：case seg[0] == "users"。"""
    found: list[str] = []
    for root in ctx.code_roots:
        for path in root.rglob("*.go"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in _API_RESOURCE.finditer(text):
                name = match.group(1)
                if name not in found:
                    found.append(name)
    return found


@register
class ApiAuthz(AttackPattern):
    id = "api-authz"
    title = "REST API 资源缺少认证 / 角色校验"
    category = "authz"
    severity = "high"
    description = "动态分发的 /api/v1/<resource> 最容易只判断登录、不判断角色。"
    reference = "CWE-284"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        if ctx.login_endpoint is None and not ctx.has_admin:
            return False, "缺少登录 / 管理员凭据"
        if not _api_resources(ctx):
            return False, "代码里没有 /api/v1 动态资源分发"
        return True, ""

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        resources = _api_resources(ctx)
        admin_only = {"users", "backups", "backup-targets"}
        # projects 的 GET 允许匿名读公开项目；repositories 是公开仓库列表。
        public_by_design = {"repositories", "version", "projects", ""}
        drafts: list[CaseDraft] = []
        for resource in resources:
            if resource in public_by_design:
                continue
            path = f"/api/v1/{resource}"
            drafts.append(
                _case(
                    f"[攻击] 匿名访问 /api/v1/{resource} 必须 401",
                    steps=[
                        {
                            "action": "request",
                            "name": f"匿名 GET {path}",
                            "method": "GET",
                            "path": path,
                            "fresh_client": True,
                            "assert": [
                                {
                                    "kind": "status_code",
                                    "operator": "in",
                                    "expected": [401, 403],
                                    "status": "confirmed",
                                    "description": "匿名不得访问 API 资源",
                                    "evidence": _policy("SEC-AUTHZ-ANON", "API 资源必须先认证"),
                                }
                            ],
                        }
                    ],
                    key=f"api-authz:anon:{resource}",
                    policy_ref="SEC-AUTHZ-ANON",
                    description=f"/api/v1/{resource} 可能允许匿名访问。",
                    tags=["authz", "api"],
                    evidence=[_policy("SEC-AUTHZ-ANON", "API 资源必须先认证")],
                )
            )
            if resource not in admin_only or not ctx.has_low_priv:
                continue
            drafts.append(
                _case(
                    f"[攻击] 普通账号访问 /api/v1/{resource} 必须 403",
                    steps=[
                        {
                            "action": "request",
                            "name": f"低权限 GET {path}",
                            "method": "GET",
                            "path": path,
                            "fresh_client": True,
                            "auth": {"mode": "basic", "account": ctx.low_priv_account()},
                            "assert": [
                                {
                                    "kind": "status_code",
                                    "operator": "in",
                                    "expected": [401, 403],
                                    "status": "confirmed",
                                    "description": "非管理员不得读取管理 API",
                                    "evidence": _policy("SEC-AUTHZ-ROLE", "管理 API 只允许管理员"),
                                }
                            ],
                        }
                    ],
                    key=f"api-authz:role:{resource}",
                    policy_ref="SEC-AUTHZ-ROLE",
                    description=f"普通账号可以读取管理 API /api/v1/{resource}。",
                    tags=["authz", "api", "role"],
                    evidence=[_policy("SEC-AUTHZ-ROLE", "管理 API 只允许管理员")],
                )
            )
        return drafts[:8]


@register
class CookieFlags(AttackPattern):
    id = "cookie-flags"
    title = "会话 Cookie 缺少安全属性"
    category = "transport"
    severity = "medium"
    description = "HttpOnly 防脚本读取，SameSite 降低 CSRF，HTTPS 下 Secure 防明文回传。"
    reference = "CWE-1004"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        if ctx.login_endpoint is None or not ctx.account("admin").get("password"):
            return False, "缺少登录接口或 admin 口令"
        return True, ""

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        login = ctx.login_endpoint
        assert login is not None
        assertions = [
            {
                "kind": "text_contains",
                "target": "headers.set-cookie",
                "operator": "contains",
                "expected": "HttpOnly",
                "status": "confirmed",
                "description": "会话 Cookie 必须 HttpOnly",
                "evidence": _policy("SEC-COOKIE-HTTPONLY", "会话 Cookie 必须 HttpOnly"),
            },
            {
                "kind": "text_contains",
                "target": "headers.set-cookie",
                "operator": "contains",
                "expected": "SameSite",
                "status": "confirmed",
                "description": "会话 Cookie 必须声明 SameSite",
                "evidence": _policy("SEC-COOKIE-SAMESITE", "会话 Cookie 必须声明 SameSite"),
            },
        ]
        if str(ctx.base).lower().startswith("https://"):
            assertions.append(
                {
                    "kind": "text_contains",
                    "target": "headers.set-cookie",
                    "operator": "contains",
                    "expected": "Secure",
                    "status": "confirmed",
                    "description": "HTTPS 下会话 Cookie 必须 Secure",
                    "evidence": _policy("SEC-COOKIE-SECURE", "HTTPS 会话 Cookie 必须 Secure"),
                }
            )
        return [
            _case(
                "[攻击] 会话 Cookie 必须带安全属性",
                steps=[
                    {
                        "action": "request",
                        "name": "登录并检查 Set-Cookie",
                        "method": "POST",
                        "path": login.path,
                        "fresh_client": True,
                        "headers": _form_headers(ctx),
                        "body": {
                            "username": "{{account.admin.username}}",
                            "password": "{{account.admin.password}}",
                            "next": "/",
                        },
                        "assert": [
                            {
                                "kind": "status_code",
                                "operator": "eq",
                                "expected": 303,
                                "status": "confirmed",
                                "description": "登录成功并下发 Cookie",
                                "evidence": _code(login.path, "登录成功 303"),
                            },
                            *assertions,
                        ],
                    }
                ],
                key="cookie-flags:login",
                policy_ref="SEC-COOKIE-HTTPONLY",
                description="登录 Cookie 缺少 HttpOnly / SameSite / Secure。",
                tags=["cookie", "session"],
                evidence=[_policy("SEC-COOKIE-HTTPONLY", "会话 Cookie 必须 HttpOnly")],
            )
        ]


@register
class LoginRateLimit(AttackPattern):
    id = "rate-limit"
    title = "登录接口没有失败次数限制"
    category = "session"
    severity = "medium"
    description = "没有 429 / 423 / 锁定策略时，在线爆破没有成本。"
    reference = "CWE-307"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        if ctx.login_endpoint is None:
            return False, "没有 POST /login"
        if ctx.admin_create_user_endpoint is None or not ctx.has_admin:
            return False, "需要管理员创建一次性账号"
        return True, ""

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        login = ctx.login_endpoint
        create = ctx.admin_create_user_endpoint
        assert login is not None and create is not None
        user = "rate{{run_id}}"
        steps: list[dict[str, Any]] = [
            _admin_step(
                ctx,
                action="request",
                name="创建一次性账号",
                method="POST",
                path=create.path,
                headers=_form_headers(ctx),
                body={"username": user, "password": "RateProbe1!", "displayName": "rate probe"},
                assertions=[
                    {
                        "kind": "status_code",
                        "operator": "eq",
                        "expected": 303,
                        "status": "confirmed",
                        "description": "一次性账号创建成功",
                        "evidence": _code(create.path, "创建用户 303"),
                    }
                ],
            )
        ]
        for index in range(10):
            steps.append(
                {
                    "action": "request",
                    "name": f"第 {index + 1} 次错误口令登录",
                    "method": "POST",
                    "path": login.path,
                    "fresh_client": True,
                    "headers": _form_headers(ctx),
                    "body": {"username": user, "password": "WrongPass!", "next": "/"},
                    "assert": [],
                }
            )
        steps[-1]["assert"] = [
            {
                "kind": "status_code",
                "operator": "in",
                "expected": [423, 429],
                "status": "confirmed",
                "description": "连续失败后必须限速或锁定",
                "evidence": _policy("SEC-RATE-LIMIT", "登录失败必须有 429/423 或锁定策略"),
            }
        ]
        draft = _case(
            "[攻击] 连续错误登录必须被限速或锁定",
            steps=steps,
            key="rate-limit:login",
            policy_ref="SEC-RATE-LIMIT",
            description="登录接口对失败次数没有限制。",
            tags=["rate-limit", "login"],
            evidence=[_policy("SEC-RATE-LIMIT", "登录失败必须有 429/423 或锁定策略")],
        )
        delete = _user_delete_endpoint(ctx)
        if delete is not None:
            draft.spec["cleanup"] = [
                _admin_step(
                    ctx,
                    action="request",
                    name="删除一次性账号",
                    method="POST",
                    path=delete.path.replace("{name}", user),
                )
            ]
        return [draft]


@register
class SecurityHeaders(AttackPattern):
    id = "security-headers"
    title = "缺少基础安全响应头"
    category = "transport"
    severity = "low"
    description = "X-Content-Type-Options / X-Frame-Options / HSTS 缺失会放大 XSS 与点击劫持。"
    reference = "CWE-693"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        if ctx.find("GET", "/", exact=True) is None:
            return False, "没有 GET /"
        return True, ""

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        assertions = [
            {
                "kind": "text_contains",
                "target": "headers.x-content-type-options",
                "operator": "contains",
                "expected": "nosniff",
                "status": "confirmed",
                "description": "必须禁止 MIME 嗅探",
                "evidence": _policy("SEC-HEADER-XCTO", "响应必须带 X-Content-Type-Options: nosniff"),
            },
            {
                "kind": "text_contains",
                "target": "headers.x-frame-options",
                "operator": "contains",
                "expected": "SAMEORIGIN",
                "status": "confirmed",
                "description": "必须限制被第三方页面嵌套",
                "evidence": _policy("SEC-HEADER-XFO", "响应必须带 X-Frame-Options"),
            },
        ]
        if str(ctx.base).lower().startswith("https://"):
            assertions.append(
                {
                    "kind": "text_contains",
                    "target": "headers.strict-transport-security",
                    "operator": "contains",
                    "expected": "max-age",
                    "status": "confirmed",
                    "description": "HTTPS 下必须启用 HSTS",
                    "evidence": _policy("SEC-HEADER-HSTS", "HTTPS 响应必须带 HSTS"),
                }
            )
        return [
            _case(
                "[攻击] 首页必须带基础安全响应头",
                steps=[
                    {
                        "action": "request",
                        "name": "检查首页响应头",
                        "method": "GET",
                        "path": "/",
                        "fresh_client": True,
                        "assert": assertions,
                    }
                ],
                key="security-headers:root",
                policy_ref="SEC-HEADER-XCTO",
                description="首页缺少 X-Content-Type-Options 或 X-Frame-Options。",
                tags=["headers", "transport"],
                evidence=[_policy("SEC-HEADER-XCTO", "响应必须带基础安全头")],
            )
        ]


def _npm_hosted_repos(ctx: AttackContext) -> list[str]:
    import json

    names: list[str] = []
    for root in ctx.code_roots:
        path = root / "config.json"
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        repos = doc.get("npmRepositories") or doc.get("npm") or []
        for repo in repos:
            if isinstance(repo, dict) and repo.get("type") == "hosted" and repo.get("name"):
                names.append(str(repo["name"]))
    return list(dict.fromkeys(names))


@register
class FuzzJson(AttackPattern):
    id = "fuzz-json"
    title = "JSON 写接口对畸形请求体返回 5xx"
    category = "injection"
    severity = "low"
    description = "客户端格式错误属于 4xx；返回 500 说明解析异常被当成了内部故障。"
    reference = "CWE-20"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        if _api_resources(ctx) or _npm_hosted_repos(ctx):
            return True, ""
        return False, "没有 JSON 写接口"

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        drafts: list[CaseDraft] = []
        resources = [item for item in _api_resources(ctx) if item in {"users", "projects"}]
        for resource in resources[:2]:
            path = f"/api/v1/{resource}"
            step = _admin_step(
                ctx,
                action="request",
                name=f"向 {path} 发送截断 JSON",
                method="POST",
                path=path,
                headers={"Content-Type": "application/json"},
                body="{bad",
                assertions=[
                    {
                        "kind": "status_code",
                        "operator": "in",
                        "expected": [400, 422],
                        "status": "confirmed",
                        "description": "非法 JSON 必须按客户端错误返回",
                        "evidence": _policy("SEC-INPUT-500", "格式错误不得返回 5xx"),
                    }
                ],
            )
            drafts.append(
                _case(
                    f"[攻击] 非法 JSON 不得让接口 500 - POST {path}",
                    steps=[step],
                    key=f"fuzz-json:{path.split('/')[-1]}",
                    policy_ref="SEC-INPUT-500",
                    description="畸形 JSON 触发了服务端 5xx。",
                    tags=["fuzz", "json"],
                    evidence=[_policy("SEC-INPUT-500", "格式错误不得返回 5xx")],
                )
            )
        for repo in _npm_hosted_repos(ctx)[:2]:
            step = _admin_step(
                ctx,
                action="request",
                name=f"向 npm 仓库 {repo} 发布截断 JSON",
                method="PUT",
                path=f"/repository/{repo}/shakedown-fuzz-{{{{run_id}}}}",
                headers={"Content-Type": "application/json"},
                body="{bad",
                assertions=[
                    {
                        "kind": "status_code",
                        "operator": "in",
                        "expected": [400, 422],
                        "status": "confirmed",
                        "description": "非法包文档必须按客户端错误返回",
                        "evidence": _policy("SEC-INPUT-500", "格式错误不得返回 5xx"),
                    }
                ],
            )
            drafts.append(
                _case(
                    f"[攻击] NPM 发布畸形 JSON 不得 500 - {repo}",
                    steps=[step],
                    key=f"fuzz-json:{repo}",
                    policy_ref="SEC-INPUT-500",
                    description="NPM 发布接口把解析错误当成内部故障。",
                    tags=["fuzz", "json", "npm"],
                    evidence=[_policy("SEC-INPUT-500", "格式错误不得返回 5xx")],
                )
            )
        return drafts


@register
class DifferentialAuthz(AttackPattern):
    id = "idor-differential"
    title = "两个普通账号交叉读取私有项目"
    category = "authz"
    severity = "high"
    description = "同一资源换一个普通账号访问，比较响应；只测匿名/管理员会漏掉横向越权。"
    reference = "CWE-639"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        accounts = [
            name
            for name in ("alice", "viewer", "user", "developer")
            if (ctx.accounts.get(name) or {}).get("password")
        ]
        if len(accounts) < 2:
            return False, "需要两个有口令的普通账号"
        if ctx.find("POST", "/new", exact=True) is None or ctx.find_contains("GET", "/p/") is None:
            return False, "没有项目创建 / 页面路由"
        return True, ""

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        names = [
            name
            for name in ("alice", "viewer", "user", "developer")
            if (ctx.accounts.get(name) or {}).get("password")
        ][:2]
        owner, attacker = names[0], names[1]
        create = ctx.find("POST", "/new", exact=True)
        page = ctx.find_contains("GET", "/p/")
        assert create is not None and page is not None

        def project_path(user: str, suffix: str = "") -> str:
            name = f"dfx{{{{run_id}}}}{suffix}"
            return page.path.replace("{ns}", user).replace("{name}", name)

        owner_proj = project_path(owner, "a")
        attacker_proj = project_path(attacker, "b")
        owner_api = f"/api/v1/projects/{owner}/dfx{{{{run_id}}}}a"
        attacker_api = f"/api/v1/projects/{attacker}/dfx{{{{run_id}}}}b"
        git_path = f"/{owner}/dfx{{{{run_id}}}}a.git/info/refs"

        def request_step(
            name: str,
            method: str,
            path: str,
            *,
            account: str = "",
            query: dict[str, str] | None = None,
            body: dict[str, Any] | None = None,
            expected: list[int] | None = None,
            policy: str = "",
            description: str = "",
        ) -> dict[str, Any]:
            step: dict[str, Any] = {
                "action": "request",
                "name": name,
                "method": method,
                "path": path,
                "fresh_client": True,
                "assert": [],
            }
            if account:
                step["auth"] = {"mode": "basic", "account": account}
            if query:
                step["query"] = query
            if body is not None:
                step["headers"] = _form_headers(ctx)
                step["body"] = body
            if expected:
                step["assert"] = [
                    {
                        "kind": "status_code",
                        "operator": "in",
                        "expected": expected,
                        "status": "confirmed",
                        "description": description,
                        "evidence": _policy(policy or "SEC-IDOR-DIFF", "对象访问必须校验归属"),
                    }
                ]
            return step

        steps = [
            request_step(
                f"{owner} 创建私有项目",
                "POST",
                create.path,
                account=owner,
                body={"namespace": owner, "name": "dfx{{run_id}}a", "description": "diff probe", "private": "on"},
                expected=[303],
                policy="SEC-IDOR-DIFF",
                description="项目所有者创建成功",
            ),
            request_step(
                f"{attacker} 读取 {owner} 的项目页面",
                "GET",
                owner_proj,
                account=attacker,
                expected=[401, 403],
                description="非成员不得打开私有项目页面",
            ),
            request_step(
                f"{attacker} 读取 {owner} 的项目 API",
                "GET",
                owner_api,
                account=attacker,
                expected=[401, 403],
                description="非成员不得读取私有项目元数据",
            ),
            request_step(
                f"{attacker} 通过 git 协议读取 {owner} 的仓库",
                "GET",
                git_path,
                account=attacker,
                query={"service": "git-upload-pack"},
                expected=[401, 403],
                description="非成员不得 clone 私有仓库",
            ),
            request_step(
                f"{attacker} 创建自己的私有项目",
                "POST",
                create.path,
                account=attacker,
                body={"namespace": attacker, "name": "dfx{{run_id}}b", "description": "diff probe", "private": "on"},
                expected=[303],
            ),
            request_step(
                f"{owner} 读取 {attacker} 的项目页面",
                "GET",
                attacker_proj,
                account=owner,
                expected=[401, 403],
                description="反向越权也必须被拒绝",
            ),
            request_step(
                f"{owner} 读取 {attacker} 的项目 API",
                "GET",
                attacker_api,
                account=owner,
                expected=[401, 403],
                description="反向越权也必须被拒绝",
            ),
        ]
        draft = _case(
            f"[攻击] 普通账号 {owner}/{attacker} 交叉访问私有项目",
            steps=steps,
            key="idor-differential:projects",
            policy_ref="SEC-IDOR-DIFF",
            description="两个普通账号之间可以横向读取对方的私有项目。",
            tags=["idor", "differential", "authz"],
            evidence=[_policy("SEC-IDOR-DIFF", "对象访问必须校验归属")],
        )
        draft.spec["cleanup"] = [
            request_step(f"删除 {owner} 的项目", "POST", owner_proj + "/delete", account=owner),
            request_step(f"删除 {attacker} 的项目", "POST", attacker_proj + "/delete", account=attacker),
        ]
        return [draft]


@register
class DeleteAuthz(AttackPattern):
    id = "delete-authz"
    title = "低权限账号可删除他人项目"
    category = "authz"
    severity = "critical"
    description = "破坏性操作必须单独校验管理员权限；读权限不能顺带获得删除权。"
    reference = "CWE-862"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        accounts = [
            name
            for name in ("alice", "viewer", "user", "developer")
            if (ctx.accounts.get(name) or {}).get("password")
        ]
        if len(accounts) < 2:
            return False, "需要两个普通账号"
        if ctx.find("POST", "/new", exact=True) is None or ctx.find_contains("GET", "/p/") is None:
            return False, "没有项目创建 / 页面路由"
        return True, ""

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        names = [
            name
            for name in ("alice", "viewer", "user", "developer")
            if (ctx.accounts.get(name) or {}).get("password")
        ][:2]
        owner, attacker = names
        create = ctx.find("POST", "/new", exact=True)
        page = ctx.find_contains("GET", "/p/")
        assert create is not None and page is not None
        name = "del{{run_id}}a"
        page_path = page.path.replace("{ns}", owner).replace("{name}", name)
        api_path = f"/api/v1/projects/{owner}/{name}"

        def step(method: str, path: str, account: str, *, expected: list[int], desc: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
            item: dict[str, Any] = {
                "action": "request",
                "name": desc,
                "method": method,
                "path": path,
                "fresh_client": True,
                "auth": {"mode": "basic", "account": account},
                "assert": [
                    {
                        "kind": "status_code",
                        "operator": "in",
                        "expected": expected,
                        "status": "confirmed",
                        "description": desc,
                        "evidence": _policy("SEC-DELETE-AUTHZ", "删除他人项目只允许管理员"),
                    }
                ],
            }
            if body is not None:
                item["headers"] = _form_headers(ctx)
                item["body"] = body
            return item

        steps = [
            step(
                "POST",
                create.path,
                owner,
                expected=[303],
                desc=f"{owner} 创建一次性私有项目",
                body={"namespace": owner, "name": name, "description": "delete probe", "private": "on"},
            ),
            {
                "action": "request",
                "name": f"{owner} 把 {attacker} 加为 guest（有读权限、无管理权限）",
                "method": "POST",
                "path": page_path + "/members",
                "fresh_client": True,
                "headers": _form_headers(ctx),
                "auth": {"mode": "basic", "account": owner},
                "body": {"username": attacker, "role": "guest"},
                "assert": [
                    {
                        "kind": "status_code",
                        "operator": "in",
                        "expected": [303],
                        "status": "confirmed",
                        "description": "guest 成员添加成功",
                        "evidence": _policy("SEC-DELETE-AUTHZ", "guest 只能读，不能删除"),
                    }
                ],
            },
            step(
                "DELETE",
                api_path,
                attacker,
                expected=[401, 403],
                desc=f"guest {attacker} 删除 {owner} 的项目必须被拒绝",
            ),
            step(
                "GET",
                api_path,
                owner,
                expected=[200],
                desc="越权删除失败后项目仍然存在",
            ),
        ]
        draft = _case(
            f"[攻击] {attacker} 不得删除 {owner} 的私有项目",
            steps=steps,
            key="delete-authz:project",
            policy_ref="SEC-DELETE-AUTHZ",
            description="普通账号可以删除他人的项目。",
            tags=["authz", "destructive"],
            evidence=[_policy("SEC-DELETE-AUTHZ", "删除他人项目只允许管理员")],
        )
        draft.spec["cleanup"] = [step("POST", page_path + "/delete", owner, expected=[303], desc="清理一次性项目")]
        return [draft]


@register
class TokenRevocation(AttackPattern):
    id = "token-revocation"
    title = "令牌吊销后仍可访问接口"
    category = "session"
    severity = "high"
    description = "吊销必须立即生效；否则泄露的令牌在整个生命周期内都能用。"
    reference = "CWE-613"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        if ctx.admin_create_user_endpoint is None or not ctx.has_admin:
            return False, "需要管理员创建一次性用户"
        if ctx.login_endpoint is None:
            return False, "没有登录接口"
        return True, ""

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        create = ctx.admin_create_user_endpoint
        assert create is not None
        user = "tok{{run_id}}"
        password = "TokPass1!"
        steps = [
            _admin_step(
                ctx,
                action="request",
                name="创建一次性用户",
                method="POST",
                path=create.path,
                headers=_form_headers(ctx),
                body={"username": user, "password": password, "displayName": "token probe"},
                assertions=[
                    {
                        "kind": "status_code",
                        "operator": "eq",
                        "expected": 303,
                        "status": "confirmed",
                        "description": "用户创建成功",
                        "evidence": _code(create.path, "创建用户 303"),
                    }
                ],
            ),
            {
                "action": "request",
                "name": "用户创建访问令牌",
                "method": "POST",
                "path": "/api/v1/tokens",
                "fresh_client": True,
                "auth": {"mode": "basic", "username": user, "password": password},
                "headers": {"Content-Type": "application/json"},
                "body": {"name": "revoke-{{run_id}}"},
                "save": {"revoke_token": "$.token"},
                "assert": [
                    {
                        "kind": "status_code",
                        "operator": "eq",
                        "expected": 201,
                        "status": "confirmed",
                        "description": "令牌创建成功",
                        "evidence": _code("internal/server/api.go", "创建令牌 201"),
                    }
                ],
            },
            {
                "action": "request",
                "name": "列出令牌并记录 id",
                "method": "GET",
                "path": "/api/v1/tokens",
                "fresh_client": True,
                "auth": {"mode": "basic", "username": user, "password": password},
                "save": {"revoke_id": "$[0].id"},
                "assert": [
                    {
                        "kind": "status_code",
                        "operator": "eq",
                        "expected": 200,
                        "status": "confirmed",
                        "description": "令牌列表可读",
                        "evidence": _code("internal/server/api.go", "列表 200"),
                    }
                ],
            },
            {
                "action": "request",
                "name": "吊销该令牌",
                "method": "DELETE",
                "path": "/api/v1/tokens/{{revoke_id}}",
                "fresh_client": True,
                "auth": {"mode": "basic", "username": user, "password": password},
                "assert": [
                    {
                        "kind": "status_code",
                        "operator": "eq",
                        "expected": 204,
                        "status": "confirmed",
                        "description": "吊销成功",
                        "evidence": _code("internal/server/api.go", "删除令牌 204"),
                    }
                ],
            },
            {
                "action": "request",
                "name": "用已吊销的令牌访问 /me",
                "method": "GET",
                "path": "/api/v1/me",
                "fresh_client": True,
                "headers": {"PRIVATE-TOKEN": "{{revoke_token}}"},
                "assert": [
                    {
                        "kind": "status_code",
                        "operator": "eq",
                        "expected": 401,
                        "status": "confirmed",
                        "description": "吊销后必须立即失效",
                        "evidence": _policy("SEC-TOKEN-REVOKE", "令牌吊销后不得继续认证"),
                    }
                ],
            },
        ]
        draft = _case(
            "[攻击] 令牌吊销后必须立即失效",
            steps=steps,
            key="token-revocation:api",
            policy_ref="SEC-TOKEN-REVOKE",
            description="吊销后的令牌仍可访问受保护接口。",
            tags=["token", "revocation", "state"],
            evidence=[_policy("SEC-TOKEN-REVOKE", "令牌吊销后不得继续认证")],
        )
        delete = _user_delete_endpoint(ctx)
        if delete is not None:
            draft.spec["cleanup"] = [
                _admin_step(
                    ctx,
                    action="request",
                    name="删除一次性用户",
                    method="POST",
                    path=delete.path.replace("{name}", user),
                )
            ]
        return [draft]


@register
class ProtocolFuzz(AttackPattern):
    id = "fuzz-protocol"
    title = "畸形协议报文导致 5xx"
    category = "injection"
    severity = "low"
    description = "git pkt-line / npm dist-tag / 非法 URL 字节不得把服务打成 5xx。"
    reference = "CWE-20"

    def applicable(self, ctx: AttackContext) -> tuple[bool, str]:
        if ctx.find("POST", "/new", exact=True) is None or not ctx.account("admin").get("token"):
            return False, "需要创建项目入口与 admin 令牌"
        return True, ""

    def build(self, ctx: AttackContext) -> list[CaseDraft]:
        create = ctx.find("POST", "/new", exact=True)
        page = ctx.find_contains("GET", "/p/")
        assert create is not None and page is not None
        admin_name = str(ctx.account("admin").get("username") or "admin")
        name = "proto{{run_id}}"
        page_path = page.path.replace("{ns}", admin_name).replace("{name}", name)

        def step(method: str, path: str, *, body: Any = None, headers: dict[str, str] | None = None,
                 expected: list[int] | None = None, desc: str = "") -> dict[str, Any]:
            item: dict[str, Any] = {
                "action": "request", "name": desc, "method": method, "path": path,
                "fresh_client": True, "headers": headers or {}, "assert": [],
            }
            if body is not None:
                item["body"] = body
            if expected:
                item["assert"] = [{
                    "kind": "status_code", "operator": "not_in", "expected": [500, 502, 503, 504],
                    "status": "confirmed", "description": desc,
                    "evidence": _policy("SEC-PROTO-5XX", "畸形协议报文不得返回 5xx"),
                }]
            return item

        steps = [
            _admin_step(
                ctx, action="request", name="创建协议探测项目", method="POST", path=create.path,
                headers=_form_headers(ctx),
                body={"namespace": admin_name, "name": name, "description": "protocol probe"},
                assertions=[{"kind": "status_code", "operator": "in", "expected": [303], "status": "confirmed",
                             "description": "项目创建成功", "evidence": _code(create.path, "创建项目 303")}],
            ),
            step("POST", f"/{admin_name}/{name}.git/git-upload-pack",
                 body="0000garbage", headers={"Content-Type": "application/x-git-upload-pack-request"},
                 expected=[200], desc="畸形 git pkt-line 不得 5xx"),
            {
                "action": "request", "name": "npm dist-tag 畸形 JSON", "method": "PUT",
                "path": f"/repository/npm-hosted/{name}/dist-tags/latest", "fresh_client": True,
                "headers": {**ctx.admin_headers(), "Content-Type": "application/json"},
                "body": "{bad",
                "assert": [{"kind": "status_code", "operator": "in", "expected": [400, 422], "status": "confirmed",
                            "description": "畸形 JSON 返回 4xx",
                            "evidence": _policy("SEC-PROTO-5XX", "格式错误返回 4xx")}],
            },
            step("GET", "/repository/maven-hosted/%00", expected=[400, 404], desc="非法 URL 字节不得 5xx"),
        ]
        draft = _case(
            "[攻击] 畸形协议报文不得打崩服务",
            steps=steps,
            key="fuzz-protocol:http",
            policy_ref="SEC-PROTO-5XX",
            description="畸形 pkt-line / JSON / URL 字节触发了 5xx。",
            tags=["fuzz", "protocol"],
            evidence=[_policy("SEC-PROTO-5XX", "畸形协议报文不得返回 5xx")],
        )
        draft.spec["cleanup"] = [
            _admin_step(ctx, action="request", name="删除协议探测项目", method="POST", path=page_path + "/delete")
        ]
        return [draft]
