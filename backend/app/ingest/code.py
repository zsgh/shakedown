"""代码库解析：切块（按类/方法）+ 路由抽取 + 符号检索。

tree-sitter 可用时按语法节点切块；不可用时退化为按行窗口切块，
保证任何环境都能导入代码库。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app.logging import get_logger

logger = get_logger("shakedown.ingest.code")

CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".go", ".rb", ".php", ".rs", ".c", ".h",
    ".cc", ".cpp", ".cs", ".swift", ".scala", ".sql", ".sh", ".vue", ".svelte", ".dart",
}

DEFAULT_INCLUDE = sorted(CODE_EXTENSIONS)

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env", "node",
    "dist", "build", "target", "vendor", ".idea", ".vscode", ".mypy_cache", ".pytest_cache",
    ".gradle", ".terraform", "coverage", "out", ".next", ".nuxt", ".cache", "tmp",
}

# ── tree-sitter 加载（可选） ───────────────────────────────
_TS_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
}

_PARSERS: dict[str, Any] = {}


def _get_parser(language: str) -> Any | None:
    if language in _PARSERS:
        return _PARSERS[language]
    try:
        import tree_sitter  # type: ignore

        if language == "python":
            import tree_sitter_python as ts_python  # type: ignore

            lang = tree_sitter.Language(ts_python.language())
        elif language == "javascript":
            import tree_sitter_javascript as ts_js  # type: ignore

            lang = tree_sitter.Language(ts_js.language())
        elif language == "typescript":
            import tree_sitter_typescript as ts_ts  # type: ignore

            lang = tree_sitter.Language(ts_ts.language_typescript())
        elif language == "java":
            import tree_sitter_java as ts_java  # type: ignore

            lang = tree_sitter.Language(ts_java.language())
        else:
            _PARSERS[language] = None
            return None
        parser = tree_sitter.Parser(lang)
    except Exception as exc:  # noqa: BLE001
        logger.debug("tree-sitter %s 不可用：%s", language, exc)
        _PARSERS[language] = None
        return None
    _PARSERS[language] = parser
    return parser


@dataclass
class CodeChunk:
    content: str
    file_path: str
    start_line: int
    end_line: int
    symbol: str = ""
    kind: str = "block"
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class RouteCandidate:
    method: str
    path: str
    file_path: str
    line: int
    framework: str = ""
    snippet: str = ""


def iter_source_files(root: Path, include_globs: Iterable[str] | None = None) -> list[Path]:
    """列出代码文件（跳过常见构建/依赖目录）。"""
    root = Path(root).expanduser().resolve()
    if not root.exists():
        return []
    patterns = list(include_globs) if include_globs else ["*"]
    files: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        for path in root.rglob(pattern):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() not in CODE_EXTENSIONS:
                continue
            if path in seen:
                continue
            seen.add(path)
            files.append(path)
    return sorted(files)


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def chunk_code_file(path: Path, root: Path) -> list[CodeChunk]:
    """把一个文件切成若干块：优先按类/函数，否则按行窗口。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if not text.strip():
        return []
    rel = _relative(path, root)
    lines = text.splitlines()
    parser = _get_parser(_TS_LANGUAGES.get(path.suffix.lower(), ""))
    if parser is not None:
        chunks = _chunk_with_tree_sitter(text, rel, parser)
        if chunks:
            return chunks
    return _chunk_by_window(text, rel, lines)


