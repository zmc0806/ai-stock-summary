#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股开盘前全球情报 + 小红书自动发布系统
- 拉取全球 RSS + A股专项数据源（含龙头股动态、行业板块轮动、宏观政策）
- 调用 Ollama (qwen3:14b) 生成：
  1) 全球宏观快讯（地缘+市场，聚焦对A股传导路径）
  2) A股开盘前布局简报（大盘/板块/北向/龙头方向）
  3) 小红书结构化文案（JSON格式，供 xhs_mcp_server 自动发布）
- 输出 Markdown 报告 + xhs_payload.json（可直接被发布脚本消费）

用法：
    python intelligence_report_ashare.py
    python intelligence_report_ashare.py --model qwen3:8b
    python intelligence_report_ashare.py --no-file
    python intelligence_report_ashare.py --output-dir ./my_reports
"""

import re
import os
import sys
import json
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import requests

# ─── 配置 ────────────────────────────────────────────────────────────────

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL    = os.environ.get("OLLAMA_MODEL", "qwen3:14b")

FETCH_TIMEOUT        = 12
MAX_CONCURRENT_FEEDS = 20
ITEMS_PER_FEED       = 6
MAX_HEADLINES_GEO    = 20
MAX_HEADLINES_ECON   = 25
MAX_HEADLINES_ASHARE = 30
OUTPUT_DIR           = "reports"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# ─── 时间工具 ─────────────────────────────────────────────────────────────
# 脚本在美股收盘后运行（美东时间16:00-23:00，对应北京时间次日05:00-12:00）
# 对用户来说：美股是"昨晚"，A股是"今天"即将开盘

def get_time_context() -> dict:
    """
    返回准确的时间语境，供Prompt使用。
    例：现在UTC 2026-03-05 21:00 → 北京 2026-03-06 05:00
    → 美股收盘日 = 3月5日（昨晚），A股开盘日 = 3月6日（今天）
    """
    utc_now = datetime.now(timezone.utc)
    bj_now  = utc_now + timedelta(hours=8)

    # A股开盘日就是北京今天
    ashare_date   = bj_now.strftime("%m月%d日")
    ashare_weekday = ["周一","周二","周三","周四","周五","周六","周日"][bj_now.weekday()]

    # 美股收盘日 = 北京昨天（因为美股收盘时北京已是次日凌晨）
    us_close_date = (bj_now - timedelta(days=1)).strftime("%m月%d日")

    return {
        "bj_datetime":    bj_now.strftime("%Y-%m-%d %H:%M"),
        "ashare_date":    ashare_date,
        "ashare_weekday": ashare_weekday,
        "us_close_date":  us_close_date,
        "file_slug":      bj_now.strftime("%Y%m%d_%H%M"),
        "timestamp":      bj_now.strftime("%Y-%m-%d %H:%M CST"),
    }

# ─── Feed 定义 ───────────────────────────────────────────────────────────
# 核心思路：美股收盘后运行，需要的信号是：
# 1. 美股三大指数收盘表现 + 行业板块涨跌
# 2. 影响A股的大宗商品（原油/黄金/铜）
# 3. 中国政策/监管动态（当天盘后）
# 4. 港股收盘（先行指标）
# 5. 影响A股的重大地缘/宏观新闻

# ── 美股收盘 + 全球金融 ──
FINANCE_FEEDS: dict[str, list[dict]] = {
    "美股市场": [
        {"name": "CNBC Markets",     "url": "https://www.cnbc.com/id/20910258/device/rss/rss.html"},
        {"name": "CNBC Top News",    "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html"},
        {"name": "MarketWatch",      "url": "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines"},
        {"name": "Yahoo Finance",    "url": "https://finance.yahoo.com/rss/topstories"},
        {"name": "Seeking Alpha",    "url": "https://seekingalpha.com/market_currents.xml"},
    ],
    "大宗商品": [
        {"name": "Oil Price",        "url": "https://oilprice.com/rss/main"},
        {"name": "Kitco Gold",       "url": "https://www.kitco.com/rss/kitco-news-gold.xml"},
    ],
    "宏观/债券": [
        {"name": "Financial Times",  "url": "https://www.ft.com/rss/home"},
        {"name": "WSJ Markets",      "url": "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain"},
        {"name": "Federal Reserve",  "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
    ],
    "科技板块": [
        {"name": "TechCrunch",       "url": "https://techcrunch.com/feed/"},
        {"name": "The Verge",        "url": "https://www.theverge.com/rss/index.xml"},
    ],
}

# ── 地缘政治（只保留影响A股的核心源）──
GEO_FEEDS: dict[str, list[dict]] = {
    "全球政治": [
        {"name": "BBC World",        "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
        {"name": "Al Jazeera",       "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    ],
    "中东/能源": [
        {"name": "BBC Middle East",  "url": "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml"},
    ],
    "贸易/关税": [
        {"name": "Financial Times",  "url": "https://www.ft.com/rss/home/uk"},
        {"name": "WSJ US",           "url": "https://feeds.content.dowjones.io/public/rss/RSSUSnews"},
    ],
}

# ── A股专项：中国政策 + 港股 + 行业动态 ──
ASHARE_FEEDS: dict[str, list[dict]] = {
    "中国政策/监管": [
        {"name": "SCMP China",       "url": "https://www.scmp.com/rss/4/feed"},
        {"name": "China Daily",      "url": "http://www.chinadaily.com.cn/rss/china_rss.xml"},
        {"name": "Xinhua Finance",   "url": "http://www.xinhuanet.com/english/rss/financerss.xml"},
        {"name": "People's Daily",   "url": "http://en.people.cn/rss/90001.xml"},
    ],
    "港股/亚太": [
        {"name": "SCMP HK Business", "url": "https://www.scmp.com/rss/5/feed"},
        {"name": "Nikkei Asia",      "url": "https://asia.nikkei.com/rss/feed/nar"},
        {"name": "CNA Business",     "url": "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=6936"},
    ],
    "半导体/科技": [
        {"name": "The Register",     "url": "https://www.theregister.com/headlines.atom"},
        {"name": "EE Times",         "url": "https://www.eetimes.com/feed/"},
    ],
    "新能源/大宗": [
        {"name": "Oil Price China",  "url": "https://oilprice.com/rss/main"},
        {"name": "Reuters Energy",   "url": "https://feeds.reuters.com/reuters/businessNews"},
    ],
    "贸易/关税": [
        {"name": "WTO News",         "url": "https://www.wto.org/english/news_e/news_e.rss"},
        {"name": "SCMP Economy",     "url": "https://www.scmp.com/rss/11/feed"},
    ],
}

# ─── 威胁分级 ─────────────────────────────────────────────────────────────

THREAT_KEYWORDS: dict[str, list[str]] = {
    "CRITICAL": [
        "nuclear", "invasion", "war declared", "coup", "market crash", "default",
        "bank run", "systemic risk", "circuit breaker", "collapse",
        # A股专项
        "pboc emergency", "rate cut emergency", "trading halt", "systemic",
        "china crash", "csi 300 plunge",
    ],
    "HIGH": [
        "war", "airstrike", "missile", "sanctions", "embargo", "recession",
        "rate hike", "fed rate", "tariff", "trade war", "military operation",
        # A股专项
        "pboc", "csrc", "ndrc", "china stimulus", "china rate",
        "northbound", "southbound", "hang seng", "shanghai composite",
        "china gdp", "china cpi", "china pmi", "rrr cut", "lpr",
        "property", "evergrande", "country garden", "tech crackdown",
        "antitrust china", "delisting", "vie structure",
    ],
    "MEDIUM": [
        "protest", "military exercise", "inflation", "interest rate",
        "trade agreement", "election", "layoffs",
        # A股专项
        "china exports", "china imports", "trade surplus", "china manufacturing",
        "semiconductor", "chips act", "supply chain china",
        "a-share", "shenzhen", "shanghai stock", "hong kong stock",
        "tencent", "alibaba", "baidu", "huawei", "xiaomi", "byd",
        "new energy", "ev china", "solar china", "lithium",
        "china property", "developers", "mortgage",
        # 龙头股动态关键词
        "catl", "contemporary amperex", "moutai", "kweichow",
        "ping an insurance", "china construction bank", "icbc",
        "cnooc", "sinopec", "petrochina", "china mobile",
        "longi", "sungrow", "ganfeng", "tianqi",
        "earnings beat", "earnings miss", "profit warning",
        "buyback china", "dividend china", "ipo china",
    ],
    "LOW": [
        "summit", "agreement", "cooperation", "partnership",
        "china initiative", "belt road", "rcep",
    ],
}

THREAT_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
THREAT_CN    = {"CRITICAL": "极高", "HIGH": "高", "MEDIUM": "中", "LOW": "低"}


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

# ─── RSS 解析 ─────────────────────────────────────────────────────────────

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
        xml_text = re.sub(r"^[^\x3c]*", "", xml_text, count=1)
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return items

    tag = root.tag.lower()
    if "feed" in tag:
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

# ─── 实时市场数据（东方财富免费API）─────────────────────────────────────────

def fetch_market_snapshot() -> dict:
    """
    抓取实时市场快照：
    - A股昨日成交额（沪深两市合计）
    - 港股恒生指数收盘
    - 美元/人民币汇率
    返回格式化字符串供Prompt使用，失败时返回空dict不影响主流程。
    """
    result = {}

    # ── A股成交额（东方财富，无需登录）──
    try:
        # 上证指数行情（含成交额）
        sh_url = ("https://push2.eastmoney.com/api/qt/stock/get"
                  "?secid=1.000001&fields=f43,f44,f45,f47,f170")
        sz_url = ("https://push2.eastmoney.com/api/qt/stock/get"
                  "?secid=0.399001&fields=f43,f44,f45,f47,f170")
        headers = {**HEADERS, "Referer": "https://www.eastmoney.com/"}

        sh_resp = requests.get(sh_url, headers=headers, timeout=8).json()
        sz_resp = requests.get(sz_url, headers=headers, timeout=8).json()

        sh_data = sh_resp.get("data", {})
        sz_data = sz_resp.get("data", {})

        # f47 = 成交额（单位：元）
        sh_vol = sh_data.get("f47", 0)
        sz_vol = sz_data.get("f47", 0)
        # f43 = 最新价（×100）, f170 = 涨跌幅（×100）
        sh_price  = sh_data.get("f43", 0) / 100
        sh_chg    = sh_data.get("f170", 0) / 100
        sz_price  = sz_data.get("f43", 0) / 100
        sz_chg    = sz_data.get("f170", 0) / 100

        if sh_vol and sz_vol:
            # 东方财富f47返回值单位验证：
            # 正常A股日成交在5000亿-2万亿之间，即500_000_000_000 ~ 2_000_000_000_000 元
            # 如果返回值本身就是很小的数字（<1000），说明API已经返回了亿元单位
            raw_total = sh_vol + sz_vol
            if raw_total > 1_000_000_000:       # 明显是元为单位
                total_vol_yi = raw_total / 1e8
            elif raw_total > 1_000:              # 已经是亿元单位
                total_vol_yi = raw_total
            else:                                # 数据异常，按亿元处理
                total_vol_yi = raw_total
            sh_sign  = "+" if sh_chg >= 0 else ""
            sz_sign  = "+" if sz_chg >= 0 else ""
            result["a_share"] = (
                f"上证{sh_price:.0f}点（{sh_sign}{sh_chg:.2f}%），"
                f"深证{sz_price:.0f}点（{sz_sign}{sz_chg:.2f}%），"
                f"沪深两市合计成交{total_vol_yi:.0f}亿元"
            )
            result["total_vol_yi"] = total_vol_yi
            result["sh_price"] = sh_price
            result["sz_price"] = sz_price
            result["sh_chg"]   = sh_chg
            result["sz_chg"]   = sz_chg
            # 量能判断
            if total_vol_yi < 7000:
                result["volume_comment"] = f"成交{total_vol_yi:.0f}亿，缩量，场外资金观望"
            elif total_vol_yi > 12000:
                result["volume_comment"] = f"成交{total_vol_yi:.0f}亿，明显放量，情绪高涨"
            else:
                result["volume_comment"] = f"成交{total_vol_yi:.0f}亿，量能适中"
    except Exception as e:
        print(f"  [WARN] A股数据获取失败：{e}")

    # ── 港股恒生指数 ──
    try:
        hsi_url = ("https://push2.eastmoney.com/api/qt/stock/get"
                   "?secid=100.HSI&fields=f43,f44,f45,f47,f170")
        hsi_resp = requests.get(hsi_url, headers=headers, timeout=8).json()
        hsi_data = hsi_resp.get("data", {})
        hsi_price = hsi_data.get("f43", 0) / 100
        hsi_chg   = hsi_data.get("f170", 0) / 100
        if hsi_price:
            sign = "+" if hsi_chg >= 0 else ""
            direction = "上涨" if hsi_chg >= 0 else "下跌"
            result["hsi"] = (
                f"恒生指数收{hsi_price:.0f}点，{direction}{abs(hsi_chg):.2f}%"
            )
    except Exception as e:
        print(f"  [WARN] 港股数据获取失败：{e}")

    # ── 美元/人民币即期汇率 ──
    try:
        fx_url = ("https://push2.eastmoney.com/api/qt/stock/get"
                  "?secid=120.USDCNH&fields=f43,f170")
        fx_resp = requests.get(fx_url, headers=headers, timeout=8).json()
        fx_data  = fx_resp.get("data", {})
        usdcnh   = fx_data.get("f43", 0) / 10000  # 离岸人民币
        fx_chg   = fx_data.get("f170", 0) / 100
        if usdcnh:
            sign = "+" if fx_chg >= 0 else ""
            result["fx"] = f"美元/离岸人民币 {usdcnh:.4f}（{sign}{fx_chg:.2f}%）"
    except Exception as e:
        print(f"  [WARN] 汇率数据获取失败：{e}")

    return result


def format_market_snapshot(snap: dict) -> str:
    """把市场快照格式化为Prompt可用的文本段落"""
    if not snap:
        return ""
    lines = ["【实时市场数据】"]
    if "a_share" in snap:
        lines.append(f"A股昨日收盘：{snap['a_share']}")
    if "volume_comment" in snap:
        lines.append(f"量能：{snap['volume_comment']}")
    if "hsi" in snap:
        lines.append(f"港股今日收盘：{snap['hsi']}")
    if "fx" in snap:
        lines.append(f"汇率：{snap['fx']}")
    return "\n".join(lines)


def build_content_prefix(snap: dict, tc: dict) -> str:
    """
    硬拼文案开头两段，不经过模型，确保数字100%准确。
    第一段：固定实验说明
    第二段：实时数据构成的事实陈述
    """
    # 固定开头
    lines = [
        "在做一个AI自动化实验，每天美股收盘后自动抓取外盘新闻、生成分析、自动发到小红书。",
        "内容仅供参考，不构成投资建议。",
        "早上好大家",
    ]

    # 数据段：只用真实数字拼，没有的跳过
    data_parts = []

    if "a_share" in snap:
        sh_chg = snap.get("sh_chg", 0)
        sz_chg = snap.get("sz_chg", 0)
        vol    = snap.get("total_vol_yi", 0)
        # 大盘方向
        if sh_chg > 0.5:
            direction = "昨日A股收涨"
        elif sh_chg < -0.5:
            direction = "昨日A股收跌"
        else:
            direction = "昨日A股小幅震荡"
        data_parts.append(
            f"{direction}，上证{snap.get('sh_price',0):.0f}点（{'+' if sh_chg>=0 else ''}{sh_chg:.2f}%），"
            f"深证{snap.get('sz_price',0):.0f}点（{'+' if sz_chg>=0 else ''}{sz_chg:.2f}%）"
        )
        if vol:
            data_parts.append(f"两市成交{vol:.0f}亿")

    if "hsi" in snap:
        data_parts.append(f"今天港股{snap['hsi']}")

    if data_parts:
        lines.append("，".join(data_parts) + "。")

    return "\n".join(lines)

# ─── 去重/排序 ────────────────────────────────────────────────────────────

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

# ─── Ollama ────────────────────────────────────────────────────────────────

def strip_thinking(text: str) -> str:
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<think>[\s\S]*", "", text, flags=re.IGNORECASE)
    return text.strip()


def call_ollama(user_prompt: str, system_prompt: str,
                label: str = "", max_tokens: int = 1800) -> str:
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
            "num_predict": max_tokens,
        },
    }
    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json=payload,
            timeout=300,
        )
        resp.raise_for_status()
        return strip_thinking(resp.json().get("response", ""))
    except requests.exceptions.ConnectionError:
        print(f"\n[ERROR] 无法连接 Ollama：{OLLAMA_BASE_URL}")
        print("  请先启动：ollama serve")
        sys.exit(1)
    except Exception as e:
        return f"[LLM ERROR: {e}]"

# ─── Prompts ──────────────────────────────────────────────────────────────

# ── 1. 美股收盘 + 外部信号快读 ──
SYSTEM_MACRO = """\
你是一名服务A股投资者的外盘分析师，专门在美股收盘后提炼影响次日A股的关键信号。

