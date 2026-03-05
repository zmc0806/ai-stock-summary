#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全球情报日报生成器（专业中文输出）
- 拉取RSS/Atom新闻
- 关键词威胁分级 + 去重
- 调用 Ollama（默认 qwen3:14b）分别生成：
  1) 地缘政治风险评估（面向机构）
  2) 宏观/市场方向简报（面向交易台）
  3) 60秒可读的执行摘要（融合两者）
- 生成 Markdown 报告（中文）

用法：
    python intelligence_report_cn.py
    python intelligence_report_cn.py --output-dir ./my_reports
    python intelligence_report_cn.py --no-file
    python intelligence_report_cn.py --model qwen3:14b

依赖：
    pip install requests
"""

import re
import os
import sys
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

# ─── 配置 ────────────────────────────────────────────────────────────────

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:14b")

FETCH_TIMEOUT = 12        # 单个feed请求超时（秒）
MAX_CONCURRENT_FEEDS = 20 # 并发抓取feed数量
ITEMS_PER_FEED = 5        # 每个feed最多保留多少条
MAX_HEADLINES_GEO = 30    # 输入地缘政治分析的headline数量
MAX_HEADLINES_ECON = 25   # 输入宏观/市场分析的headline数量
OUTPUT_DIR = "reports"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# ─── Feed 定义 ───────────────────────────────────────────────────────────

# Geopolitics: politics, regional conflict, energy, think tanks, crisis
GEO_FEEDS: dict[str, list[dict]] = {
    "全球政治": [
        {"name": "BBC World",         "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
        {"name": "Guardian World",    "url": "https://www.theguardian.com/world/rss"},
        {"name": "Al Jazeera",        "url": "https://www.aljazeera.com/xml/rss/all.xml"},
        {"name": "France 24",         "url": "https://www.france24.com/en/rss"},
        {"name": "DW News",           "url": "https://rss.dw.com/xml/rss-en-all"},
    ],
    "美国与美洲": [
        {"name": "NPR News",          "url": "https://feeds.npr.org/1001/rss.xml"},
        {"name": "ABC News",          "url": "https://feeds.abcnews.com/abcnews/topstories"},
        {"name": "PBS NewsHour",      "url": "https://www.pbs.org/newshour/feeds/rss/headlines"},
        {"name": "BBC Latin America", "url": "https://feeds.bbci.co.uk/news/world/latin_america/rss.xml"},
    ],
    "欧洲": [
        {"name": "EuroNews",          "url": "https://www.euronews.com/rss?format=xml"},
        {"name": "Le Monde EN",       "url": "https://www.lemonde.fr/en/rss/une.xml"},
        {"name": "BBC Europe",        "url": "https://feeds.bbci.co.uk/news/world/europe/rss.xml"},
    ],
    "中东": [
        {"name": "BBC Middle East",   "url": "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml"},
        {"name": "Guardian ME",       "url": "https://www.theguardian.com/world/middleeast/rss"},
    ],
    "亚太": [
        {"name": "BBC Asia",          "url": "https://feeds.bbci.co.uk/news/world/asia/rss.xml"},
        {"name": "The Diplomat",      "url": "https://thediplomat.com/feed/"},
        {"name": "CNA",               "url": "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml"},
    ],
    "非洲": [
        {"name": "BBC Africa",        "url": "https://feeds.bbci.co.uk/news/world/africa/rss.xml"},
    ],
    "危机与安全": [
        {"name": "CrisisWatch",       "url": "https://www.crisisgroup.org/rss"},
        {"name": "IAEA",              "url": "https://www.iaea.org/feeds/topnews"},
        {"name": "WHO",               "url": "https://www.who.int/rss-feeds/news-english.xml"},
        {"name": "UN News",           "url": "https://news.un.org/feed/subscribe/en/news/all/rss.xml"},
    ],
    "智库": [
        {"name": "Foreign Policy",    "url": "https://foreignpolicy.com/feed/"},
        {"name": "Atlantic Council",  "url": "https://www.atlanticcouncil.org/feed/"},
        {"name": "Foreign Affairs",   "url": "https://www.foreignaffairs.com/rss.xml"},
    ],
    "政府 / 政策": [
        {"name": "Federal Reserve",   "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
        {"name": "SEC",               "url": "https://www.sec.gov/news/pressreleases.rss"},
    ],
}

# Finance: markets, macro, commodities, forex, bonds, crypto, regulation
FINANCE_FEEDS: dict[str, list[dict]] = {
    "市场": [
        {"name": "CNBC",              "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html"},
        {"name": "Yahoo Finance",     "url": "https://finance.yahoo.com/rss/topstories"},
        {"name": "Seeking Alpha",     "url": "https://seekingalpha.com/market_currents.xml"},
    ],
    "宏观 / 经济": [
        {"name": "Financial Times",   "url": "https://www.ft.com/rss/home"},
        {"name": "WSJ US",            "url": "https://feeds.content.dowjones.io/public/rss/RSSUSnews"},
    ],
    "大宗商品": [
        {"name": "Oil Price",         "url": "https://oilprice.com/rss/main"},
    ],
    "加密资产": [
        {"name": "CoinDesk",          "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
        {"name": "Cointelegraph",     "url": "https://cointelegraph.com/rss"},
    ],
    "监管与央行": [
        {"name": "Federal Reserve",   "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
        {"name": "SEC Releases",      "url": "https://www.sec.gov/news/pressreleases.rss"},
    ],
}

# ─── 威胁分级（基于英文关键词；来源多为英文RSS） ────────────────────────────

THREAT_KEYWORDS: dict[str, list[str]] = {
    "CRITICAL": [
        "nuclear", "nuke", "missile strike", "invasion", "war declared", "declaration of war",
        "genocide", "chemical attack", "biological attack", "article 5", "nuclear weapon",
        "ballistic missile", "icbm", "mass casualty", "coup", "government collapse",
        "martial law", "assassination", "tactical nuclear",
    ],
    "HIGH": [
        "war", "armed conflict", "airstrike", "drone strike", "missile", "shelling", "bombing",
        "terrorist attack", "terror", "cyberattack", "hacked", "ransomware",
        "earthquake", "tsunami", "hurricane", "catastrophic", "explosion",
        "troops deployed", "sanctions", "embargo", "market crash", "recession",
        "bank run", "default", "military operation", "offensive", "ceasefire collapsed",
    ],
    "MEDIUM": [
        "protest", "riot", "strike", "unrest", "military exercise", "drills",
        "diplomatic crisis", "expelled", "recalled ambassador", "trade war", "tariff",
        "economic slowdown", "inflation surge", "interest rate", "fed rate", "rate hike",
        "flood", "wildfire", "drought", "humanitarian crisis", "refugee",
        "election", "political crisis", "parliament dissolved", "layoffs", "job cuts",
    ],
    "LOW": [
        "summit", "meeting", "talks", "agreement", "deal", "treaty", "signed",
        "climate", "environment", "vaccine", "health initiative",
        "trade agreement", "partnership", "cooperation", "aid",
    ],
}

THREAT_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
THREAT_CN = {"CRITICAL": "极高", "HIGH": "高", "MEDIUM": "中", "LOW": "低"}


def classify_threat(title: str, description: str = "") -> str:
    text = (title + " " + description).lower()
    for level in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        if any(kw in text for kw in THREAT_KEYWORDS[level]):
            return level
    return "LOW"

# ─── 数据模型 ─────────────────────────────────────────────────────────────

@dataclass
class NewsItem:
    source:      str
    category:    str
    title:       str
    link:        str
    published:   str
    description: str = ""
    threat:      str = "LOW"

# ─── RSS/Atom 抓取与解析 ───────────────────────────────────────────────────

_NS = {
    "atom":    "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc":      "http://purl.org/dc/elements/1.1/",
}

def _text(el: Optional[ET.Element]) -> str:
    if el is None:
        return ""
    return re.sub(r"<[^>]+>", " ", el.text or "").strip()


def parse_rss(xml_text: str, source_name: str, category: str) -> list[NewsItem]:
    items: list[NewsItem] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        # strip BOM / preamble and retry
        xml_text = re.sub(r"^[^\x3c]*", "", xml_text, count=1)
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return items

    tag = root.tag.lower()

    if "feed" in tag:
        # Atom feed
        ns = "http://www.w3.org/2005/Atom"
        entries = root.findall(f"{{{ns}}}entry") or root.findall("entry")
        for entry in entries[:ITEMS_PER_FEED]:
            title_el = entry.find(f"{{{ns}}}title") or entry.find("title")
            link_el  = entry.find(f"{{{ns}}}link")  or entry.find("link")
            pub_el   = (entry.find(f"{{{ns}}}updated") or
                        entry.find(f"{{{ns}}}published") or
                        entry.find("updated") or entry.find("published"))
            desc_el  = entry.find(f"{{{ns}}}summary") or entry.find("summary")

            title = _text(title_el)
            link  = (link_el.get("href") or _text(link_el)) if link_el is not None else ""
            pub   = _text(pub_el)
            desc  = _text(desc_el)
            if title:
                items.append(NewsItem(source_name, category, title, link, pub, desc,
                                      classify_threat(title, desc)))
    else:
        # RSS 2.0
        channel = root.find("channel") or root
        for item in channel.findall("item")[:ITEMS_PER_FEED]:
            title_el = item.find("title")
            link_el  = item.find("link")
            pub_el   = item.find("pubDate") or item.find("dc:date", _NS)
            desc_el  = item.find("description") or item.find("content:encoded", _NS)

            title = _text(title_el)
            link  = _text(link_el)
            pub   = _text(pub_el)
            desc  = _text(desc_el)
            if title:
                items.append(NewsItem(source_name, category, title, link, pub, desc,
                                      classify_threat(title, desc)))
    return items


def fetch_feed(feed: dict, category: str) -> list[NewsItem]:
    try:
        resp = requests.get(feed["url"], headers=HEADERS, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        return parse_rss(resp.text, feed["name"], category)
    except Exception:
        return []


def fetch_all_feeds(feed_map: dict[str, list[dict]]) -> list[NewsItem]:
    tasks = [(feed, cat) for cat, feeds in feed_map.items() for feed in feeds]
    results: list[NewsItem] = []
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FEEDS) as executor:
        futures = {executor.submit(fetch_feed, f, c): (f, c) for f, c in tasks}
        for future in as_completed(futures):
            results.extend(future.result())
    return results

# ─── 去重与排序 ────────────────────────────────────────────────────────────

def deduplicate(items: list[NewsItem]) -> list[NewsItem]:
    seen: set[str] = set()
    unique: list[NewsItem] = []
    for item in items:
        key = " ".join(re.sub(r"[^a-z0-9 ]", "", item.title.lower()).split()[:8])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def sort_by_threat(items: list[NewsItem]) -> list[NewsItem]:
    return sorted(items, key=lambda x: THREAT_ORDER.get(x.threat, 4))

# ─── Ollama API ─────────────────────────────────────────────────────────────

def strip_thinking(text: str) -> str:
    """移除 qwen3 <think>...</think> 块。"""
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<think>[\s\S]*", "", text, flags=re.IGNORECASE)
    return text.strip()


def call_ollama(user_prompt: str, system_prompt: str, label: str = "") -> str:
    """
    调用 Ollama API。qwen3 thinking 关闭策略：
      - options: think=false（Ollama >= 0.6.0）
      - prompt 前缀 /no_think（旧版本兼容）
    """
    if label:
        print(f"  [LLM] {label} ...", flush=True)

    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "system": system_prompt,
        "prompt": "/no_think\n\n" + user_prompt,
        "options": {
            "think": False,
            "temperature": 0.3,
            "num_predict": 1600,
        },
    }

    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json=payload,
            timeout=240,
        )
        resp.raise_for_status()
        return strip_thinking(resp.json().get("response", ""))
    except requests.exceptions.ConnectionError:
        print(f"\n[ERROR] 无法连接 Ollama：{OLLAMA_BASE_URL}")
        print("  请先启动：ollama serve")
        sys.exit(1)
    except Exception as e:
        return f"[LLM ERROR: {e}]"

# ─── Prompts（中文专业版） ────────────────────────────────────────────────

SYSTEM_GEO = """\
你是一名服务机构投资者的地缘政治风险分析师。你的任务是评估政治事件如何影响资本市场与企业经营。

