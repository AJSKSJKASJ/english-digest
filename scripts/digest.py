#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日外刊精读：抓 RSS -> DeepSeek 挑 3 句 -> 生成网页 -> 微信推链接。

只用 Python 标准库，不需要 pip 安装任何东西。

用法：
    python scripts/digest.py                # 抓取 + 生成 docs/ 下的页面 + 存推送内容
    python scripts/digest.py --push-only    # 只发微信（读取上次生成的推送内容）
    python scripts/digest.py --dry-run      # 不联网、不用 API，用示例数据渲染页面，检查排版
    python scripts/digest.py --render-json x.json   # 用自己写的 JSON 渲染页面（不联网、不用 API）

环境变量：DEEPSEEK_API_KEY（必需）、PUSHPLUS_TOKEN（推送时必需）、PAGE_BASE_URL（可选）
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
import xml.etree.ElementTree as ET

CST = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (compatible; english-digest/2.0)"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
PAYLOAD = os.path.join(ROOT, "push-payload.json")

# 卫报 RSS 自带 600–1500 字符正文摘要，直接可用；
# BBC 的 RSS 每条只有一句话摘要，所以会按链接再抓一次文章页取正文。
DEFAULT_FEEDS = [
    "https://www.theguardian.com/world/rss",
    "https://www.theguardian.com/international/rss",
    "https://www.theguardian.com/business/rss",
    "https://www.theguardian.com/science/rss",
    "https://www.theguardian.com/technology/rss",
    "https://www.theguardian.com/culture/rss",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
    "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
]

SYSTEM_PROMPT = "你是「移动端外刊语境教练」，输出精简、可扫读、不废话。只返回 JSON。"

USER_TEMPLATE = """日期：{date}

读者画像：中国大一电子信息类学生，已背完六级词汇，正从背单词过渡到真实语境；只用手机看，屏幕小，抗拒长篇大论。痛点是看得懂单词、读不懂长难句，容易写中式英语。

下面是今天抓到的外刊文章：

{articles}

任务：精选 3 个长难句，每句配「前情提要」+ 极简微讲解。

选题要求（很重要）：
- 3 句必须覆盖两家媒体：至少 1 句来自 The Guardian，至少 1 句来自 BBC；
- 3 句的话题必须互不重复（政治、科技、科学、商业、文化、环境、体育……各占一个）；
- 3 句的语言风格要拉开差距：一句偏叙事、一句偏解释、一句偏观点或引用。

选句标准：
- 长度 45–60 词，单句读完要停顿两次以上；
- 难度对标 IELTS 8+ / TOEFL 110+，明显高于六级，读起来「每个词都认识、连起来要读三遍」；
- 句中必须同时出现至少三种复杂结构：让步或条件从句、倒装、多重定语从句、省略关系代词、插入语（括号或破折号）、分词短语、独立主格、名词化、take aim at X as Y 这类「动词 + 宾语 + as 补语」结构；
- 含学术词或熟词生义（如 the very games、settled upon、a licence for）；
- 必须是文章原文的连续一句，一字不改，括号、破折号、引号都保留，不要自己造句子；尽量避开把两句拼在一起的段落边界。

每个字段怎么写：
- tag：这一句的语法标签，2–3 个中文结构名，用「 · 」隔开，如「让步从句 · 破折号同位语 · 省略关系代词」。
- background：前情提要，2–3 句话、30–60 字，说清「谁在做什么、为什么此刻值得读」，像给朋友的一句解释；不要复述下面这句原句。
- text：英文原句，只标 1–2 个目标词/短语，用 ** 包住，其余原样。
- breakdown：一句话点明主语和核心动作，说出难点的结构名称（如「省略关系代词的定语从句」「破折号插入语」），并给一句读法建议。
- phrase：核心短语或熟词生义 + 中文释义。
- trap：最容易犯的中式英语错误或误解。
- example：12 词以内的极简英文例句。

硬性约束：
- 除 background 外不再解释新闻背景，不写全文翻译，只拆骨架；
- 中文总字数（含 background，不含英文原句和例句）不超过 450 字。

只返回下面这种 JSON，不要代码块标记，不要任何解释文字：
{{
  "sentences": [
    {{
      "source": "The Guardian · World",
      "tag": "让步从句 · 破折号同位语 · 省略关系代词",
      "background": "前情提要，两三句话",
      "text": "英文原句，目标词用 ** 包住",
      "breakdown": "一句话点明主语和核心动作，并指出难点",
      "phrase": "核心短语或熟词生义 + 中文释义",
      "trap": "最容易犯的中式英语错误或误解",
      "example": "12 词以内的极简英文例句"
    }}
  ]
}}"""