【时间语境】
- 现在是北京时间{bj_datetime}，美股刚收盘
- 美股收盘 = "昨晚"（对A股投资者而言）
- A股次日开盘 = "今天"

【角色约束】
- 只分析标题中明确呈现的信息
- 每条信号必须点明对A股的传导路径
- 有具体数字的用数字（如"纳指跌1.2%"比"纳指下跌"好）
- 全文中文，保留必要英文缩写

【输出格式】

## 昨晚外盘总结
[一句话，美股三大指数整体表现，涨跌方向+核心原因，不超过30字]

## 关键信号（最多5条，每条一行）
- [信号]：[对A股影响，点明板块]
（例："昨晚原油涨2%：利好石油石化板块，中石油/中石化今天可关注"）
（例："纳指跌1.5%，半导体ETF领跌：A股半导体今天承压，北向可能流出"）

## 大宗商品收盘
- 原油：[价格方向] [影响A股板块]
- 黄金：[价格方向] [影响A股板块]
（无明确信号则省略对应行）\
"""

USER_MACRO = """\
时间背景：北京时间{bj_datetime}，美股{us_close_date}收盘后。

以下是今日外盘新闻标题：
{headlines}

请生成外盘收盘快读，重点提炼影响{ashare_date}A股开盘的关键信号：\
"""

# ── 2. A股开盘前布局（核心分析） ──
SYSTEM_ASHARE = """\
你是一名专注A股的资深操盘手，每天美股收盘后为粉丝写次日A股开盘前布局思路。