【角色约束】
- 受众是需要做决策的机构客户，不是大众读者
- 只允许基于“给定标题文本”进行分析：不得补充背景知识、不得推断标题外信息
- 给出方向性判断，尽量减少“可能/也许/或许”等模糊措辞
- 全文用中文输出；资产与术语可保留英文缩写（UST、DXY、VIX等）
- 若某部分缺乏足够标题信号，写“信息不足”，不要猜测

【严格输出格式】

## 总体风险等级
[极高 / 高 / 中 / 低] — 30字以内一句话理由

## 区域热点（最多3个；只纳入标题信号明确的区域）
### [区域名称]
- 当前态势：（1句，描述正在发生什么）
- 风险方向：（升级 / 缓和 / 僵持）
- 机构关注点：（资本流动/供应链/能源/制裁合规等，最多2句）

## 趋势信号
- [信号1]（不超过40字）
- [信号2]（不超过40字；若无明确第二条则省略）

## 市场传导
（用2–3句说明地缘政治如何传导至资产价格；必须点名资产：UST / Gold / Crude Oil / EM Equities / USD 等；\
只能基于标题中可见事实。）\
"""

USER_GEO = """\
以下为今日地缘政治相关新闻标题，已按威胁等级排序（CRITICAL > HIGH > MEDIUM > LOW）。