# ────────────────────────────── 基础工具 ──────────────────────────────

def env(name, default=None):
    return os.environ.get(name) or default


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def http_get(url, timeout=25, retries=2):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001
            if attempt == retries:
                raise
            print(f"[warn] GET {url} 失败（{exc}），重试…", file=sys.stderr)
            time.sleep(3)


def http_post_json(url, payload, headers=None, timeout=120):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "User-Agent": UA}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def parse_date(raw):
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _child_text(node, *names):
    """按标签的本地名取子节点文本（忽略命名空间）。"""
    for child in node:
        local = child.tag.split("}")[-1].lower()
        if local in names:
            return child.text or ""
    return ""


# ────────────────────────────── 抓取 ──────────────────────────────

def sanitize_xml(raw):
    """RSS 里常有 &middot; 这类 XML 未定义的实体，先转成字符再解析。"""
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
    keep = {"amp", "lt", "gt", "quot", "apos"}

    def repl(match):
        if match.group(1) in keep:
            return match.group(0)
        return unescape(match.group(0))

    return re.sub(r"&([a-zA-Z][a-zA-Z0-9]{1,10});", repl, text)


def parse_feed(raw, feed_url):
    """解析 RSS 2.0 / Atom，返回文章列表。"""
    root = ET.fromstring(sanitize_xml(raw))
    feed_title = ""
    for node in root.iter():
        if node.tag.split("}")[-1].lower() == "title" and node.text:
            feed_title = strip_html(node.text)
            break

    entries = []
    for node in list(root.iter()):
        if node.tag.split("}")[-1].lower() not in ("item", "entry"):
            continue
        title = strip_html(_child_text(node, "title"))
        link = _child_text(node, "link", "id").strip()
        if not link:
            for child in node:
                if child.tag.split("}")[-1].lower() == "link" and child.get("href"):
                    link = child.get("href", "")
                    break
        body = _child_text(node, "encoded", "content", "description", "summary")
        body = strip_html(body)
        published = parse_date(_child_text(node, "pubdate", "published", "updated", "date"))
        entries.append({
            "title": title or "Untitled",
            "link": link,
            "body": body,
            "published": published,
            "source": feed_title or feed_url,
        })
    return entries


def feed_family(url):
    u = (url or "").lower()
    if "bbc." in u or "bbci." in u:
        return "bbc"
    if "guardian" in u:
        return "guardian"
    if "economist" in u:
        return "economist"
    return "other"


JUNK_PARTS = (
    "enable javascript", "bbc homepage", "skip to", "more menu", "sign in",
    "can not be played", "watch live", "follow us", "this video",
)


def fetch_article_text(url, limit=3000):
    """BBC 的 RSS 只有一句话摘要，正文要再抓一次文章页取 <article> 里的段落。"""
    if not url:
        return ""
    try:
        html = http_get(url).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 抓正文失败 {url}: {exc}", file=sys.stderr)
        return ""

    match = re.search(r"<article\b.*?</article>", html, re.S | re.I)
    segment = match.group(0) if match else html
    paras = []
    for raw in re.findall(r"<p\b[^>]*>(.*?)</p>", segment, re.S | re.I):
        text = strip_html(raw)
        low = text.lower()
        if len(text) < 40 or any(j in low for j in JUNK_PARTS):
            continue
        paras.append(text)
    return " ".join(paras)[:limit]