【时间语境】
- 美股{us_close_date}刚收盘（="昨晚"）
- A股{ashare_date}（{ashare_weekday}）即将开盘（="今天"）
- 时间词用法：昨晚美股、昨日A股、今天开盘、今天可以关注

【分析框架】
1. 今天大盘方向（一句话判断：高开/低开/平开，单边/震荡）
2. 港股收盘表现（先行指标，如有信号：港股跌了A股大概率跟跌）
3. 昨日A股收盘情况（从新闻提取：哪些板块涨跌、量能情况）
4. 今天值得关注的板块（有外部催化的，给出逻辑）
5. 昨日强势板块今天的延续性判断
6. 汇率/北向（如有明确信号才写，没有就省略）

【约束】
- 语气像有经验的老股民在和朋友说话，不是写分析报告
- 用具体数字（如"缩量8000亿""4000家下跌"），从新闻提取，没有就不用
- 板块判断要有依据，但用口语表达（"因为昨晚油价涨了"而不是"受国际油价上涨影响"）
- 不推荐具体个股买卖，只说方向和逻辑
- 禁止预测任何指数点位数字（如"4080""3200"等），这类预测准确率低不要写
- 全文中文

【输出格式 — 直接输出分析内容，不要标题头部，不要markdown表格】
今天大盘：[一句话判断]

