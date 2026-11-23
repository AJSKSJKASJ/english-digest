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

# ── 三个指定源（每天每源恰好 1 句，取不到就记 missing_source，不拿别的源顶替）──
# 实测（2026-11-23）：这三家的 RSS 行为完全不同，所以正文要分三种方式拿。
#   page    Guardian Long Read：RSS 的 description 只有开头 1–2 段（665–2150 字符），
#           文章页能拿到完整正文（实测 34,752 字符 / 70 段）。所以优先抓页面，失败再退回 RSS。
#   wayback Economist Briefing：economist.com 对脚本一律 403（直连、本地代理、Googlebot UA、
#           r.jina.ai 全部试过）；RSS 里只有 16 字符的导语，没有正文。
#           唯一能拿到全文的路子是 web.archive.org 的存档快照。注意：快照通常要发布后 1–4 天才出现，
#           当天的文章基本没有 → 这种情况必须明确记 missing，而不是静默跳过。
#   feed    The Atlantic Ideas：Atom 的 <content> 就是全文（3,630–20,002 字符），不用再抓页面。
SOURCES = [
    {
        "key": "guardian",
        "name": "The Guardian · Long Read",
        "url": "https://www.theguardian.com/news/series/the-long-read/rss",
        "extract": "page",
        "style": "structural",
    },
    {
        # 原来的 Economist Briefing 已换掉：它每周四才更一次，文章页 403，只能靠
        # Wayback 快照，多数日子拿到的是 700–800 字符的导语，选不出像样的句子。
        # 换成 New Yorker 的原因：日更、篇幅长、行文本身就是英文学术/散文写作的范本，
        # 而且它的 JSON-LD 里有完整 articleBody（见 fetch_jsonld_body），不花钱绕墙。
        "key": "newyorker",
        "name": "The New Yorker",
        "url": "https://www.newyorker.com/feed/everything",
        "extract": "jsonld",
        # style=literary：这个源不考语法复杂度，考「写得好不好」，标准见 LITERARY_CRITERIA
        "style": "literary",
    },
    {
        "key": "atlantic",
        "name": "The Atlantic · Ideas",
        "url": "https://www.theatlantic.com/feed/channel/ideas/",
        "extract": "feed",
        "style": "structural",
    },
]

# 仍然是三个地址，只是改成从 SOURCES 里推出来，方便 FEEDS 环境变量按顺序覆盖。
DEFAULT_FEEDS = [s["url"] for s in SOURCES]

# 两套选句标准分开写：结构句考「能不能拆出骨架」，文学句考「有没有画面和节奏」。
# 混成一套的话，模型会拿语法清单去评散文，最后选出一堆正确但难看的句子。
STRUCT_CRITERIA = """选用标准（结构｜语法难度优先）：逐条打勾，命中 ≥3 条、且不命中排除条件，才可以选。
A. 让步 / 条件 / 原因状语从句，且从句与主句之间有逻辑张力（让步 + 转折、条件 + 结果倒置）
B. 非限制性定语从句或插入语（破折号 / 括号），且插入成分打断了主谓宾
C. 名词性从句（what / whether / that）作主语、宾语或同位语
D. 分词短语（having done / being / 过去分词）作状语或后置定语
E. 修饰关系容易误读（例如定语从句修饰的是隔了介词短语的先行词）
排除：纯新闻叙事句；语义直白的引语；简单主谓宾 + 并列形容词；句号或分号拼出的「伪长句」。"""

LITERARY_CRITERIA = """选用标准（文采｜写得好不好，语法难不难不重要）：命中 ≥2 条才可选，一条都不中说明只是信息句，换一句。
L1. 有具体画面：能看见场景、动作或物件，而不是抽象判断（“the light went out of the afternoon”，不是“it was a difficult time”）
L2. 有比喻 / 通感 / 拟人：as if、as though、like，或把一个抽象名词写成能摸到的东西
L3. 节奏讲究：长句后接短句、破折号停顿、三项并列、刻意的重复
L4. 格言感或反讽：这一句本身可以独立成立，带着不动声色的判断
L5. 用词偏文学：hush、gauzy、ache、unspool、shimmer 这一类，而不是 news / report / official 这一类新闻词
排除：采访引语（“…,” she said）；纯信息陈述（时间、地点、数字、职务）；纯论证没有画面；带网址 / 订阅 / 版权 / 图注的句子。"""

# 判断「这句是不是文学句」时用的机械特征，只用来做粗筛和返回后校验，不追求准确。
LITERARY_MARKS = (
    " as if ", " as though ", " like a ", " like an ", " seemed to ", " felt like ",
    "—", " – ", "; ", "hush", "gauzy", "shimmer", "ache", "unspool", "grief",
    "tender", "silence", "shadow", "dust", "light", "smell", "taste", "wound",
)

SYSTEM_PROMPT = "你是「移动端外刊语境教练」，输出精简、可扫读、不废话。只返回 JSON。"