{headlines}

规则：
- 每条标题是独立事件：禁止把不同标题的信息拼接为同一事实链
- 只分析标题中明确写出的内容：不要补充背景、不要外推
- 若某个小节信息不足，请直接写“信息不足”或省略该小节

请生成地缘政治风险评估报告：\
"""

SYSTEM_ECON = """\
你是宏观对冲基金的首席策略师，为交易台生成每日市场方向简报。

【分析框架】对每个信号，用四维度给出方向：
1) 风险偏好：Risk-On / Risk-Off / Neutral
2) 美元方向：Stronger / Weaker / Neutral（可指 DXY）
3) 利率预期：Hawkish / Dovish / Neutral
4) 大宗倾向：Bullish / Bearish / Neutral

【角色约束】
- 只允许基于“给定标题文本”提炼信号：不得引入外部数据、不得添加预测基线
- 给出清晰方向性结论，避免“观望/等待更多信息”的空话
- 全文中文输出；金融术语可保留英文（Risk-Off, DXY, VIX, UST 等）
- 某部分缺乏标题信号时写“信息不足”

【严格输出格式】

## 市场情绪
[Risk-On / Risk-Off / Neutral] — 核心驱动（25字以内）

## 关键宏观信号
| 信号 | 资产方向 | 理由 |
|------|----------|------|
| （标题1核心事实） | Bullish/Bearish/Neutral [资产类别] | 1句话 |
| （标题2核心事实） | ... | ... |
| （标题3，最多3行） | ... | ... |