[板块1分析，2-3句]

[板块2分析，2-3句]

[昨日强势板块延续性，2句]

[风险提示，1句]\
"""

USER_ASHARE = """\
时间背景：北京时间{bj_datetime}
- 美股{us_close_date}收盘 = 昨晚
- A股{ashare_date}（{ashare_weekday}）开盘 = 今天

【昨晚美股/外盘信号】
{macro_headlines}

【中国/港股/行业相关新闻】
{ashare_headlines}

【实时市场数据（权威数据，优先使用）】
{market_snapshot}

【外盘分析摘要（参考）】
{macro_report_excerpt}

规则：
- 实时市场数据中的数字（成交额、指数、涨跌幅、汇率）直接引用，不要改动
- 时间词必须准确：昨晚/昨日=美股和A股昨天，今天港股收盘=今天
- 有具体数字就用，没有就不编造

请生成A股{ashare_date}开盘前布局思路：\
"""

# ── 3. 小红书文案（对标参考博主风格）──
SYSTEM_XIAOHONGSHU = """\
你是一个在小红书发A股财经内容的博主，正在做一个"AI自动化盘前提醒"的实验项目。
每天美股收盘后，脚本自动抓取外盘新闻，用AI分析后自动发到小红书。

【参考风格】（严格模仿语气和结构，这是目标效果）
---
早上好大家
昨晚美股、黄金调整，亚洲股市早盘集体大幅度低开。
今天a股开盘又要承压。