def _chunk_with_tree_sitter(text: str, rel: str, parser: Any) -> list[CodeChunk]:

    try:
        tree = parser.parse(text.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return []
    source = text.encode("utf-8")
    chunks: list[CodeChunk] = []
    interesting = {"function_definition", "class_definition", "method_definition", "function_declaration", "method_declaration"}

    def walk(node: Any, prefix: str = "") -> None:
        for child in node.children:
            if child.type in interesting:
                name_node = child.child_by_field_name("name")
                name = name_node.text.decode("utf-8", "replace") if name_node else ""
                full = f"{prefix}{name}" if name else child.type
                start_line = child.start_point[0] + 1
                end_line = child.end_point[0] + 1
                body = source[child.start_byte : child.end_byte].decode("utf-8", "replace")
                chunks.append(
                    CodeChunk(
                        content=body,
                        file_path=rel,
                        start_line=start_line,
                        end_line=end_line,
                        symbol=full,
                        kind="definition",
                        meta={"node_type": child.type},
                    )
                )
                if child.type in {"class_definition", "class_declaration"}:
                    walk(child, f"{full}.")
            else:
                walk(child, prefix)

    walk(tree.root_node)
    if not chunks:
        return []
    chunks.sort(key=lambda c: (c.start_line, c.end_line))
    return chunks


def _chunk_by_window(text: str, rel: str, lines: list[str], window: int = 80) -> list[CodeChunk]:
    chunks: list[CodeChunk] = []
    for start in range(0, max(len(lines), 1), window):
        end = min(start + window, len(lines))
        content = "\n".join(lines[start:end])
        if not content.strip():
            continue
        chunks.append(
            CodeChunk(
                content=content,
                file_path=rel,
                start_line=start + 1,
                end_line=end,
                symbol="",
                kind="window",
            )
        )
    return chunks


# ── 路由抽取 ───────────────────────────────────────────────
# 框架无关的启发式：装饰器 / 路由注册 / 映射注解。
# 所有 pattern 统一用命名分组：method（可选）+ path。
_ROUTE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # FastAPI / Flask：@app.get("/x") / @router.post('/x')
    (
        re.compile(
            r"@(?:[\w.]+\.)?(?P<method>get|post|put|delete|patch|head|options)\s*\(\s*[\"'](?P<path>[^\"']*)[\"']",
            re.I,
        ),
        "decorator",
    ),
    # Spring：@GetMapping("/x") / @RequestMapping("/x")
    (
        re.compile(
            r"@(?P<method>Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?[\"'](?P<path>[^\"']+)[\"']",
            re.I,
        ),
        "spring",
    ),
    # Express：router.get('/x', ...)
    (
        re.compile(r"\b(?:app|router|r)\.(?P<method>get|post|put|delete|patch|all)\s*\(\s*[\"'](?P<path>[^\"']+)[\"']", re.I),
        "express",
    ),
    # Go 1.22 ServeMux：mux.Handle("GET /api/procs", ...) / Handle("/x", ...)
    (
        re.compile(
            r"\b[\w.]*\.?Handle\(\s*[\"'](?:(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+)?(?P<path>/[^\"']*)[\"']",
            re.I,
        ),
        "go_mux",
    ),
    # Go 1.22 ServeMux 函数式注册：mux.HandleFunc("POST /api/x", ...) / http.HandleFunc("/x", ...)
    (
        re.compile(
            r"\b[\w.]*\.?HandleFunc\(\s*[\"'](?:(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+)?(?P<path>/[^\"']*)[\"']",
            re.I,
        ),
        "go_mux",
    ),
    # Go：路由表 map 字面量 {"GET /api/x": handler, ...}
    (
        re.compile(
            r"[\"'](?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(?P<path>/[^\"']+)[\"']\s*:",
            re.I,
        ),
        "go_map",
    ),
    # Go 老写法：http.HandleFunc("/x", ...)
    (re.compile(r"\bhttp\.HandleFunc\s*\(\s*[\"'](?P<path>/[^\"']*)[\"']", re.I), "go"),
    # Sinatra / Ruby：get '/x' do
    (
        re.compile(r"^\s*(?P<method>get|post|put|delete|patch)\s+[\"'](?P<path>/[^'\"]*)[\"']", re.I | re.M),
        "sinatra",
    ),
    # Django urls.py：path("x", view)
    (re.compile(r"\bpath\s*\(\s*[\"'](?P<path>[^\"']+)[\"']", re.I), "django"),
    # Axum / actix：route("/x", ...)
    (re.compile(r"\b(?:route|web::resource)\s*\(\s*[\"'](?P<path>[^\"']+)[\"']", re.I), "rust"),
    # Laravel：Route::get('/x', ...)
    (
        re.compile(
            r"Route::(?P<method>get|post|put|delete|patch|match|any)\s*\(\s*[\"'](?P<path>[^\"']+)[\"']",
            re.I,
        ),
        "laravel",
    ),
    # 通用前端路由表：{ "path": "/x", ... }（只作为弱信号）
    (re.compile(r"[\"']path[\"']\s*:\s*[\"'](?P<path>/[^\"']*)[\"']", re.I), "frontend"),
]