## 大宗与外汇聚焦
- 原油（Crude Oil）：[方向] — 1句理由
- 黄金（Gold）：[方向] — 1句理由
- 美元（DXY）：[方向] — 1句理由
（无明确信号的行请省略）

## 本周需关注的关键事件
- [事件/数据1]：预期影响（1句）
- [事件/数据2]（最多2条；若无信号则省略该小节）

## 尾部风险提示
（当前标题中可见的“小概率高冲击”事件，1句；若无明确线索则整段省略。）\
"""

USER_ECON = """\
以下为今日金融与经济相关新闻标题，已按重要性排序。

{headlines}

规则：
- 每条标题是独立事件：不要把多个标题混为同一事实
- 只分析标题中明确写出的内容：不要引入外部预测或数据
- 表格“信号”列应是标题核心事实的精炼复述

请生成市场与宏观方向简报：\
"""

SYSTEM_EXEC = """\
你是一名为C-Suite撰写情报简报的分析师。高管只有60秒：要知道发生了什么、为什么重要、该怎么做。

【写作规则】
- 严格字数：220–320字（中文）
- 第一句必须是最重要结论（不是背景）
- 需要写清“地缘政治 → 市场”的直接传导链条
- 结尾给出1条明确、可执行的建议（例如“提高黄金对冲比例”/“降低EM久期并提高现金仓位”）
- 必须是连续段落：不要标题、不要项目符号、不要分段
- 全文中文输出；资产/指数缩写可保留英文（UST、DXY、VIX等）\
"""

USER_EXEC = """\
基于以下两份分析报告，写一段融合后的执行摘要（不要分别复述两段报告）。

【地缘政治报告】
{geo_report_excerpt}

【宏观/市场报告】
{econ_report_excerpt}

要求：
- 找出两份报告之间最关键的一条“事件→价格/风险偏好”传导链，并围绕它写
- 不要罗列；输出应是一段统一判断
- 结尾必须是1条明确、可执行的建议（不能是“密切关注/持续跟踪”）