板块上昨日石油、航运领涨，其它科技板块大幅度调整。

板块上昨日算力尾盘抢筹，看看今天能不能有资金回流

这次外围动荡对股市还是影响比较大，已经连续两日4000家下跌。
---

【开头固定格式】
第一段必须是这样（三行，每行换行）：
"在做一个AI自动化实验，每天美股收盘后自动抓取外盘新闻、生成分析、自动发到小红书。
内容仅供参考，不构成投资建议。
早上好大家"

然后紧接外盘情况1-2句，自然过渡，不要额外空行。

【正文规则】
- 中间2-3段，段落间空行，每段1-3句
- 外盘整体 + 港股收盘情况（如有）→ 今天A股大方向判断
- 具体板块：有新闻依据才写，口语解释逻辑
- 如果新闻提到尾盘抢筹/板块异动，结合具体信号说延续性
- 汇率/北向：如有明确信号顺带一句，没有就不提
- 语气朴实，像老朋友在说话

【禁止】
- 任何指数点位数字（4080、3200等）
- "可以看到的话点个赞"等引流话术
- 结尾不加提问句，以最后一个判断自然收尾
- "仅供参考""风险自担"（开头已说，后面不重复）
- 没有新闻依据的内容不要编造
- emoji最多1-2个

字数：150-220字