_METHOD_ALIASES = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "delete": "DELETE",
    "patch": "PATCH",
    "head": "HEAD",
    "options": "OPTIONS",
    "all": "GET",
    "any": "GET",
    "match": "GET",
    "request": "GET",
}

# Go：helper 批量注册路由
_GO_HELPER_DEF = re.compile(r"(\w+)\s*:=\s*func\s*\([^)]*\bpattern\s+string\b[^)]*\)\s*\{", re.S)
_GO_HELPER_METHOD = re.compile(r"Handle\(\s*\"(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s*\"\s*\+\s*pattern", re.I)


def extract_go_helpers(content: str) -> dict[str, list[str]]:
    """找出 Go 里「用 helper 批量注册路由」的写法。

    典型例子（mymac 就这么写）::

        api := func(pattern string, h http.HandlerFunc) {
            s.mux.Handle("GET "+pattern, ...)
            s.mux.Handle("POST "+pattern, ...)
            ...
        }
        api("/api/health", s.handleHealth)

    返回 ``{helper 名: [方法, ...]}``。
    """
    helpers: dict[str, list[str]] = {}
    matches = list(_GO_HELPER_DEF.finditer(content))
    for index, match in enumerate(matches):
        name = match.group(1)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        body = content[match.end() : min(end, match.end() + 2000)]
        methods = [m.upper() for m in _GO_HELPER_METHOD.findall(body)]
        methods = list(dict.fromkeys(methods))
        if methods:
            helpers[name] = methods
    return helpers


def _extract_go_helper_routes(
    content: str,
    helpers: dict[str, list[str]],
    seen: set[tuple[str, str]],
    found: list[RouteCandidate],
    lines: list[str],
) -> None:
    """展开 helper 调用点（``api("/api/health", ...)`` → 各方法 × 该路径）。"""
    for name, methods in helpers.items():
        pattern = re.compile(rf"(?<![\w.]){re.escape(name)}\(\s*\"(?P<path>/[^\"]*)\"")
        for match in pattern.finditer(content):
            raw_path = match.group("path")
            if not raw_path or raw_path.startswith(("http://", "https://", "//")):
                continue
            line_no = content[: match.start()].count("\n") + 1
            snippet = lines[line_no - 1].strip()[:200] if 0 < line_no <= len(lines) else ""
            for method in methods:
                key = (method, raw_path)
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    RouteCandidate(
                        method=method,
                        path=raw_path,
                        file_path="",
                        line=line_no,
                        framework="go_helper",
                        snippet=snippet,
                    )
                )


def _python_multiline_string_lines(content: str) -> set[int]:
    """返回位于多行字符串内部的行号（用于跳过 docstring 里的示例路由）。"""
    import io
    import tokenize

    inside: set[int] = set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(content).readline):
            if token.type != tokenize.STRING:
                continue
            start_line, end_line = token.start[0], token.end[0]
            for line_no in range(start_line + 1, end_line + 1):
                inside.add(line_no)
    except (tokenize.TokenError, IndentationError):
        return set()
    return inside