def collect_articles(window_hours=30, max_articles=8, min_body=120, per_feed=3):
    """按媒体分桶收集，保证卫报和 BBC 都在里面。"""
    feeds = [u.strip() for u in env("FEEDS", ",".join(DEFAULT_FEEDS)).split(",") if u.strip()]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    buckets = {"guardian": [], "bbc": [], "economist": [], "other": []}
    seen_links, seen_titles = set(), set()

    for url in feeds:
        try:
            entries = parse_feed(http_get(url), url)
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] feed 解析失败 {url}: {exc}", file=sys.stderr)
            continue

        family = feed_family(url)
        taken = 0
        for item in entries:
            if taken >= per_feed:
                break
            link = item["link"]
            key = re.sub(r"[^a-z0-9]+", "", item["title"].lower())[:60]
            if link and link in seen_links:
                continue
            if key and key in seen_titles:
                continue
            if item["published"] and item["published"] < cutoff:
                continue
            # BBC 的摘要只有一句话，长度够也要去抓正文
            if family == "bbc" and len(item["body"]) < 600:
                item["body"] = fetch_article_text(link) or item["body"]
            if len(item["body"]) < min_body:
                continue
            if link:
                seen_links.add(link)
            if key:
                seen_titles.add(key)
            buckets[family].append(item)
            taken += 1

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    for items in buckets.values():
        items.sort(key=lambda a: a["published"] or epoch, reverse=True)

    quota = max(1, max_articles // 2)
    picked = buckets["guardian"][:quota] + buckets["bbc"][:quota]
    spare = (buckets["guardian"][quota:] + buckets["bbc"][quota:]
             + buckets["economist"] + buckets["other"])
    spare.sort(key=lambda a: a["published"] or epoch, reverse=True)
    picked.extend(spare[: max(0, max_articles - len(picked))])
    picked.sort(key=lambda a: a["published"] or epoch, reverse=True)
    return picked[:max_articles]


# ────────────────────────────── 模型 ──────────────────────────────

def build_prompt(articles, date_str):
    blocks = []
    for idx, item in enumerate(articles, 1):
        blocks.append(
            f"[{idx}] 标题：{item['title']}\n来源：{item['source']}\n"
            f"正文：{item['body'][:1500]}\n"
        )
    return USER_TEMPLATE.format(date=date_str, articles="\n".join(blocks))


def extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON")
    return json.loads(text[start:end + 1])


def call_llm(articles, date_str):
    api_key = env("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY")
    base_url = env("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    prompt = build_prompt(articles, date_str)

    payload = {
        "model": env("LLM_MODEL", "deepseek-chat"),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.5,
        "max_tokens": 1800,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    last = None
    for attempt in range(2):
        try:
            data = json.loads(http_post_json(f"{base_url}/chat/completions", payload, headers))
            content = data["choices"][0]["message"]["content"]
            parsed = extract_json(content)
            sentences = [s for s in parsed.get("sentences", []) if s.get("text")]
            if len(sentences) < 3:
                raise ValueError(f"只返回了 {len(sentences)} 句")
            return sentences[:3]
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"[warn] 模型第 {attempt + 1} 次失败：{exc}", file=sys.stderr)
            time.sleep(4)
    raise RuntimeError(f"模型调用失败：{last}")


# ────────────────────────────── 渲染 ──────────────────────────────

CSS = """
  :root{
    --paper:#f6f2ea;--paper-2:#fffdf8;--ink:#1d1a16;--ink-2:#6b6459;
    --line:#e2dacb;--accent:#a8442a;
    --mono: ui-monospace,"SFMono-Regular",Consolas,"Liberation Mono",monospace;
    --serif: Georgia,"Times New Roman","Songti SC","Noto Serif SC","Source Han Serif SC",serif;
    --sans: "Helvetica Neue","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  }
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--serif);
    line-height:1.78;font-size:16.5px;-webkit-font-smoothing:antialiased;
    background-image:radial-gradient(900px 400px at 10% -6%,#fffaf1 0,rgba(255,250,241,0) 62%),
      radial-gradient(700px 360px at 100% 8%,#f2e9da 0,rgba(242,233,218,0) 56%);}
  .wrap{max-width:480px;margin:0 auto;padding:40px 20px 64px}
  header.head{margin-bottom:26px}
  .eyebrow{font-family:var(--mono);font-size:10px;letter-spacing:.24em;text-transform:uppercase;
    color:var(--accent);margin-bottom:14px;display:flex;align-items:center;gap:10px}
  .eyebrow::after{content:"";flex:1;height:1px;background:var(--line)}
  h1{font-size:33px;line-height:1.2;font-weight:600;letter-spacing:-.015em;margin:0 0 10px}
  h1 em{font-style:italic;color:var(--accent)}
  .sub{font-size:14px;color:var(--ink-2);font-style:italic;margin:0 0 16px}
  .meta{display:flex;flex-wrap:wrap;gap:6px 16px;font-family:var(--mono);font-size:10px;
    letter-spacing:.12em;text-transform:uppercase;color:var(--ink-2);
    border-top:1px solid var(--line);padding-top:12px}
  .meta b{color:var(--ink);font-weight:600}
  .card{background:var(--paper-2);border:1px solid var(--line);padding:22px 20px 20px;
    margin:0 0 16px;border-radius:2px}
  .card-top{display:flex;align-items:baseline;justify-content:space-between;gap:12px;
    font-family:var(--mono);font-size:10px;letter-spacing:.14em;text-transform:uppercase;
    margin-bottom:14px}
  .card-top .tag{color:var(--accent);font-family:var(--sans);font-size:12px;font-weight:600;letter-spacing:.06em}
  .card-top .src{color:var(--ink-2);font-size:9.5px}
  .tags{margin:-6px 0 12px;font-family:var(--mono);font-size:9.5px;letter-spacing:.1em;text-transform:uppercase;
    color:var(--accent);opacity:.85}
  .bg{margin:0 0 13px;font-size:13.5px;line-height:1.7;color:var(--ink-2);font-family:var(--sans)}
  .bg .lb{font-size:11.5px;margin-right:7px}
  .en{font-size:18.5px;line-height:1.66;margin:0 0 6px;color:var(--ink);letter-spacing:-.003em;
    font-style:italic;text-wrap:pretty}
  .en strong{font-weight:700;color:#8f3620;border-bottom:2px solid var(--accent);padding-bottom:.5px}
  .note{margin:16px 0 0;padding-top:14px;border-top:1px dotted var(--line);
    font-size:14.5px;line-height:1.72;color:#2a251f}
  .lb{display:inline-block;white-space:nowrap;font-family:var(--sans);font-size:12.5px;
    font-weight:600;color:var(--accent);letter-spacing:.03em;margin-right:9px}
  .lb.only{display:block;margin:18px 0 7px}
  ul.pt{list-style:none;margin:0;padding:0 0 0 12px}
  ul.pt li{position:relative;font-size:14.5px;line-height:1.72;color:#2a251f;margin-bottom:6px}
  ul.pt li::before{content:"\\2022";position:absolute;left:-11px;color:var(--accent)}
  ul.pt b{font-family:var(--sans);font-size:11.5px;color:var(--accent);letter-spacing:.02em;margin-right:5px}
  em.en-i{font-style:italic}
  footer{margin-top:30px;padding-top:16px;border-top:1px solid var(--line);
    font-family:var(--mono);font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;
    color:var(--ink-2);display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}
  footer a{color:var(--accent);text-decoration:none}
  @media print{
    @page{size:A4;margin:16mm 15mm}
    body{background:#fff}
    .wrap{max-width:640px;padding:0}
    .card{background:#fff;break-inside:avoid}
    h1{font-size:30px}}
  @media (max-width:360px){h1{font-size:28px}.en{font-size:17px}}
"""


def esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt(text):
    """转义 HTML，再把 **词** 变成加粗强调。"""
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc(text))


def source_counts(sentences):
    counts = {}
    for item in sentences:
        src = (item.get("source") or "").lower()
        name = "BBC" if "bbc" in src else ("Guardian" if "guardian" in src else "其他")
        counts[name] = counts.get(name, 0) + 1
    return counts


def render_card(index, item):
    background = (item.get("background") or "").strip()
    bg_html = (f'<p class="bg"><span class="lb">前情</span>{fmt(background)}</p>'
               if background else "")
    tags_html = (f'<div class="tags">{esc(item.get("tag", ""))}</div>'
                 if (item.get("tag") or "").strip() else "")
    return f"""
  <article class="card">
    <div class="card-top">
      <span class="tag">📰 句子 {index}</span>
      <span class="src">{esc(item.get('source', ''))}</span>
    </div>
    {tags_html}
    {bg_html}
    <p class="en">{fmt(item.get('text', ''))}</p>
    <p class="note"><span class="lb">🧠 拆解</span>{fmt(item.get('breakdown', ''))}</p>
    <p class="lb only">💡 精讲</p>
    <ul class="pt">
      <li><b>短语</b>{fmt(item.get('phrase', ''))}</li>
      <li><b>防坑</b>{fmt(item.get('trap', ''))}</li>
      <li><b>造句</b>{fmt(item.get('example', ''))}</li>
    </ul>
  </article>"""


def render_page(date_str, sentences, articles, is_archive=False):
    cards = "\n".join(render_card(i, s) for i, s in enumerate(sentences, 1))
    links = " · ".join(
        f'<a href="{esc(a["link"])}">{esc(a["title"][:28])}</a>'
        for a in articles[:4] if a.get("link")
    )
    counts = source_counts(sentences)
    mixed = " · ".join(f"{k} ×{v}" for k, v in counts.items())
    back = '<a href="../index.html">← 最新一期</a>' if is_archive else ''
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>今日三句 · 外刊精读 · {date_str}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="head">
    <div class="eyebrow">Daily Digest · 外刊精读</div>
    <h1>今日<em>三句</em></h1>
    <p class="sub">不译全文，只拆骨架。</p>
    <div class="meta">
      <span>{date_str}</span>
      <span><b>{len(sentences)}</b> 句 · <b>8</b> 分钟</span>
      <span>{mixed}</span>
      <span>IELTS 8+ / TOEFL 110+</span>
    </div>
  </header>
{cards}
  <footer>
    <span>{back or 'Actions · DeepSeek 生成'}</span>
    <span>{links}</span>
  </footer>
</div>
</body>
</html>
"""


# ────────────────────────────── 推送 ──────────────────────────────

def page_url(date_str=None):
    base = env("PAGE_BASE_URL", "").rstrip("/")
    if not base:
        repo = env("GITHUB_REPOSITORY", "")  # owner/repo
        if repo:
            owner, name = repo.split("/", 1)
            base = f"https://{owner}.github.io/{name}"
    if not base:
        base = "http://localhost"
    if date_str:
        return f"{base}/archive/{date_str}.html"
    return f"{base}/"


def build_push_html(date_str, sentences):
    rows = []
    for i, s in enumerate(sentences, 1):
        text = esc(re.sub(r"\*\*(.+?)\*\*", r"\1", s.get("text", "")))
        src = esc(s.get("source", ""))
        tag = esc(s.get("tag", ""))
        label = f"{i}. {src}" + (f" ｜ {tag}" if tag else "")
        rows.append(
            f"<p style='margin:16px 0 3px;font-size:11px;color:#a8442a;letter-spacing:.06em'>"
            f"{label}</p>"
            f"<p style='margin:0;font-style:italic;font-size:15px;line-height:1.62'>{text}</p>"
        )
    counts = source_counts(sentences)
    mixed = " · ".join(f"{k} ×{v}" for k, v in counts.items())
    link = page_url()
    return (
        f"<div style='font-size:15px;line-height:1.7'>"
        f"<p style='color:#6b6459;margin:0 0 10px'>今天 {len(sentences)} 句（{mixed}），点开看拆解 👇</p>"
        + "".join(rows)
        + f"<p style='margin-top:18px'><a href='{link}'>【阅读全文】</a></p>"
        + f"</div>"
    )


def push_pushplus(title, html):
    token = env("PUSHPLUS_TOKEN")
    if not token:
        print("[info] 未配置 PUSHPLUS_TOKEN，跳过微信推送")
        return False
    try:
        raw = http_post_json(
            "https://www.pushplus.plus/send",
            {"token": token, "title": title, "content": html, "template": "html"},
            timeout=30,
        )
        data = json.loads(raw)
        ok = data.get("code") == 200
        print(f"[info] PushPlus {'成功' if ok else '失败：' + raw[:200]}")
        return ok
    except urllib.error.HTTPError as exc:
        print(f"[warn] PushPlus HTTP {exc.code}: {exc.read()[:200]}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] PushPlus 异常：{exc}", file=sys.stderr)
    return False


# ────────────────────────────── 主流程 ──────────────────────────────

SAMPLE = {
    "sentences": [
        {"source": "The Guardian · World",
         "tag": "动词 + 宾语 + 宾语补足 · 省略关系代词",
         "background": "英国政府正推动一项产业赔偿方案，但始终没有公开表态；批评者认为这是一种不加说明的默许。",
         "text": "The government's **tacit** endorsement of the plan has left critics wondering whether it ever intended to **hold the industry to account** for the damage it has caused.",
         "breakdown": "主语是 endorsement（政府的默许），核心动作 has left critics wondering；难点是 whether 从句挂在 wondering 后面，别误当成疑问句。",
         "phrase": "tacit endorsement 默许；hold sb to account 追究某人责任",
         "trap": "tacit 不是「沉默」，是「不说但默认」；hold sb accountable 后面不要再加 to。",
         "example": "His silence looked like tacit approval."},
        {"source": "The Guardian · Business",
         "tag": "独立主格 · 分词",
         "background": "粮价连续两年高于疫情前水平，2030 年消除饥饿的目标被重新审视，连起草文件的官员也开始松口。",
         "text": "With food prices still climbing, the **pledge** to end hunger by 2030 looks increasingly **untenable**, even to some officials who drafted it.",
         "breakdown": "With 引导的独立主格先铺背景，主句是 the pledge looks untenable；难点是独立主格里没有谓语，climbing 只是分词。",
         "phrase": "an untenable position 守不住的立场",
         "trap": "别写 With prices still climb——独立主格里必须用分词或形容词。",
         "example": "With costs rising, the plan looks untenable."},
        {"source": "BBC News · Technology",
         "tag": "破折号同位语 · 定语从句套时间状语从句",
         "background": "这家初创公司在发布会前夜临时换了主讲人，团队彻夜重做演示；第二天的谈判因此多出半小时缓冲。",
         "text": "She has become **adept at** **reading the room** — a skill that served her well during the negotiations, when tempers ran high.",
         "breakdown": "主句加破折号后的同位语 a skill，再由 that 引导定语从句；难点是定语从句里又套了一层 when 状语从句。",
         "phrase": "be adept at doing 擅长；read the room 察言观色",
         "trap": "adept 后面只能跟 at；read the room 直译成「读房间」就彻底跑偏。",
         "example": "He is adept at defusing tense meetings."},
    ]
}


def write_outputs(date_str, sentences, articles):
    os.makedirs(os.path.join(DOCS, "archive"), exist_ok=True)
    latest = render_page(date_str, sentences, articles)
    archive = render_page(date_str, sentences, articles, is_archive=True)
    with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(latest)
    with open(os.path.join(DOCS, "archive", f"{date_str}.html"), "w", encoding="utf-8") as fh:
        fh.write(archive)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--push-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--render-json", metavar="FILE", help="用本地 JSON 渲染页面，不联网、不调用模型")
    args = parser.parse_args()

    if args.push_only:
        if not os.path.exists(PAYLOAD):
            print("[warn] 没有找到推送内容，跳过")
            return 1
        with open(PAYLOAD, encoding="utf-8") as fh:
            payload = json.load(fh)
        return 0 if push_pushplus(payload["title"], payload["html"]) else 1

    date_str = datetime.now(CST).strftime("%Y-%m-%d")

    if args.render_json:
        with open(args.render_json, encoding="utf-8") as fh:
            data = json.load(fh)
        sentences = data.get("sentences") or []
        if not sentences:
            print("[error] JSON 里没有 sentences", file=sys.stderr)
            return 1
        if data.get("date"):
            date_str = data["date"]
        articles = data.get("articles") or [{"title": "本地样本", "link": page_url(), "source": "sample"}]
        write_outputs(date_str, sentences, articles)
        print(f"[info] 已渲染 docs/index.html 和 docs/archive/{date_str}.html（来源：{args.render_json}）")
        return 0

    if args.dry_run:
        articles = [{"title": "示例文章", "link": "https://example.com", "source": "示例"}]
        sentences = SAMPLE["sentences"]
        print("[info] dry-run：使用示例数据")
    else:
        window = int(env("WINDOW_HOURS", "30"))
        articles = collect_articles(window, int(env("MAX_ARTICLES", "8")))
        if not articles:
            print("[error] 没抓到文章", file=sys.stderr)
            push_pushplus("⚠️ 外刊精读失败", "<p>今天没抓到文章，检查 RSS 或网络。</p>")
            return 1
        print(f"[info] 抓到 {len(articles)} 篇，调用模型…")
        sentences = call_llm(articles, date_str)

    write_outputs(date_str, sentences, articles)

    title = f"☕ 外刊精读 · {datetime.now(CST).strftime('%m月%d日')}"
    payload = {"title": title, "html": build_push_html(date_str, sentences)}
    with open(PAYLOAD, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)

    print(f"[info] 已生成 docs/index.html 和 docs/archive/{date_str}.html")
    print(f"[info] 推送内容已存 push-payload.json（{len(payload['html'])} 字符）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