话题标签tags：7-9个，必含：财经、A股、股票、今日操作、小红书新人报道，加上提到的板块名

【严格输出 — 纯JSON】
{{
  "title": "（含日期+核心信号，≤20字，最多1个emoji）",
  "content": "（正文，段落间\\n\\n分隔）",
  "tags": ["财经", "A股", "股票", "今日操作", "板块1", "板块2", "小红书新人报道"]
}}\
"""

USER_XIAOHONGSHU = """\
时间背景：
- 昨晚 = 美股{us_close_date}收盘
- 今天 = A股{ashare_date}（{ashare_weekday}）开盘

【A股开盘前布局分析】
{ashare_report}

【外盘关键信号】
{macro_summary}

写作要求：
1. 开头第一段固定三行："在做一个AI自动化实验，每天美股收盘后自动抓取外盘新闻、生成分析、自动发到小红书。\n内容仅供参考，不构成投资建议。\n早上好大家"，然后紧接外盘情况
2. 时间词准确：用"昨晚""昨日""今天"
3. 如有港股收盘信号，提一句作为先行参考
4. 只写有新闻依据的板块和信号，没有就不写
5. 不写任何指数点位数字
6. 不写引流话术，结尾不加提问句

只输出JSON：\
"""

# ─── 辅助函数 ─────────────────────────────────────────────────────────────

def build_headlines_block(items: list[NewsItem]) -> str:
    return "\n".join(
        f"[{i.threat}/{THREAT_CN.get(i.threat,'低')}] ({i.source}/{i.category}) {i.title}"
        for i in items
    )


def build_raw_table(items: list[NewsItem]) -> str:
    rows = ["| 等级 | 来源 | 类别 | 标题 |",
            "|------|------|------|------|"]
    for i in sort_by_threat(deduplicate(items)):
        rows.append(
            f"| {i.threat}/{THREAT_CN.get(i.threat,'低')} | {i.source} | {i.category} | "
            f"{i.title.replace('|','/')} |"
        )
    return "\n".join(rows)

# ─── 分析流水线 ───────────────────────────────────────────────────────────

def analyze_macro(geo_items: list[NewsItem], econ_items: list[NewsItem],
                  tc: dict) -> str:
    combined = sort_by_threat(deduplicate(geo_items + econ_items))
    top = combined[:MAX_HEADLINES_ECON]
    system = SYSTEM_MACRO.format(**tc)
    user   = USER_MACRO.format(headlines=build_headlines_block(top), **tc)
    return call_ollama(user, system, label="外盘收盘快读", max_tokens=800)


def analyze_ashare(ashare_items: list[NewsItem], econ_items: list[NewsItem],
                   macro_report: str, tc: dict, market_snap: str = "") -> str:
    macro_top  = sort_by_threat(deduplicate(econ_items))[:15]
    ashare_top = sort_by_threat(deduplicate(ashare_items))[:MAX_HEADLINES_ASHARE]
    system = SYSTEM_ASHARE.format(**tc)
    user   = USER_ASHARE.format(
        macro_headlines=build_headlines_block(macro_top),
        ashare_headlines=build_headlines_block(ashare_top),
        macro_report_excerpt=macro_report[:600],
        market_snapshot=market_snap or "（数据获取失败，请依据新闻判断）",
        **tc,
    )
    return call_ollama(user, system, label="A股布局分析", max_tokens=1200)


def generate_xiaohongshu(ashare_report: str, macro_report: str,
                         tc: dict, market_snap: dict = None) -> dict:
    macro_summary = "\n".join(macro_report.split("\n")[:6])

    # 更新Prompt：告诉模型开头已经写好了，只写后面的分析段
    user = USER_XIAOHONGSHU.format(
        ashare_report=ashare_report[:900],
        macro_summary=macro_summary,
        **tc,
    )
    # 在user prompt里注明开头已硬拼，不要重复写
    user = ("注意：正文开头（实验说明+早上好大家+昨日数据）已经由程序自动生成，"
            "你只需要生成content字段里【开头之后】的分析段落（从外盘情况开始），"
            "不要再写'在做一个AI自动化实验'这段。\n\n") + user

    raw = call_ollama(user, SYSTEM_XIAOHONGSHU, label="小红书文案", max_tokens=600)

    try:
        clean = re.sub(r"^```(?:json)?\s*|```\s*$", "", raw.strip(),
                       flags=re.MULTILINE).strip()
        data = json.loads(clean)
        data["raw"] = raw
        data.setdefault("title", f"{tc['ashare_date']}A股开盘前提醒")
        data.setdefault("tags", ["财经", "A股", "股票", "今日操作", "小红书新人报道"])

        # 硬拼前缀 + 模型生成的分析段
        prefix = build_content_prefix(market_snap or {}, tc)
        body   = data.get("content", ashare_report[:280])
        # 去掉模型可能重复写的开头
        for drop in ["在做一个AI自动化实验", "内容仅供参考，不构成投资建议", "早上好大家"]:
            if body.startswith(drop):
                body = "\n".join(body.split("\n")[3:]).lstrip()
        data["content"] = prefix + "\n\n" + body
        return data

    except (json.JSONDecodeError, ValueError):
        print("  [WARN] JSON解析失败，降级模式")
        lines = [l.strip() for l in raw.split("\n") if l.strip()]
        prefix = build_content_prefix(market_snap or {}, tc)
        return {
            "title":   lines[0][:20] if lines else f"{tc['ashare_date']}A股布局",
            "content": prefix + "\n\n" + ("\n\n".join(lines[1:]) if len(lines) > 1 else raw[:200]),
            "tags":    ["财经", "A股", "股票", "今日操作", "小红书新人报道"],
            "raw":     raw,
        }

# ─── 主程序 ────────────────────────────────────────────────────────────────

def main() -> None:
    global OLLAMA_MODEL

    parser = argparse.ArgumentParser(description="A股开盘前情报 + 小红书文案生成器")
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    parser.add_argument("--no-file",    action="store_true")
    parser.add_argument("--model",      default=OLLAMA_MODEL)
    args = parser.parse_args()
    OLLAMA_MODEL = args.model

    tc = get_time_context()  # 时间上下文

    print(f"\n{'='*64}")
    print("  A股开盘前情报系统 + 小红书文案生成器")
    print(f"  Model    : {OLLAMA_MODEL}")
    print(f"  北京时间  : {tc['bj_datetime']}")
    print(f"  昨晚美股  : {tc['us_close_date']}")
    print(f"  今天A股   : {tc['ashare_date']} {tc['ashare_weekday']}")
    print(f"{'='*64}\n")

    # ── 1. 抓取 ──
    print("[1/5] 抓取外盘金融新闻...", flush=True)
    econ_items = deduplicate(fetch_all_feeds(FINANCE_FEEDS))
    print(f"      去重后：{len(econ_items)} 条")

    print("[2/5] 抓取地缘/宏观新闻...", flush=True)
    geo_items = deduplicate(fetch_all_feeds(GEO_FEEDS))
    print(f"      去重后：{len(geo_items)} 条")

    print("[3/5] 抓取A股专项新闻...", flush=True)
    ashare_items = deduplicate(fetch_all_feeds(ASHARE_FEEDS))
    print(f"      去重后：{len(ashare_items)} 条")

    print("[+]   抓取实时市场数据（A股量能/港股/汇率）...", flush=True)
    market_snap = fetch_market_snapshot()
    snap_str    = format_market_snapshot(market_snap)
    if market_snap:
        for k, v in market_snap.items():
            if k != "volume_comment":
                print(f"      {v}")
    else:
        print("      （实时数据获取失败，继续用新闻分析）")

    if not econ_items and not ashare_items:
        print("\n[ERROR] 未抓取到任何新闻，请检查网络。")
        sys.exit(1)

    # ── 2. LLM 分析 ──
    print("[4/5] 外盘分析 + A股布局...", flush=True)
    macro_report  = analyze_macro(geo_items, econ_items, tc)
    ashare_report = analyze_ashare(ashare_items, econ_items, macro_report, tc, snap_str)

    print("[5/5] 生成小红书文案...", flush=True)
    xhs_data = generate_xiaohongshu(ashare_report, macro_report, tc, market_snap)

    # ── 3. 组装报告 ──
    all_items = geo_items + econ_items + ashare_items
    counts = {lvl: sum(1 for i in all_items if i.threat == lvl)
              for lvl in ("CRITICAL", "HIGH", "MEDIUM", "LOW")}
    xhs_tags_str = " ".join(f"#{t}" for t in xhs_data.get("tags", []))

    report = f"""# A股开盘前情报 · {tc['ashare_date']} {tc['ashare_weekday']}
