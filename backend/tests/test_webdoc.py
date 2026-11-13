"""网页文档抓取 / HTML→Markdown / Agent 抽接口测试。"""
from __future__ import annotations

import pytest

from app.agent.docextract import rule_based_extraction
from app.ingest.webdoc import WebDocError, html_to_markdown

HTML = """
<html><head><title>用户中心 API</title><style>.x{}</style><script>var a=1;</script></head>
<body>
<nav>首页 | 文档</nav>
<h1>用户中心 API</h1>
<p>创建用户：<code>POST /api/users</code>。</p>
<h2>端点</h2>
<table>
<tr><th>方法</th><th>路径</th><th>说明</th></tr>
<tr><td>GET</td><td>/api/users</td><td>用户列表</td></tr>
</table>
<pre><code>curl -X GET http://127.0.0.1:9100/api/users</code></pre>
<ul><li>参数 <code>page</code></li><li>返回 <code>items</code></li></ul>
<p>参考 <a href="https://example.com/spec">接口规范</a></p>
</body></html>
"""


def test_html_to_markdown_keeps_content():
    md, title = html_to_markdown(HTML)
    assert title == "用户中心 API"
    assert "# 用户中心 API" in md
    assert "POST /api/users" in md
    assert "| GET | /api/users | 用户列表 |" in md
    assert "```" in md  # 代码块
    assert "- 参数 `page`" in md
    assert "[参考 接口规范](https://example.com/spec)" in md
    # 噪音要去掉
    assert "var a=1" not in md
    assert ".x{}" not in md
    assert "首页" not in md


def test_html_to_markdown_terminates_quickly():
    """曾经的死循环回归测试。"""
    for _ in range(3):
        md, _title = html_to_markdown("<ul><li>a</li><li>b</li></ul><table><tr><td>x</td></tr></table>")
    assert md


def test_rule_based_extraction_from_prose():
    chunks = [
        {
            "id": 1,
            "content": (
                "创建结算单用 `POST /api/settle/orders` 接口。\n"
                "- 查询：`GET /api/settle/orders/{id}`\n"
                "- 回调：`POST /api/settle/callback`\n"
                "路径里的 ~/.bash_profile 与 /etc/hosts 不是接口\n"
            ),
            "locator": {"file_path": "wiki/结算.md"},
        }
    ]
    results = rule_based_extraction(chunks)
    keys = {(item["method"], item["path"]) for item in results}
    assert ("POST", "/api/settle/orders") in keys
    assert ("GET", "/api/settle/orders/{id}") in keys
    assert ("POST", "/api/settle/callback") in keys
    assert not any(path.startswith("/etc") for _method, path in keys)


def test_rule_based_extraction_keeps_evidence():
    chunks = [
        {
            "id": 1,
            "content": "结算回调：POST /api/settle/callback（第三方调用）",
            "locator": {"file_path": "wiki/结算.md"},
        }
    ]
    results = rule_based_extraction(chunks)
    assert results
    assert results[0]["evidence"]["quote"]
    assert results[0]["evidence"]["ref"] == "wiki/结算.md"


def test_agent_extraction_falls_back_to_rules():
    """离线 provider 也要能抽出接口。"""
    from app.agent.providers.mock import MockProvider

    provider = MockProvider()
    result = provider.structured(
        "sys", "user", {"type": "object"},
        context={
            "task": "doc_endpoint_extraction",
            "chunks": [{"ref": "a.md", "content": "GET /api/a 列表\nPOST /api/b 创建"}],
        },
    )
    keys = {(item["method"], item["path"]) for item in result["endpoints"]}
    assert ("GET", "/api/a") in keys
    assert ("POST", "/api/b") in keys


def test_void_elements_do_not_swallow_content():
    """回归：<meta>/<link> 没有结束标签，早期实现会把整页当嵌套跳过。"""
    html = """<html><head><meta charset="utf-8"><link rel="x" href="y"></head>
<body><h1>标题</h1><p>正文内容</p></body></html>"""
    md, title = html_to_markdown(html)
    assert "正文内容" in md
    assert "# 标题" in md


def test_webdoc_error_on_bad_url():
    from app.ingest.webdoc import fetch_web_doc

    with pytest.raises(WebDocError):
        fetch_web_doc("http://127.0.0.1:1/nothing", timeout=1.0, retries=1)
