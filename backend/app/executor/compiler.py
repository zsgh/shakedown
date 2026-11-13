"""用例编译：统一 JSON → 可执行 pytest 代码（数据库为准，代码由数据库生成）。"""
from __future__ import annotations

import copy
import json
import pprint
import shutil
from pathlib import Path
from typing import Any

from app.spec_repair import repair_spec

TEMPLATE_DIR = Path(__file__).parent / "templates"
RESULT_MARKER = "<<<SHAKEDOWN_RESULT>>>"


def _runtime_source() -> str:
    return (TEMPLATE_DIR / "runtime.py").read_text(encoding="utf-8")


def compile_cases(cases: list[dict[str, Any]]) -> dict[str, str]:
    """返回 ``{文件名: 内容}``：运行时 + 测试文件。"""
    payload = []
    for case in cases:
        spec = copy.deepcopy(case)
        spec.setdefault("id", "")
        # 最后一道保险：即使库里的断言类型是历史脏数据，执行器也按步骤动作判定。
        repair_spec(spec)
        payload.append(spec)

    lines = [
        "# 由 Shakedown 自动生成，请勿手工修改（真身在数据库）。",
        "import shakedown_runtime",
        "",
        "CASES = " + pprint.pformat(payload, width=110, sort_dicts=False),
        "",
    ]
    for index, case in enumerate(payload):
        case_id = str(case.get("id", "0")).replace("-", "_")
        lines.append("")
        lines.append(f"def test_shakedown_case_{case_id}():")
        lines.append(f"    shakedown_runtime.run_case(CASES[{index}])")
    lines.append("")
    return {
        "shakedown_runtime.py": _runtime_source(),
        "test_shakedown_cases.py": "\n".join(lines),
    }


def build_command(files: dict[str, str], env: dict[str, str], *, workdir: str = "/workspace") -> str:
    """生成在沙箱里执行的 shell 脚本（写文件 → 跑 pytest → 汇总结果）。"""
    parts = ["set -eu", f"mkdir -p {workdir}/results {workdir}/artifacts", f"cd {workdir}"]
    for name, content in files.items():
        parts.append(f"cat > {workdir}/{name} <<'SHAKEDOWN_EOF'\n{content}\nSHAKEDOWN_EOF")
    for key, value in env.items():
        parts.append(f"export {key}={json.dumps(value)}")
    if str(env.get("SHAKEDOWN_BOOTSTRAP_DEPS") or "") == "1":
        # berth 的通用镜像不带 pytest/httpx：每次执行前装到 /tmp，避免依赖自制 runner 镜像。
        index = str(env.get("SHAKEDOWN_PIP_INDEX_URL") or "https://pypi.tuna.tsinghua.edu.cn/simple")
        parts.append(
            "python3 -m pip install --quiet --no-cache-dir --disable-pip-version-check "
            f"--index-url {index} --target /tmp/shakedown-deps pytest httpx"
        )
        parts.append('export PYTHONPATH=/tmp/shakedown-deps:${PYTHONPATH:-}')
    parts.append(
        "python3 -m pytest -q --no-header --tb=no -p no:cacheprovider test_shakedown_cases.py || true"
    )
    parts.append(
        "python3 - <<'SHAKEDOWN_COLLECT'\n"
        "import glob, json, os\n"
        "results = []\n"
        "for path in sorted(glob.glob('results/*.json')):\n"
        "    try:\n"
        "        with open(path, encoding='utf-8') as handle:\n"
        "            results.append(json.load(handle))\n"
        "    except Exception as exc:\n"
        "        results.append({'status': 'error', 'error': str(exc), 'case_id': os.path.basename(path)})\n"
        f"print('{RESULT_MARKER}' + json.dumps(results, ensure_ascii=False, default=str))\n"
        "SHAKEDOWN_COLLECT"
    )
    return "\n".join(parts)


def collect_results(stdout: str) -> list[dict[str, Any]]:
    """从沙箱 stdout 里解析结果。"""
    if RESULT_MARKER not in stdout:
        return []
    payload = stdout.split(RESULT_MARKER, 1)[1].strip()
    # 后面可能还有别的输出，截到第一个换行前的 JSON 结束
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        try:
            data, _end = decoder.raw_decode(payload)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []


def materialize(files: dict[str, str], target_dir: Path) -> Path:
    """把编译产物写到本地目录（本地沙箱模式用）。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (target_dir / name).write_text(content, encoding="utf-8")
    return target_dir


def copy_template(name: str, target: Path) -> None:
    shutil.copyfile(TEMPLATE_DIR / name, target)