**生成时间：** {tc['timestamp']}  
**模型：** {OLLAMA_MODEL}  
**昨晚美股：** {tc['us_close_date']} · **今天A股：** {tc['ashare_date']}

---

## 📊 实时市场数据
{snap_str if snap_str else "（数据获取失败）"}

---

## 🇨🇳 A股{tc['ashare_date']}开盘前布局
{ashare_report}

---

## 🌍 昨晚外盘快读
{macro_report}

---

## 📱 小红书文案

**标题：** {xhs_data.get('title', '')}

{xhs_data.get('content', '')}

{xhs_tags_str}

---

## 原始新闻
{build_raw_table(all_items)}

---
*Model: {OLLAMA_MODEL} · {tc['timestamp']}*
"""

    xhs_payload = {
        "generated_at": tc["timestamp"],
        "ashare_date":  tc["ashare_date"],
        "title":        xhs_data.get("title", ""),
        "content":      xhs_data.get("content", ""),
        "tags":         xhs_data.get("tags", []),
        "image_paths":  [],
    }

    print(f"\n{'='*64}")
    print(report)
    print(f"{'='*64}")

    if not args.no_file:
        out = args.output_dir
        os.makedirs(out, exist_ok=True)
        slug = tc["file_slug"]

        md_path      = os.path.join(out, f"ashare_{slug}.md")
        xhs_path     = os.path.join(out, f"xhs_payload_{slug}.json")
        xhs_latest   = os.path.join(out, "xhs_payload_latest.json")

        with open(md_path, "w", encoding="utf-8") as f:
            f.write(report)
        for p in [xhs_path, xhs_latest]:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(xhs_payload, f, ensure_ascii=False, indent=2)

        print(f"\n[SAVED] 报告      → {md_path}")
        print(f"[SAVED] XHS JSON  → {xhs_latest}")
        print(f"\n[NEXT]  python publish_to_xhs.py --auto-cover")


if __name__ == "__main__":
    main()