USER_TEMPLATE = """日期：{date}

读者画像：中国大一电子信息类学生，已背完六级词汇，正从背单词过渡到真实语境；只用手机看，屏幕小，抗拒长篇大论。痛点有两个：看得懂单词、读不懂长难句；写得出语法正确的句子，但句子干、没有文采。

候选句已经由程序做过机械粗筛，按来源分组，每组标题下面写着这一组的选用标准。粗筛只保证长度和基本干净，选谁由你判断：

{pool}

任务：从每个来源的候选句里各选恰好 1 句（今天共 {expected} 句），每句配「前情提要」+ 极简微讲解。

来源配比（硬性）：
- 恰好 1 句来自 The Guardian Long Read，1 句来自 The New Yorker，1 句来自 The Atlantic Ideas；
- 只能从各组自己的候选句里选，不得跨来源借句，不得两句同源；
- 某个来源今天没有候选句（该组根本没出现）时，就少选一句，并在 JSON 顶层的 missing_source 里写上该来源，绝不拿别的来源补位。

通用排除条件（所有来源都适用，命中任意一条即淘汰）：
- 引语本身语义直白、不需要推理就能读懂；
- 纯新闻叙事：主语 + 被动语态 + 时间地点 + 伤亡 / 金额数字；
- 两个独立句子被句号或分号拼成的「伪长句」；
- 含网址、订阅引导、版权声明、图注（Photograph / Illustration by / Getty / Sign up）。

The New Yorker 这一组额外要求（文采优先，语法难度让位）：
- 优先有画面、有比喻、节奏讲究的散文句；宁可句子结构简单，也不要选只是信息量大的一句；
- 不要选「谁说了什么」的采访引语，也不要选纯粹的论证句（a 应该 b、政策 c 导致 d）；
- 18–45 词最合适；超过 55 词不收，讲不清。

每个字段怎么写：
- source：媒体名，如「The New Yorker」。
- source_feed：固定写 guardian / newyorker / atlantic 三者之一（机器要用它核对配比）。
- matched_criteria：结构组写命中的 A–E 编号；文采组写命中的 L1–L5 编号。必须与你逐条打勾的结果一致，不能虚报。
- tag：结构组写结构（「让步从句 · 破折号插入语」）；文采组写手法（「文采 · 通感 · 长句节奏」）。
- background：前情提要，2–3 句、30–60 字，像给朋友的一句解释：这段在文章里是什么位置、在讲谁、为什么这句值得读。不要复述原句。
- text：英文原句，一字不改，只把 1–2 个目标词/短语用 ** 包住。
- breakdown：一句话说清这句在干什么——结构组说骨架和难点；文采组说它写的画面以及靠什么手法立起来。
- phrase：能直接搬进写作的表达——结构组优先短语和熟词生义；文采组优先文学化表达（要能用在作文开头或结尾）。
- trap：最容易犯的中文式英文——结构组讲语法误判；文采组讲形容词堆砌、比喻直译、抽象词空心。
- example：12 词以内的极简英文仿写。

硬性约束：
- 除 background 外不解释新闻背景，不写全文翻译，只拆骨架；
- 中文总字数（含 background，不含英文原句和例句）不超过 420 字。

只返回下面这种 JSON，不要代码块标记，不要任何解释文字：
{{
  "missing_source": [],
  "sentences": [
    {{
      "source": "The New Yorker",
      "source_feed": "newyorker",
      "matched_criteria": ["L1", "L2", "L3"],
      "tag": "文采 · 通感 · 长句节奏",
      "background": "前情提要，两三句话",
      "text": "英文原句，目标词用 ** 包住",
      "breakdown": "一句话说清画面与手法",
      "phrase": "可迁移的表达 + 中文释义",
      "trap": "最容易犯的中文式英文",
      "example": "12 词以内的极简英文仿写"
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


# 旧的 feed_family()（guardian/bbc/economist 分类）已删除：
# 现在三源各自独立成组，不再需要按媒体名分桶。


JUNK_PARTS = (
    # 通用网页噪音：导航、登录、订阅、Cookie、脚本残留
    "enable javascript", "skip to", "more menu", "sign in", "log in", "subscribe",
    "newsletter", "cookie", "advertisement", "read more", "follow us", "this video",
    "all rights reserved", "getty images", "photograph:", "image caption",
    "function(", "addeventlistener", "document.", "window.", "var ", "javascript",
    # Economist / Wayback 尾巴：正文末尾会带「本文刊于…」和目录推荐，会被当成句子选走
    "appeared in the", "print edition", "discover stories", "list of contents",
    "is also dev", "read the full", "this article", "sign up to", "listen to",
)


def fetch_page_paras(url, min_len=60):
    """抓文章页并取正文段落。

    旧版把整页 HTML 里的 <p> 全掏出来，结果 Guardian 的内联 JS 被当成正文
    （一次抓到 7,758 字符的「Sign in document.addEventListener…」）。所以这里
    先删掉 <script>/<style>/<noscript> 再取 <p>，并按 JUNK_PARTS 过滤导航块。
    """
    if not url:
        return ""
    try:
        html = http_get(url).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 抓正文失败 {url}: {type(exc).__name__}", file=sys.stderr)
        return ""

    html = re.sub(r"<(script|style|noscript)\b.*?</\1>", " ", html, flags=re.S | re.I)
    paras = []
    for chunk in re.findall(r"<p\b[^>]*>(.*?)</p>", html, re.S | re.I):
        text = strip_html(chunk)
        low = text.lower()
        if len(text) < min_len or any(j in low for j in JUNK_PARTS):
            continue
        # 段落之间用空格拼接时，没句号的小标题（Guardian 的 kicker「No flinching」）
        # 会和下一段粘成一句，所以先把每段补齐终止标点。
        if not text.rstrip().endswith((".", "!", "?", "”", '"', "’")):
            text = text.rstrip() + "."
        paras.append(text)
    return " ".join(paras)


def fetch_wayback_text(url):
    """Economist 正文只能从 Wayback 快照拿：先问 availability API 有没有快照，再取 id_ 版本。

    没有快照时返回空字符串（调用方据此记 missing_source），不做二次重试，
    因为「没有就是没有」，重试只会拖慢整条流水线。
    """
    import urllib.parse  # 只在 Wayback 这里用到，避免顶部 import 噪音

    try:
        api = "https://archive.org/wayback/available?url=" + urllib.parse.quote(url, safe="")
        payload = json.loads(http_get(api, timeout=20).decode("utf-8", "replace"))
        snap = (payload.get("archived_snapshots") or {}).get("closest") or {}
        timestamp = snap.get("timestamp") or ""
        if not timestamp:
            return ""
        # 加 id_ 后缀：拿原始页面，不要 Wayback 自己的工具条和重写过的链接
        return fetch_page_paras(f"https://web.archive.org/web/{timestamp}id_/{url}")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Wayback 取正文失败 {url}: {type(exc).__name__}", file=sys.stderr)
        return ""


def fetch_jsonld_body(url):
    """从页面的 JSON-LD（schema.org NewsArticle）里取 articleBody。

    New Yorker 的文章页有 900KB，正文段落里混着导航、推荐位和付费墙文案；
    但它在 SEO 用的 ld+json 里放了一份干净的 articleBody（实测 6,000–7,700 字符，
    结尾是 ♦）。比正则掏 <p> 干净得多，也不需要绕过订阅墙。
    拿不到就返回空，调用方退回 fetch_page_paras。
    """
    if not url:
        return ""
    try:
        html = http_get(url).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 抓 JSON-LD 失败 {url}: {type(exc).__name__}", file=sys.stderr)
        return ""

    for raw in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        # 有的页面是 @graph 数组，有的是单个对象，统一摊平后再找 articleBody
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict):
                continue
            body = node.get("articleBody") or ""
            if body:
                # 去掉结尾的 ♦ 和多余空白，正文末尾那个装饰符不是句子
                return re.sub(r"\s*♦\s*$", "", body.strip()).strip()
    return ""


def ensure_body(item, source, min_body=600):
    """按每个源各自的抽取方式补正文；返回补到的全文（可能为空）。"""
    body = item.get("body") or ""
    link = item.get("link") or ""
    extract = source.get("extract", "feed")

    if extract == "page":
        # Guardian Long Read：RSS 只给开头一两段（实测 665–2150 字符），而 min_body
        # 是 600，很多条目会「刚好够长」从而跳过抓页面，候选句就全锁死在文章开头。
        # 所以这里**不看长度**，一律去抓文章页；抓不到才退回 RSS 摘要。
        fetched = fetch_page_paras(link)
        if fetched:
            return fetched
        return body
    if extract == "jsonld":
        # New Yorker：RSS 只有几十字导语，正文只在文章页的 JSON-LD 里。
        # 同样不看 RSS 长度；JSON-LD 拿不到时退回抓段落，再不行才用导语。
        fetched = fetch_jsonld_body(link) or fetch_page_paras(link)
        if not fetched:
            log_failure(source, "JSON-LD 与正文段落都取不到")
        return fetched or body
    if extract == "wayback":
        # Economist 曾经走这条（页面 403，只能吃存档快照）。现在源已换成 New Yorker，
        # 保留这个分支是为了 FEEDS 覆盖时还能接回需要存档的源。
        fetched = fetch_wayback_text(link)
        if not fetched:
            log_failure(source, "正文 403，Wayback 无快照")
        return fetched or body

    # feed：Atlantic 的 Atom content 本身就是全文，够 600 就直接用
    if len(body) >= min_body:
        return body
    return fetch_page_paras(link) or body


def log_failure(src, error):
    """某个源抓不到正文时的统一文案，方便在 Actions 日志里搜索。

    Economist 时代要求固定打印「Economist RSS 不可用」；换源后同样给 New Yorker
    一个固定前缀，否则日志里只剩一行普通 warning，事后根本搜不到。
    返回统一后的错误文案（同时写进 collected[src]['error']，渲染时也要能看到）。
    """
    key = src.get("key")
    if key == "newyorker":
        text = f"New Yorker RSS 不可用（{error}），跳过该源"
    elif key == "economist":
        text = f"Economist RSS 不可用（{error}），跳过该源"
    else:
        text = f"{src['name']}：{error}"
    print(f"[warn] {text}", file=sys.stderr)
    return text


def is_text_article(item):
    """音频/视频/谜题条目没有可精读的正文，必须跳过。

    实测两种：Guardian 的 podcast 页 <p> 数量为 0；New Yorker 的 feed 里混着
    填字游戏（Play Shuffalo / Laugh Lines）、漫画和播客，抓下来只有交互文案。
    诗和小说也不算「句子」素材，一并排除。
    """
    link = (item.get("link") or "").lower()
    title = (item.get("title") or "").lower()
    if any(mark in link for mark in (
        "/audio/", "/video/", "/podcast/", "/radio/",
        "puzzles-and-games-dept", "/cartoons/", "/newsletters/",
        "/poetry/", "-poem", "/feed/",
    )):
        return False
    if title.startswith("play ") and ":" in title:
        return False
    if title.rstrip().endswith(("podcast", "– podcast", "- podcast", "audio")):
        return False
    return True


def collect_by_source(window_hours=30, per_feed=3, min_body=600):
    """三个源各自独立抓取，每源最多 per_feed 篇。

    和旧版的关键差别：不再把卫报和 BBC 揉进一个池子按配额分配，而是每个源
    单独一份结果。某个源失败或没有合格正文时，只记 error，绝不从别的源补位——
    宁可当天只有 2 句，也不要凑数。
    """
    feeds_env = [u.strip() for u in env("FEEDS", "").split(",") if u.strip()]
    sources = []
    for idx, src in enumerate(SOURCES):
        merged = dict(src)
        # FEEDS 按顺序覆盖三个地址，抽取方式（page/wayback/feed）仍跟着位置走
        if idx < len(feeds_env):
            merged["url"] = feeds_env[idx]
        sources.append(merged)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    allow_stale = env("ALLOW_STALE", "1") != "0"
    seen_links, seen_titles = set(), set()
    result = {}

    for src in sources:
        picked, error = [], ""
        try:
            entries = parse_feed(http_get(src["url"]), src["url"])
        except Exception as exc:  # noqa: BLE001
            error = log_failure(src, f"feed 抓取失败（{type(exc).__name__}）")
            entries = []

        # 音频/视频条目先剔除：它们没有可精读的正文
        entries = [item for item in entries if is_text_article(item)]

        # 第一轮：严格 30 小时窗口（需求第二节）
        strict = [item for item in entries
                  if not item.get("published") or item["published"] >= cutoff]
        # 第二轮（兜底）：窗口内没有正文时，退回「该源最新的文字文章」，并打 stale 标记。
        # 原因：Guardian Long Read 隔天发 podcast、Economist 每周四才更一次，
        # 统一 30 小时会让这两个源大多数日子直接空掉；用户要的是「每天各一句」。
        # 想严格只吃 30 小时内的新文章，把 ALLOW_STALE 设成 0 即可。
        batches = [("strict", strict)]
        if allow_stale:
            batches.append(("stale", entries))

        stale_used = False
        for mode, batch in batches:
            if picked:
                break
            for item in batch:
                if len(picked) >= per_feed:
                    break
                link, title = item.get("link", ""), item.get("title", "")
                title_key = re.sub(r"[^a-z0-9]+", "", title.lower())[:60]
                # 去重沿用旧逻辑：链接 + 标题规范化前 60 字
                if link and link in seen_links:
                    continue
                if title_key and title_key in seen_titles:
                    continue
                published = item.get("published")
                if mode == "strict" and published and published < cutoff:
                    continue
                body = ensure_body(item, src, min_body)
                if len(body) < min_body:
                    continue
                item["body"] = body
                item["feed_key"] = src["key"]
                item["feed_name"] = src["name"]
                item["stale"] = mode == "stale"
                if item["stale"]:
                    stale_used = True
                    print(f"[info] {src['name']} 窗口内无新文章，退回过期文章：{title[:40]}", file=sys.stderr)
                picked.append(item)
                if link:
                    seen_links.add(link)
                if title_key:
                    seen_titles.add(title_key)

        if picked:
            # 用了过期文章时不算失败，但要把「为什么用到旧文」写进日志
            error = "" if not stale_used else "窗口内无新文章，使用最近一篇旧文"
        elif not error:
            reason = "30 小时窗口内没有可用正文（该源更新较慢，多数日子没有新文章）" \
                if src.get("key") == "newyorker" else "30 小时窗口内没有可用正文"
            error = log_failure(src, reason)

        result[src["key"]] = {"name": src["name"], "items": picked, "error": error}

    return result


# ── 候选句前置粗筛 ──────────────────────────────────────────
# 目的：把「整篇正文」先压到 10–15 个高潜力句子再交给模型。
# 规则是纯机械的（不依赖模型），模型只负责在剩下的句子里挑和讲。
CONNECTORS = (
    "while", "whilst", "despite", "whereas", "although", "though", "thereby", "albeit",
    "notwithstanding", "conversely", "paradoxically", "insofar as", "even as", "let alone",
    "rather than", "instead", "but", "yet", "because", "as a result", "so that",
)
NONFINITE = re.compile(r"\b(?:having\s+\w+|\w+ing\b|\w+ed\b|to\s+\w+)\b", re.I)
QUOTE_RE = re.compile(r"[\"\u201c](.+?)[\"\u201d]", re.S)
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\u201c\"'(])")


def _words(sentence):
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’\-]*", sentence))


def prefilter_sentences(text, limit=15, literary=False):
    """按来源的选句标准做机械粗筛，返回高潜力句子列表。

    结构组（Guardian / Atlantic）规则：
      1. 词数 <25 或 >60 直接丢弃——太短没有结构，太长会变成「变态长句」；
      2. 引号内直接引语超过 15 词丢弃——纯引语句没有可讲的语法结构；
      3. 以 According to / Said / Mr / Ms 开头且没有从句的丢弃；
      4. 既不含逻辑连接词、也不含非谓语动词短语的丢弃（说明只是简单主谓宾）；
      5. 含单字母残词（Guardian 首字母下沉的 <span>I</span>n 会变成「I n」）的直接丢弃。

    文采组（New Yorker）规则换一套：语法清单会把这些句子全部滤掉，
    因为好散文往往就是「简单句 + 一个漂亮的比喻」。这里改为：
      1. 词数 18–55（太短没有气口，太长一屏放不下）；
      2. 必须有文学信号：比喻（as if / like）、破折号、分号，或 LITERARY_MARKS 里的意象词；
      3. 丢采访引语（含 said / says）和带数字的句子（那是新闻信息）；
      4. 丢以引号开头、以问号收尾的句子（对话与设问，不适合当精读素材）。
    """
    out = []
    for raw in SENT_SPLIT.split(text or ""):
        sentence = re.sub(r"\s+", " ", raw).strip()
        if len(sentence) < 40:
            continue
        words = _words(sentence)
        low = sentence.lower()

        if literary:
            if words < 18 or words > 55:
                continue
            if re.search(r"\b(said|says|asked|told reporters)\b", low):
                continue
            if re.search(r"\d", sentence):
                continue
            if sentence[0] in "\"““" or sentence.rstrip().endswith("?"):
                continue
            if not any(mark in low for mark in LITERARY_MARKS):
                continue
            out.append(sentence)
            if len(out) >= limit:
                break
            continue

        if words < 25 or words > 60:
            continue
        # 规则 5：单字母残词（Guardian 首字母下沉的 <span>I</span>n 会变成「I n」）
        # 只排 a / i（正常冠词和代词），其余孤立单字母基本都是抓取残渣
        if re.search(r"(?:^|\s)([b-hj-zB-HJ-Z])(?:\s|$)", sentence):
            continue
        # 规则 2：引语超长 → 说明这句主要在转述别人的话
        quotes = QUOTE_RE.findall(sentence)
        if quotes and max(_words(q) for q in quotes) > 15:
            continue
        # 规则 3：导语式开头 + 没有从句结构
        if re.match(r"^(according to|said|says|mr\.?|ms\.?|mrs\.?)\b", low) and not re.search(
            r"\b(which|that|who|whether|while|although|despite|because|as)\b", low
        ):
            continue
        # 规则 4：连接词和非谓语短语都没有，就是简单主谓宾
        has_connector = any(f" {c} " in f" {low} " for c in CONNECTORS)
        has_nonfinite = bool(NONFINITE.search(sentence))
        if not has_connector and not has_nonfinite:
            continue
        out.append(sentence)
        if len(out) >= limit:
            break
    return out


def build_sentence_pool(collected, limit=15):
    """对每个源的文章正文做粗筛，返回 {源 key: [候选句]}。

    文采组多给一些候选（limit×2）：文学句的命中率低，池子太小模型只能将就。
    """
    pool = {}
    for key, entry in collected.items():
        literary = any(src["key"] == key and src.get("style") == "literary" for src in SOURCES)
        cap = limit * 2 if literary else limit
        seen = set()
        sentences = []
        for article in entry["items"]:
            for candidate in prefilter_sentences(article["body"], limit=cap, literary=literary):
                fingerprint = candidate[:70].lower()
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                sentences.append(candidate)
                if len(sentences) >= cap:
                    break
            if len(sentences) >= cap:
                break
        pool[key] = sentences
    return pool


# 旧的 collect_articles（按 guardian/bbc 分桶 + 配额互补）已删除：
# 需求改成一个源一天一句，失败不补位，所以由上面的 collect_by_source 取代。


# ────────────────────────────── 模型 ──────────────────────────────


def build_prompt(collected, pool, date_str):
    """把每个源的候选句分组写进提示词；没有候选句的源明确写「缺失」。

    每组标题下面直接贴这一组自己的选用标准：结构组贴 A–E，文采组贴 L1–L5。
    这样模型不需要在两套标准之间自己判断「这个源该用哪套」，省掉一类错选。
    """
    blocks, expected = [], 0
    for src in SOURCES:
        key = src["key"]
        sentences = pool.get(key) or []
        criteria = LITERARY_CRITERIA if src.get("style") == "literary" else STRUCT_CRITERIA
        head = f"【{src['name']}】（feed_key={key}）\n{criteria}\n候选句："
        if not sentences:
            blocks.append(f"{head}\n今天该来源没有合格候选句，缺失。\n")
            continue
        expected += 1
        lines = "\n".join(f"{i}. {s}" for i, s in enumerate(sentences, 1))
        blocks.append(f"{head}\n{lines}\n")
    return USER_TEMPLATE.format(date=date_str, pool="\n".join(blocks), expected=expected)


def _chat(prompt, max_tokens=1800):
    """一次模型调用。temperature 固定 0.2（需求第六节）。"""
    api_key = env("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY")
    base_url = env("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    payload = {
        "model": env("LLM_MODEL", "deepseek-chat"),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        # 0.5 → 0.2：选句是「从固定候选池里挑 + 拆解」，不需要创造性，
        # 温度越低越不容易每次换一批句子，也更容易守住 25–60 词和结构要求。
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    raw = http_post_json(f"{base_url}/chat/completions", payload, headers)
    return json.loads(raw)["choices"][0]["message"]["content"]


def extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON")
    return json.loads(text[start:end + 1])


def looks_too_simple(sentence, literary=False):
    """返回后校验的规则版：粗判「这句值不值得讲」。

    结构组：纯新闻叙事句，或简单到不需要推理（数字 + 被动 + 时间；没有任何从句/插入语/非谓语）。
    文采组：没有任何文学信号（比喻、破折号、分号、意象词），或者干脆是引语和数字句——
            这类句子语法上可能很漂亮，但「文采」这一栏会是空的。

    不追求百分之百准确，只决定「要不要让模型重选一次」。
    """
    low = sentence.lower()
    if literary:
        if re.search(r"\b(said|says)\b", low) or re.search(r"\d", sentence):
            return True
        return not any(mark in low for mark in LITERARY_MARKS)

    passive = re.search(
        r"\b(?:was|were|is|are|has been|have been|had been)\s+"
        r"(?:\w+ed|built|held|made|given|taken|known|seen|found|told)\b",
        sentence, re.I,
    )
    numbers = re.search(r"\b\d[\d,.]*\b", sentence)
    weekday = re.search(
        r"\b(?:on|in|at)\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b",
        sentence, re.I,
    )
    # 条件一：纯新闻叙事 = 被动 + 数字 + 时间状语
    if passive and numbers and weekday:
        return True
    # 条件二：没有任何从句引导词 / 插入语 / 非谓语 → 结构过简
    has_clause = re.search(
        r"\b(?:which|that|who|whom|whose|whether|while|although|though|despite|because|"
        r"whereas|unless|provided|as|when|where|if)\b",
        sentence, re.I,
    )
    has_insert = any(mark in sentence for mark in ("—", "–", "(", ","))
    if not has_clause and not has_insert and not NONFINITE.search(sentence):
        return True
    return False


def normalize_sentences(raw_sentences, pool):
    """保证「一个源最多一句」，并补上 source_feed / text 缺失时的兜底。"""
    picked, used = [], set()
    for item in raw_sentences or []:
        text = (item.get("text") or "").strip()
        feed = (item.get("source_feed") or "").strip().lower()
        if not text:
            continue
        # 模型偶尔不写 source_feed：按原句是否出现在某组候选里反查
        if feed not in pool:
            for key, sentences in pool.items():
                if any(text.strip("*").strip()[:60] == s[:60] for s in sentences):
                    feed = key
                    break
        if feed in used or feed not in pool:
            continue
        item["source_feed"] = feed
        item.setdefault("matched_criteria", [])
        picked.append(item)
        used.add(feed)
    return picked


def replace_one(sentence, candidates, collected, date_str, literary=False):
    """让模型从同一来源的候选池里重选一句（最多替换 1 次）。

    提示词按来源风格分成两版：结构组说「纯新闻叙事 / 结构过简」，
    文采组说「没有画面和手法」——照搬结构组的话，模型会把散文句越换越像公文。
    """
    joined = "\n".join(f"{i}. {s}" for i, s in enumerate(candidates, 1))
    if literary:
        verdict = "这句没有画面、没有比喻、也没有节奏，只是一句普通陈述（文采不合格）"
        demand = ("要求：18–55 词；至少命中 L1–L5 中的 2 条（具体画面 / 比喻通感 / 节奏讲究 / "
                  "格言感 / 文学用词）；不要采访引语，不要带数字的信息句。")
    else:
        verdict = "这句被判定为「纯新闻叙事句 / 结构过简，不需要推理就能读懂」，不适合做精读素材"
        demand = ("要求：25–60 词；至少满足「让步/条件/原因状语从句 + 逻辑张力」「非限制性定语从句或插入语」"
                  "「名词性从句」「分词短语」「易误读的修饰关系」中的 3 条；不能是纯新闻叙事句。")
    prompt = (
        f"日期：{date_str}\n"
        f"读者画像：中国大一电子信息类学生，已背完六级词汇，只用手机看，抗拒长篇大论。\n\n"
        f"下面这句：{verdict}：\n"
        f"「{sentence.get('text', '')}」\n\n"
        f"请从同一来源的候选句里另外选一句（不要选上面这句，也不要与已选句子重复）：\n{joined}\n\n"
        f"{demand}\n"
        f"字段与之前一致（source/source_feed/matched_criteria/tag/background/text/breakdown/phrase/trap/example），"
        f"只返回 JSON：{{\"sentence\": {{...}}}}"
    )
    parsed = extract_json(_chat(prompt, max_tokens=900))
    replacement = parsed.get("sentence") or {}
    if replacement.get("text") and not looks_too_simple(replacement["text"], literary=literary):
        replacement["source_feed"] = (sentence.get("source_feed") or "")
        replacement.setdefault("matched_criteria", [])
        return replacement
    return None


def call_llm(collected, pool, date_str):
    """挑句 + 拆解。返回 (sentences, missing_source)。"""
    if not any(pool.values()):
        raise RuntimeError("三个源都没有候选句")
    prompt = build_prompt(collected, pool, date_str)
    expected = sum(1 for key in pool if pool[key])

    last = None
    parsed = None
    for attempt in range(2):
        try:
            parsed = extract_json(_chat(prompt))
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"[warn] 模型第 {attempt + 1} 次失败：{exc}", file=sys.stderr)
            time.sleep(4)
    if parsed is None:
        raise RuntimeError(f"模型调用失败：{last}")

    sentences = normalize_sentences(parsed.get("sentences"), pool)

    # 第六节：模型返回后逐句校验，不合格的从同源候选池替换（最多 1 次）。
    # 校验标准跟着来源风格走：New Yorker 那句看文采，其余看结构。
    style_of = {src["key"]: src.get("style", "structural") for src in SOURCES}
    for idx, sentence in enumerate(list(sentences)):
        literary = style_of.get(sentence.get("source_feed"), "structural") == "literary"
        if not looks_too_simple(sentence.get("text", ""), literary=literary):
            continue
        print(f"[warn] 第 {idx + 1} 句未通过典型性校验，尝试替换：{sentence.get('text', '')[:50]}", file=sys.stderr)
        replacement = None
        try:
            replacement = replace_one(
                sentence, pool.get(sentence["source_feed"], []), collected, date_str, literary=literary
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 替换失败：{exc}", file=sys.stderr)
        if replacement:
            sentences[idx] = replacement

    missing = [src["key"] for src in SOURCES if not pool.get(src["key"])]
    if len(sentences) < expected:
        print(f"[warn] 期望 {expected} 句，模型只给出 {len(sentences)} 句", file=sys.stderr)
    return sentences, missing


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
    """按三个来源统计句数，用于页头和推送里的「Long Read ×1 · New Yorker ×1」提示。"""
    labels = {"guardian": "Long Read", "newyorker": "New Yorker", "atlantic": "Atlantic"}
    counts = {}
    for item in sentences:
        key = (item.get("source_feed") or "").strip().lower()
        name = labels.get(key, "其他")
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
    # 三源缺一个时只剩两句，标题跟着变，别硬写「三句」
    head_word = {3: "三句", 2: "两句", 1: "一句"}.get(len(sentences), f"{len(sentences)} 句")
    back = '<a href="../index.html">← 最新一期</a>' if is_archive else ''
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>今日{head_word} · 外刊精读 · {date_str}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="head">
    <div class="eyebrow">Daily Digest · 外刊精读</div>
    <h1>今日<em>{head_word}</em></h1>
    <p class="sub">不译全文，只拆骨架。</p>
    <div class="meta">
      <span>{date_str}</span>
      <span><b>{len(sentences)}</b> 句 · <b>8</b> 分钟</span>
      <span>{mixed}</span>
      <span>IELTS 7+ / TOEFL 100+ · 结构 + 文采</span>
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
    # 链接带时间戳，绕开 GitHub Pages 的 10 分钟 CDN 缓存和微信内置浏览器缓存
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d%H%M")
    link = f"{page_url()}?d={stamp}"
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
        {"source": "The Guardian · Long Read",
         "source_feed": "guardian",
         "tag": "分词状语前置 · 插入语 · 冒号补充",
         "background": "一场在酒店里办的扇耳光比赛，主办方怕选手赛前动手，干脆把人按楼层分开住。",
         "text": "Sitting in a meeting room at UFC's head office in Las Vegas, White, as CEO, **was supposed to be** discussing matchups and pay-per-view deals, yet his attention **was elsewhere**: specifically, on a social media video of two large Russian men taking turns to slap each other.",
         "breakdown": "Sitting 分词短语先摆姿势，主句主语是 White，中间插 as CEO；yet 一转，冒号后再补一句他真正在看什么。",
         "phrase": "be supposed to be doing 本该在做；attention is elsewhere 心思不在这儿",
         "trap": "别把 as CEO 当成主语的一部分读成「作为 CEO 的 White」——它是插入语，先跳过去读主句。",
         "example": "He was supposed to be reviewing slides, yet his mind was elsewhere."},
        {"source": "The New Yorker",
         "source_feed": "newyorker",
         "tag": "文采 · 通感 · 破折号补刀",
         "background": "芬奇执导、塔伦蒂诺编剧的《好莱坞往事》续集，影评人说这片子的装饰性桥段比故事本身还抢眼。",
         "text": "Tarantino's script **dangles** a gaudy array of decorative events and tangy characters, florid dialogue and snappy gestures that are more than the movie's raison d'être—they're its main matter.",
         "breakdown": "主语 script，动词 dangles——把一堆花哨东西晃在你眼前；破折号后一句 they're its main matter，等于说「这些装饰就是全部」。",
         "phrase": "dangle sth before sb 拿…在眼前晃；a gaudy array of 一堆花里胡哨的",
         "trap": "别译成「丰富多彩」。tangy 本是味道、florid 本是花色，作者故意用感官词形容人物和台词。",
         "example": "The menu dangles a gaudy array of choices."},
        {"source": "The Atlantic · Ideas",
         "source_feed": "atlantic",
         "tag": "Just as 对比 · 不定式插入 · 过去分词定语",
         "background": "作者回看特朗普治下那几年，写法院和国会如何一路让路，直到权力集中在一个人手里。",
         "text": "**Just as** Americans who have known no system other than liberal democracy struggle to imagine its downfall, to understand an authoritarian American president, we tried to compare him to analogues past and present.",
         "breakdown": "Just as 引导比较状语从句先立参照系，主句是 we tried to compare him；中间插不定式 to understand 说明目的。",
         "phrase": "know no system other than 只知道…这一种制度；analogues past and present 古今先例",
         "trap": "Just as 不是客套话，它要求后半句也有一个完整动作；写成 Just as the Constitution, the court… 就断了。",
         "example": "Just as fish cannot imagine dry land, we struggled to picture its collapse."},
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
        # 三源各取最多 3 篇，不互相顶替（需求第二节）
        window = int(env("WINDOW_HOURS", "30"))
        collected = collect_by_source(window, per_feed=3)
        for key, entry in collected.items():
            state = "缺失" if not entry["items"] else f"{len(entry['items'])} 篇"
            note = f"（{entry['error']}）" if entry.get("error") else ""
            print(f"[info] {entry['name']}：{state}{note}")

        pool = build_sentence_pool(collected, limit=int(env("POOL_LIMIT", "15")))
        for key, sentences in pool.items():
            print(f"[info] {key} 候选句 {len(sentences)} 条")
        if not any(pool.values()):
            print("[error] 三个源都没有候选句", file=sys.stderr)
            push_pushplus("⚠️ 外刊精读失败", "<p>今天三个源都没抓到可用句子，检查 RSS 或网络。</p>")
            return 1

        articles = [item for entry in collected.values() for item in entry["items"]]
        print("[info] 调用模型挑句…")
        sentences, missing = call_llm(collected, pool, date_str)
        if not sentences:
            print("[error] 模型没有给出可用的句子", file=sys.stderr)
            push_pushplus("⚠️ 外刊精读失败", "<p>模型没有给出可用句子。</p>")
            return 1
        if missing:
            print(f"[warn] 缺失来源：{', '.join(missing)}（按需求不跨源补位）", file=sys.stderr)

    write_outputs(date_str, sentences, articles)

    title = f"☕ 外刊精读 · {datetime.now(CST).strftime('%m月%d日')}"
    payload = {"title": title, "html": build_push_html(date_str, sentences)}
    with open(PAYLOAD, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)

    print(f"[info] 已生成 docs/index.html 和 docs/archive/{date_str}.html")
    print(f"[info] 推送内容已存 push-payload.json（{len(payload['html'])} 字符）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        # 早七点没人守着，出错至少要发一条微信，别让用户以为「今天没有外刊」
        print(f"[error] 运行失败：{exc}", file=sys.stderr)
        try:
            push_pushplus("⚠️ 外刊精读失败", f"<p>今天没生成内容：{esc(str(exc))[:200]}</p>")
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
