#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自检：不发网络请求、不调用模型。

    python tests/smoke_test.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import digest  # noqa: E402

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>The Guardian &middot; World</title>
    <item>
      <title>Ministers face pressure over the plan</title>
      <link>https://example.com/a</link>
      <description><![CDATA[<p>The government's <b>tacit</b> endorsement of the plan has left critics wondering whether it ever intended to hold the industry to account for the damage it has caused.</p>]]></description>
      <pubDate>Fri, 21 Nov 2026 02:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Too short</title>
      <link>https://example.com/b</link>
      <description>short</description>
      <pubDate>Fri, 21 Nov 2026 03:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>"""

failures = []


def check(name, fn):
    try:
        fn()
        print(f"PASS  {name}")
    except AssertionError as exc:
        failures.append(name)
        print(f"FAIL  {name}: {exc}")


def t_strip_html():
    out = digest.strip_html("<p>Hello <b>world</b></p><script>x()</script>")
    assert out == "Hello world", out


def t_parse_date():
    assert digest.parse_date("Fri, 21 Nov 2026 02:00:00 GMT").year == 2026
    assert digest.parse_date("2026-11-21T02:00:00Z").hour == 2
    assert digest.parse_date("") is None
    assert digest.parse_date("not a date") is None


def t_parse_feed():
    entries = digest.parse_feed(RSS.encode("utf-8"), "https://example.com/rss")
    assert len(entries) == 2, len(entries)
    first = entries[0]
    assert first["title"] == "Ministers face pressure over the plan", first["title"]
    assert first["link"] == "https://example.com/a"
    assert "tacit" in first["body"] and "<" not in first["body"], first["body"]
    assert first["published"] is not None
    assert "Guardian" in first["source"], first["source"]


def t_fmt():
    assert digest.fmt("a **b** c") == "a <strong>b</strong> c"
    assert digest.fmt("<x> & **y**") == "&lt;x&gt; &amp; <strong>y</strong>"


def t_extract_json():
    raw = '```json\n{"sentences": [{"text": "hi"}]}\n```'
    assert digest.extract_json(raw)["sentences"][0]["text"] == "hi"
    raw2 = '好的，这是结果：{"sentences": []} 完毕'
    assert digest.extract_json(raw2) == {"sentences": []}


def t_render_page():
    html = digest.render_page("2026-11-21", digest.SAMPLE["sentences"],
                              [{"title": "T", "link": "https://example.com"}])
    assert html.count('<article class="card">') == 3
    assert "<strong>tacit</strong>" in html
    assert "2026-11-21" in html
    assert "<h1>今日<em>三句</em></h1>" in html
    assert "https://example.com" in html


def t_push_html():
    html = digest.build_push_html("2026-11-21", digest.SAMPLE["sentences"])
    assert "tacit" in html and "<strong>" not in html  # 推送里去掉加粗标记
    assert "【阅读全文】" in html


def t_write_outputs():
    import shutil
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tmp = os.path.join(base, "_smoke_out")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    old = digest.DOCS
    digest.DOCS = tmp
    try:
        digest.write_outputs("2026-11-21", digest.SAMPLE["sentences"],
                             [{"title": "T", "link": "https://example.com"}])
        assert os.path.exists(os.path.join(tmp, "index.html"))
        assert os.path.exists(os.path.join(tmp, "archive", "2026-11-21.html"))
    finally:
        digest.DOCS = old
        shutil.rmtree(tmp, ignore_errors=True)


check("strip_html", t_strip_html)
check("parse_date", t_parse_date)
check("parse_feed", t_parse_feed)
check("fmt 加粗与转义", t_fmt)
check("extract_json 去代码块", t_extract_json)
check("render_page", t_render_page)
check("push_html", t_push_html)
check("write_outputs", t_write_outputs)

print()
if failures:
    print(f"{len(failures)} 项失败：{', '.join(failures)}")
    sys.exit(1)
print("全部通过")