执行摘要：\
"""

# ─── 分析流水线 ───────────────────────────────────────────────────────────

def build_headlines_block(items: list[NewsItem]) -> str:
    # 注意：标题多为英文，括号内给中文类别与来源信息，便于中文分析叙述
    return "\n".join(
        f"[{i.threat}/{THREAT_CN.get(i.threat,'低')}] ({i.source} / {i.category}) {i.title}"
        for i in items
    )


def analyze_geopolitics(items: list[NewsItem]) -> str:
    top = sort_by_threat(items)[:MAX_HEADLINES_GEO]
    return call_ollama(
        USER_GEO.format(headlines=build_headlines_block(top)),
        SYSTEM_GEO,
        "地缘政治分析（中文）",
    )


def analyze_economy(items: list[NewsItem]) -> str:
    top = sort_by_threat(items)[:MAX_HEADLINES_ECON]
    return call_ollama(
        USER_ECON.format(headlines=build_headlines_block(top)),
        SYSTEM_ECON,
        "宏观/市场分析（中文）",
    )


def generate_executive_summary(geo_report: str, econ_report: str) -> str:
    return call_ollama(
        USER_EXEC.format(
            geo_report_excerpt=geo_report[:1200],
            econ_report_excerpt=econ_report[:1200],
        ),
        SYSTEM_EXEC,
        "执行摘要（中文）",
    )


def build_raw_signals_table(geo_items: list[NewsItem], econ_items: list[NewsItem]) -> str:
    all_items = sort_by_threat(deduplicate(geo_items + econ_items))
    rows = ["| 威胁等级 | 来源 | 类别 | 标题 |",
            "|----------|------|------|------|"]
    for item in all_items:
        rows.append(
            f"| {item.threat}/{THREAT_CN.get(item.threat,'低')} | {item.source} | {item.category} | "
            f"{item.title.replace('|', '/')} |"
        )
    return "\n".join(rows)

# ─── 主程序 ────────────────────────────────────────────────────────────────

def main() -> None:
    global OLLAMA_MODEL
    parser = argparse.ArgumentParser(description="全球情报日报生成器（专业中文输出）")
    parser.add_argument("--output-dir", default=OUTPUT_DIR,
                        help=f"报告保存目录（默认：{OUTPUT_DIR}）")
    parser.add_argument("--no-file", action="store_true",
                        help="只输出到控制台，不写入文件")
    parser.add_argument("--model", default=OLLAMA_MODEL,
                        help=f"Ollama 模型名（默认：{OLLAMA_MODEL}）")
    args = parser.parse_args()

    OLLAMA_MODEL = args.model

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%d %H:%M UTC")
    file_slug = now.strftime("%Y%m%d_%H%M")

    print(f"\n{'='*64}")
    print("  全球情报日报生成器（专业中文输出）")
    print(f"  Model : {OLLAMA_MODEL}")
    print(f"  Time  : {timestamp}")
    print(f"{'='*64}\n")

    # 1) 拉取
    print("[1/5] 拉取地缘政治 feeds...", flush=True)
    geo_items = deduplicate(fetch_all_feeds(GEO_FEEDS))
    print(f"      去重后：{len(geo_items)} 条")

    print("[2/5] 拉取金融/宏观 feeds...", flush=True)
    econ_items = deduplicate(fetch_all_feeds(FINANCE_FEEDS))
    print(f"      去重后：{len(econ_items)} 条")

    if not geo_items and not econ_items:
        print("\n[ERROR] 未抓取到任何新闻。请检查网络或 feed URL。")
        sys.exit(1)

    # 2) LLM 分析（顺序执行：执行摘要需要前两段）
    print("[3/5] 生成地缘政治分析（LLM）...", flush=True)
    geo_report = analyze_geopolitics(geo_items)

    print("[4/5] 生成宏观/市场分析（LLM）...", flush=True)
    econ_report = analyze_economy(econ_items)

    print("[5/5] 生成执行摘要（LLM）...", flush=True)
    exec_summary = generate_executive_summary(geo_report, econ_report)

    # 3) 组装 Markdown 报告
    counts = {lvl: sum(1 for i in geo_items + econ_items if i.threat == lvl)
              for lvl in ("CRITICAL", "HIGH", "MEDIUM", "LOW")}

    raw_signals = build_raw_signals_table(geo_items, econ_items)

    report = f"""# 全球情报日报（World Intelligence Report）
**生成时间：** {timestamp}  
**模型：** {OLLAMA_MODEL}  
**来源：** 地缘政治 {len(geo_items)} 条 · 金融/宏观 {len(econ_items)} 条  
**威胁分布：** CRITICAL(极高) {counts['CRITICAL']} | HIGH(高) {counts['HIGH']} | MEDIUM(中) {counts['MEDIUM']} | LOW(低) {counts['LOW']}

---

## 执行摘要（融合地缘政治 + 市场）
{exec_summary}

---

## 地缘政治风险评估
{geo_report}

---

## 宏观与市场方向简报
{econ_report}

---

## 原始新闻信号（按威胁等级排序）
{raw_signals}

---
*Generated by `intelligence_report_cn.py` · Model: {OLLAMA_MODEL} · Ollama local inference*
"""

    # 4) 输出
    print(f"\n{'='*64}")
    print(report)
    print(f"{'='*64}")

    if not args.no_file:
        output_dir = args.output_dir
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, f"intel_report_{file_slug}_CN.md")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\n[SAVED] {filepath}")


if __name__ == "__main__":
    main()