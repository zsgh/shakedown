"""依赖与工具链漏洞扫描：清单解析 + OSV 查询。

不引入第三方漏洞库依赖：直接查 https://api.osv.dev（Google 维护，覆盖
Go / npm / PyPI / Maven）。网络不可用时降级为「未扫描」并在报告里说明，
不会把「查不到」当成「没有漏洞」。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx

from app.logging import get_logger
from app.security.registry import FindingDraft

logger = get_logger("shakedown.security.deps")

OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/{vuln_id}"
MAX_DEPS = 120
MAX_VULN_DETAILS = 30
CACHE_TTL = 6 * 3600

# Go 1.24 之前的版本已经停止安全维护；这里只做下限判断，避免写死会过期的列表。
GO_EOL_BELOW = (1, 24)

_ECOSYSTEM_BY_EXT = {
    "go.mod": "Go",
    "package.json": "npm",
    "package-lock.json": "npm",
    "requirements.txt": "PyPI",
    "pom.xml": "Maven",
}

_cache: dict[str, tuple[float, Any]] = {}


@dataclass(frozen=True)
class Dependency:
    ecosystem: str
    name: str
    version: str
    source: str
    line: int = 0


@dataclass
class DependencyReport:
    dependencies: list[Dependency]
    findings: list[FindingDraft]
    errors: list[str]
    manifests: list[str]
    toolchains: list[dict[str, Any]]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _go_mod(path: Path) -> tuple[list[Dependency], dict[str, Any] | None]:
    deps: list[Dependency] = []
    lines = _read(path).splitlines()
    toolchain: dict[str, Any] | None = None
    in_block = False
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if line.startswith("go ") and toolchain is None:
            toolchain = {"name": "go", "version": line.split()[1], "source": str(path), "line": number}
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        if not in_block and not line.startswith("require "):
            continue
        body = line[len("require ") :].strip() if line.startswith("require ") else line
        body = re.sub(r"//.*$", "", body).strip()
        parts = body.split()
        if len(parts) >= 2 and parts[1].startswith("v"):
            deps.append(
                Dependency(
                    ecosystem="Go",
                    name=parts[0],
                    version=parts[1].lstrip("v"),
                    source=str(path),
                    line=number,
                )
            )
    return deps, toolchain


def _package_json(path: Path, lock: Path | None) -> list[Dependency]:
    deps: list[Dependency] = []
    try:
        doc = json.loads(_read(path) or "{}")
    except json.JSONDecodeError:
        return deps
    declared: dict[str, str] = {}
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        for name, version in (doc.get(section) or {}).items():
            if isinstance(version, str):
                declared[name] = version
    exact: dict[str, str] = {}
    if lock is not None and lock.exists():
        try:
            locked = json.loads(_read(lock) or "{}")
        except json.JSONDecodeError:
            locked = {}
        packages = locked.get("packages") or {}
        for key, meta in packages.items():
            if not isinstance(meta, dict):
                continue
            name = str(meta.get("name") or key.split("node_modules/")[-1])
            if name and meta.get("version"):
                exact[name] = str(meta["version"])
    for name, version in declared.items():
        resolved = exact.get(name) or version.lstrip("^~=<> ")
        if re.fullmatch(r"\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?", resolved):
            deps.append(Dependency(ecosystem="npm", name=name, version=resolved, source=str(path)))
    return deps


def _requirements(path: Path) -> list[Dependency]:
    deps: list[Dependency] = []
    for number, raw in enumerate(_read(path).splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*==\s*([^\s;]+)", line)
        if match:
            deps.append(
                Dependency(
                    ecosystem="PyPI",
                    name=match.group(1),
                    version=match.group(2),
                    source=str(path),
                    line=number,
                )
            )
    return deps


def _pom_xml(path: Path) -> list[Dependency]:
    text = _read(path)
    deps: list[Dependency] = []
    for match in re.finditer(r"<dependency>(.*?)</dependency>", text, re.S):
        block = match.group(1)
        group = re.search(r"<groupId>\s*([^<]+)\s*</groupId>", block)
        artifact = re.search(r"<artifactId>\s*([^<]+)\s*</artifactId>", block)
        version = re.search(r"<version>\s*([^<]+)\s*</version>", block)
        if not (group and artifact and version):
            continue
        value = version.group(1).strip()
        if "${" in value:
            continue
        deps.append(
            Dependency(
                ecosystem="Maven",
                name=f"{group.group(1).strip()}:{artifact.group(1).strip()}",
                version=value,
                source=str(path),
                line=text[: match.start()].count("\n") + 1,
            )
        )
    return deps


def discover_dependencies(code_roots: Iterable[Path]) -> tuple[list[Dependency], list[str], list[dict[str, Any]]]:
    deps: list[Dependency] = []
    manifests: list[str] = []
    toolchains: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for root in code_roots:
        if not root.exists():
            continue
        candidates = [
            root / "go.mod",
            root / "package.json",
            root / "requirements.txt",
            root / "pom.xml",
        ]
        for path in candidates:
            if not path.is_file():
                continue
            manifests.append(str(path))
            found: list[Dependency] = []
            if path.name == "go.mod":
                found, toolchain = _go_mod(path)
                if toolchain:
                    toolchains.append(toolchain)
            elif path.name == "package.json":
                found = _package_json(path, root / "package-lock.json")
            elif path.name == "requirements.txt":
                found = _requirements(path)
            elif path.name == "pom.xml":
                found = _pom_xml(path)
            for dep in found:
                key = (dep.ecosystem, dep.name, dep.version)
                if key in seen:
                    continue
                seen.add(key)
                deps.append(dep)
        if len(deps) >= MAX_DEPS:
            break
    return deps[:MAX_DEPS], manifests, toolchains


def _cached(key: str, loader: Any) -> Any:
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    value = loader()
    _cache[key] = (now, value)
    return value


def _post_osv(payload: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(timeout=15.0, trust_env=True) as client:
        response = client.post(OSV_BATCH, json=payload)
        response.raise_for_status()
        return response.json()


def _vuln_detail(vuln_id: str) -> dict[str, Any]:
    def load() -> dict[str, Any]:
        try:
            with httpx.Client(timeout=10.0, trust_env=True) as client:
                response = client.get(OSV_VULN.format(vuln_id=vuln_id))
                response.raise_for_status()
                return response.json()
        except Exception:  # noqa: BLE001
            return {}

    return _cached("vuln:" + vuln_id, load)


def _severity(detail: dict[str, Any]) -> str:
    database = detail.get("database_specific") or {}
    raw = str(database.get("severity") or "").upper()
    if raw in {"CRITICAL", "HIGH", "MEDIUM", "LOW"}:
        return raw.lower()
    for item in detail.get("severity") or []:
        score = str(item.get("score") or "")
        match = re.search(r"(\d+\.\d+)", score) if score else None
        if match:
            value = float(match.group(1))
            if value >= 9:
                return "critical"
            if value >= 7:
                return "high"
            if value >= 4:
                return "medium"
            return "low"
    return "medium"


def _fixed_versions(detail: dict[str, Any]) -> list[str]:
    fixed: list[str] = []
    for affected in detail.get("affected") or []:
        for rng in affected.get("ranges") or []:
            for event in rng.get("events") or []:
                value = event.get("fixed")
                if value and value not in fixed:
                    fixed.append(str(value))
    return fixed[:3]


def query_vulnerabilities(deps: list[Dependency]) -> tuple[dict[int, list[dict[str, Any]]], list[str]]:
    if not deps:
        return {}, []
    errors: list[str] = []
    results: dict[int, list[dict[str, Any]]] = {}
    payload = {
        "queries": [
            {"package": {"name": dep.name, "ecosystem": dep.ecosystem}, "version": dep.version}
            for dep in deps
        ]
    }
    try:
        data = _post_osv(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("OSV 查询失败：%s", exc)
        return {}, [f"OSV 不可用：{type(exc).__name__}: {exc}"]
    raw_results = data.get("results") or []
    all_ids: list[str] = []
    for row in raw_results:
        for item in row.get("vulns") or []:
            vuln_id = str(item.get("id") or "")
            if vuln_id and vuln_id not in all_ids and len(all_ids) < MAX_VULN_DETAILS:
                all_ids.append(vuln_id)
    details: dict[str, dict[str, Any]] = {}
    if all_ids:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=8) as pool:
            details = dict(zip(all_ids, pool.map(_vuln_detail, all_ids)))
    for index, dep in enumerate(deps):
        row = raw_results[index] if index < len(raw_results) else {}
        vulns: list[dict[str, Any]] = []
        for item in row.get("vulns") or []:
            vuln_id = str(item.get("id") or "")
            if not vuln_id:
                continue
            detail = details.get(vuln_id, {})
            vulns.append(
                {
                    "id": vuln_id,
                    "summary": str(detail.get("summary") or item.get("id") or ""),
                    "severity": _severity(detail),
                    "fixed": _fixed_versions(detail),
                    "aliases": (detail.get("aliases") or [])[:4],
                }
            )
        if vulns:
            results[index] = vulns
    return results, errors


def _toolchain_findings(toolchains: list[dict[str, Any]]) -> list[FindingDraft]:
    findings: list[FindingDraft] = []
    for item in toolchains:
        version = str(item.get("version") or "")
        match = re.match(r"(\d+)\.(\d+)", version)
        if not match:
            continue
        major, minor = int(match.group(1)), int(match.group(2))
        if major == 1 and minor < GO_EOL_BELOW[1]:
            findings.append(
                FindingDraft(
                    key=f"go-eol:{version}",
                    title=f"[供应链] Go {version} 已停止安全维护",
                    description=(
                        f"go.mod 声明 Go {version}，低于当前仍在维护的版本线。"
                        "Go 团队只为最近两个 minor 版本修复标准库安全问题；"
                        "net/http、crypto/tls、archive/tar 等组件的后续修复不会进入该版本。"
                        "建议升级 go 指令与构建工具链后重新构建。"
                    ),
                    severity="medium",
                    risk_type="供应链依赖",
                    evidence=[
                        {
                            "type": "manifest",
                            "file": item.get("source"),
                            "line": item.get("line"),
                            "detail": f"go {version}",
                        }
                    ],
                )
            )
    return findings


def scan_dependencies(ctx: Any) -> DependencyReport:
    deps, manifests, toolchains = discover_dependencies(ctx.code_roots)
    # 可选：项目显式登记了构建用的 Go 版本（例如从二进制 strings 里读到），
    # 就把标准库本身也纳入 OSV 查询。go.mod 的 go 指令只是语言版本，不能冒充工具链。
    project = getattr(ctx, "project", None)
    settings = (getattr(project, "settings_json", None) or {}) if project is not None else {}
    security = settings.get("security") if isinstance(settings, dict) else {}
    go_version = str((security or {}).get("go_version") or "").strip()
    if go_version and not any(dep.ecosystem == "Go" and dep.name == "stdlib" for dep in deps):
        deps.append(
            Dependency(
                ecosystem="Go",
                name="stdlib",
                version=go_version,
                source="settings.security.go_version",
            )
        )
    ctx.scratch["dependencies"] = deps
    ctx.scratch["manifests"] = manifests
    vulnerabilities, errors = query_vulnerabilities(deps)
    findings: list[FindingDraft] = []
    for index, vulns in vulnerabilities.items():
        dep = deps[index]
        top = sorted(vulns, key=lambda item: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(item["severity"], 2))
        severity = top[0]["severity"] if top else "medium"
        lines = []
        for vuln in top[:5]:
            line = f"- {vuln['id']}（{vuln['severity']}）：{vuln['summary'][:160]}"
            if vuln["fixed"]:
                line += f"，修复版本 {', '.join(vuln['fixed'])}"
            lines.append(line)
        findings.append(
            FindingDraft(
                key=f"osv:{dep.ecosystem}:{dep.name}:{dep.version}",
                title=f"[供应链] {dep.name}@{dep.version} 命中 {len(vulns)} 条已知漏洞",
                description=(
                    f"{dep.ecosystem} 依赖 {dep.name} {dep.version} 在 OSV 漏洞库中命中 {len(vulns)} 条记录。\n"
                    + "\n".join(lines)
                )[:4000],
                severity=severity,
                risk_type="供应链依赖",
                evidence=[
                    {
                        "type": "dependency",
                        "file": dep.source,
                        "line": dep.line,
                        "detail": f"{dep.ecosystem}:{dep.name}@{dep.version}",
                        "vulns": [item["id"] for item in top[:10]],
                    }
                ],
            )
        )
    findings.extend(_toolchain_findings(toolchains))
    return DependencyReport(
        dependencies=deps,
        findings=findings,
        errors=errors,
        manifests=manifests,
        toolchains=toolchains,
    )


__all__ = [
    "Dependency",
    "DependencyReport",
    "discover_dependencies",
    "query_vulnerabilities",
    "scan_dependencies",
]