def extract_routes(
    path: Path,
    content: str,
    root: Path,
    helpers: dict[str, list[str]] | None = None,
) -> list[RouteCandidate]:
    """从文件内容里抽取接口路由候选。"""
    rel = _relative(path, root)
    suffix = path.suffix.lower()
    lines = content.splitlines()
    string_lines = _python_multiline_string_lines(content) if suffix == ".py" else set()
    found: list[RouteCandidate] = []
    seen: set[tuple[str, str]] = set()
    js_suffixes = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"}
    go_suffixes = {".go"}

    for pattern, framework in _ROUTE_PATTERNS:
        if framework in {"express", "frontend"} and suffix not in js_suffixes:
            continue
        if framework in {"go", "go_mux", "go_map", "go_helper"} and suffix not in go_suffixes:
            continue
        if framework == "django" and suffix != ".py":
            continue
        for match in pattern.finditer(content):
            groups = match.groupdict()
            raw_path = groups.get("path") or ""
            verb = (groups.get("method") or "").strip()
            method = _METHOD_ALIASES.get(verb.lower(), verb.upper()) if verb else "GET"

            if (not raw_path and framework not in {"decorator", "spring"}) or raw_path.startswith(
                ("http://", "https://", "//")
            ):
                continue
            if raw_path and framework != "django" and not raw_path.startswith("/"):
                continue
            line_no = content[: match.start()].count("\n") + 1
            if line_no in string_lines:
                continue
            line_text = lines[line_no - 1].lstrip() if 0 < line_no <= len(lines) else ""
            if line_text.startswith(("#", "//", "/*", "*")):
                continue
            if framework in {"decorator", "spring"} and not line_text.startswith("@"):
                continue
            key = (method, raw_path)
            if key in seen:
                continue
            seen.add(key)
            snippet = lines[line_no - 1].strip()[:200] if 0 < line_no <= len(lines) else ""
            found.append(
                RouteCandidate(
                    method=method,
                    path=raw_path,
                    file_path=rel,
                    line=line_no,
                    framework=framework,
                    snippet=snippet,
                )
            )

    # Go helper 批量注册的路由
    if helpers:
        before = len(found)
        _extract_go_helper_routes(content, helpers, seen, found, lines)
        for candidate in found[before:]:
            candidate.file_path = rel
    if suffix in go_suffixes:
        _extract_go_api_dispatch(content, rel, found, seen, lines)
    return found


_API_SEG_RESOURCE = re.compile(r'seg\[0\]\s*==\s*"([A-Za-z0-9_-]+)"')


def _extract_go_api_dispatch(
    content: str,
    rel: str,
    found: list[RouteCandidate],
    seen: set[tuple[str, str]],
    lines: list[str],
) -> None:
    """Go 的 /api/v1 动态分发：case seg[0] == "users"。

    这类路由不是逐个 HandleFunc 注册的，静态正则只能看到 /api/v1 根路径。
    按资源名补一条 GET 契约，至少让覆盖率和越权矩阵知道这个资源存在。
    """
    if "/api/v1" not in content:
        return
    for match in _API_SEG_RESOURCE.finditer(content):
        resource = match.group(1)
        if resource in {"version"}:
            continue
        path = f"/api/v1/{resource}"
        key = ("GET", path)
        if key in seen:
            continue
        seen.add(key)
        line_no = content[: match.start()].count("\n") + 1
        snippet = lines[line_no - 1].strip()[:200] if 0 < line_no <= len(lines) else ""
        found.append(
            RouteCandidate(
                method="GET",
                path=path,
                file_path=rel,
                line=line_no,
                framework="go_api_dispatch",
                snippet=snippet,
            )
        )


def repo_version(root: Path) -> str:
    """记录 commit hash（没有 git 就退回目录内容 hash）。"""
    root = Path(root).expanduser()
    head = root / ".git" / "HEAD"
    try:
        if head.exists():
            content = head.read_text(encoding="utf-8", errors="replace").strip()
            if content.startswith("ref:"):
                ref = content.split(" ", 1)[1].strip()
                ref_file = root / ".git" / ref
                if ref_file.exists():
                    return ref_file.read_text(encoding="utf-8").strip()[:40]
                packed = root / ".git" / "packed-refs"
                if packed.exists():
                    for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                        if line.strip().endswith(ref):
                            return line.split()[0][:40]
            return content[:40]
    except OSError:
        pass
    hasher = hashlib.sha256()
    for path in iter_source_files(root)[:200]:
        hasher.update(path.name.encode())
        try:
            hasher.update(path.read_bytes()[:4096])
        except OSError:
            continue
    return hasher.hexdigest()[:16]
