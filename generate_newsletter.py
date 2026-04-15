"""
Daily Financial Newsletter Generator
Run manually: python generate_newsletter.py
Scheduled:    Windows Task Scheduler fires this at 8:30 AM on weekdays

Requires:
  pip install anthropic feedparser python-dateutil jinja2
  ANTHROPIC_API_KEY set as an environment variable
"""

import json
import re
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import feedparser
import jinja2
from markupsafe import Markup
from dateutil import parser as dateparser

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

# ── Config ─────────────────────────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).parent
OUTPUT_DIR  = PROJECT_DIR / "newsletters"
TEMPLATE    = "newsletter_template.html"

ANTHROPIC_MODEL       = "claude-haiku-4-5-20251001"
MAX_ARTICLES_PER_FEED = 8
MAX_ARTICLE_AGE_HOURS = 36   # ignore anything older than this

# ── RSS Feeds ──────────────────────────────────────────────────────────────────

RSS_FEEDS = [
    # Federal Reserve
    {
        "name": "Federal Reserve",
        "url":  "https://www.federalreserve.gov/feeds/press_all.xml",
        "category": "fed",
    },
    # Macro / economic data
    {
        "name": "Reuters Business",
        "url":  "https://feeds.reuters.com/reuters/businessNews",
        "category": "macro",
    },
    {
        "name": "AP Business",
        "url":  "https://feeds.apnews.com/rss/business",
        "category": "macro",
    },
    {
        "name": "MarketWatch",
        "url":  "https://feeds.marketwatch.com/marketwatch/topstories/",
        "category": "macro",
    },
    {
        "name": "CNBC",
        "url":  "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "category": "macro",
    },
    # Geopolitics / policy
    {
        "name": "Reuters World",
        "url":  "https://feeds.reuters.com/reuters/worldNews",
        "category": "geopolitics",
    },
    {
        "name": "AP Politics",
        "url":  "https://feeds.apnews.com/rss/politics",
        "category": "geopolitics",
    },
    # Earnings / corporate
    {
        "name": "Yahoo Finance",
        "url":  "https://finance.yahoo.com/news/rssindex",
        "category": "earnings",
    },
    {
        "name": "SEC EDGAR",
        "url":  (
            "https://efts.sec.gov/LATEST/search-index"
            "?q=%22earnings%22"
            "&dateRange=custom&startdt={today}&enddt={today}"
            "&_source=feed"
        ),
        "category": "earnings",
    },
]

# ── Section headers Claude must produce ────────────────────────────────────────

SECTION_HEADERS = [
    "EXECUTIVE SUMMARY",
    "FEDERAL RESERVE & MONETARY POLICY",
    "LIQUIDITY & MONEY SUPPLY",
    "WHITE HOUSE & FISCAL POLICY",
    "GEOPOLITICS & WAR RISK",
    "EARNINGS & GUIDANCE",
    "ECONOMIC DATA RELEASES",
    "MACRO TRENDS & OTHER",
]

# ── Fetch ──────────────────────────────────────────────────────────────────────

def fetch_feed(feed_def: dict) -> list:
    now = datetime.now(timezone.utc)
    parsed = feedparser.parse(
        feed_def["url"],
        agent="Mozilla/5.0 (compatible; DailyBriefingBot/1.0)"
    )
    articles = []
    for entry in parsed.entries:
        # Parse publication date
        pub = None
        for attr in ("published", "updated", "created"):
            raw = getattr(entry, attr, None)
            if raw:
                try:
                    pub = dateparser.parse(raw)
                    if pub and pub.tzinfo is None:
                        pub = pub.replace(tzinfo=timezone.utc)
                    break
                except Exception:
                    pass

        # Skip stale articles
        if pub and (now - pub).total_seconds() > MAX_ARTICLE_AGE_HOURS * 3600:
            continue

        # Extract summary
        summary = ""
        if hasattr(entry, "summary"):
            summary = entry.summary
        elif hasattr(entry, "content") and entry.content:
            summary = entry.content[0].value
        # Strip HTML tags from summary
        summary = re.sub(r"<[^>]+>", " ", summary).strip()
        summary = re.sub(r"\s+", " ", summary)[:600]

        articles.append({
            "title":     getattr(entry, "title", ""),
            "summary":   summary,
            "link":      getattr(entry, "link", ""),
            "published": pub.isoformat() if pub else "unknown",
            "source":    feed_def["name"],
            "category":  feed_def["category"],
        })

    articles.sort(key=lambda a: a["published"], reverse=True)
    return articles[:MAX_ARTICLES_PER_FEED]


def fetch_all_feeds() -> dict:
    today_str = datetime.now().strftime("%Y-%m-%d")
    categorized = {"fed": [], "macro": [], "geopolitics": [], "earnings": []}

    for feed_def in RSS_FEEDS:
        url = feed_def["url"].replace("{today}", today_str)
        fd  = {**feed_def, "url": url}
        print(f"  Fetching: {fd['name']}...", end=" ", flush=True)
        try:
            articles = fetch_feed(fd)
            categorized[fd["category"]].extend(articles)
            print(f"{len(articles)} articles")
        except Exception as exc:
            print(f"FAILED ({exc})")

    return categorized

# ── Market data (SPY, QQQ, Dow) ───────────────────────────────────────────────

MARKET_TICKERS = [
    {"symbol": "SPY",  "name": "S&P 500 ETF",  "key": "SPY",  "prefix": "$"},
    {"symbol": "QQQ",  "name": "Nasdaq 100 ETF","key": "QQQ",  "prefix": "$"},
    {"symbol": "^DJI", "name": "Dow Jones",     "key": "DJI",  "prefix": ""},
]

def fetch_market_data() -> dict:
    if not YFINANCE_AVAILABLE:
        print("  yfinance not installed — skipping market charts")
        return {}

    result = {}
    for t in MARKET_TICKERS:
        try:
            ticker = yf.Ticker(t["symbol"])
            hist = ticker.history(period="1d", interval="5m")
            if hist.empty:
                # Market closed or weekend — use last available trading day
                hist = ticker.history(period="5d", interval="5m")
                if not hist.empty:
                    last_date = hist.index[-1].date()
                    hist = hist[hist.index.map(lambda x: x.date()) == last_date]

            if hist.empty:
                print(f"  {t['key']}: no data")
                continue

            current    = float(hist["Close"].iloc[-1])
            open_price = float(hist["Open"].iloc[0])
            change     = current - open_price
            pct        = (change / open_price) * 100
            prices     = [round(float(p), 2) for p in hist["Close"].tolist()]
            times      = [ts.strftime("%H:%M") for ts in hist.index]

            if t["prefix"]:
                price_fmt  = f"${current:,.2f}"
                change_fmt = f"{'+' if change >= 0 else '-'}${abs(change):.2f}"
            else:
                price_fmt  = f"{current:,.0f}"
                change_fmt = f"{'+' if change >= 0 else ''}{change:,.0f}"

            result[t["key"]] = {
                "name":       t["name"],
                "symbol":     t["key"],
                "price_fmt":  price_fmt,
                "change_fmt": change_fmt,
                "pct_fmt":    f"{'▲' if change >= 0 else '▼'} {abs(pct):.2f}%",
                "direction":  "up" if change >= 0 else "down",
                "prices":     prices,
                "times":      times,
            }
            print(f"  {t['key']}: {price_fmt} ({'+' if change >= 0 else ''}{pct:.2f}%)")
        except Exception as exc:
            print(f"  {t['key']}: FAILED ({exc})")

    return result

# ── Build Claude prompt ────────────────────────────────────────────────────────

def build_prompt_content(categorized: dict) -> str:
    section_labels = {
        "fed":         "FEDERAL RESERVE",
        "macro":       "MACRO / ECONOMIC DATA / POLICY",
        "geopolitics": "GEOPOLITICS / WARS / WHITE HOUSE POLICY",
        "earnings":    "EARNINGS / CORPORATE",
    }
    lines = []
    for cat, label in section_labels.items():
        articles = categorized.get(cat, [])
        lines.append(f"\n=== {label} ===")
        if not articles:
            lines.append("(No articles found in this category)")
        for a in articles:
            lines.append(f"SOURCE: {a['source']}")
            lines.append(f"TITLE: {a['title']}")
            lines.append(f"PUBLISHED: {a['published']}")
            lines.append(f"SUMMARY: {a['summary']}")
            lines.append("")
    return "\n".join(lines)

# ── Call Claude API ────────────────────────────────────────────────────────────

def call_claude_api(raw_content: str) -> str:
    client = anthropic.Anthropic()
    today  = datetime.now().strftime("%A, %B %d, %Y")

    system_prompt = f"""You are a senior financial journalist and market analyst producing a premium daily briefing for sophisticated investors and traders. Today is {today}.

Your job is to synthesize the raw news articles provided into a structured, authoritative newsletter.

FIRST LINE — before the newsletter, output exactly one line in this format:
STATS: Label1=Value1 | Label2=Value2 | Label3=Value3 | ...
Extract up to 6 key market numbers explicitly mentioned in the articles (e.g. interest rates, index levels, yields, oil prices, VIX). Use short labels (max 12 chars). Prefix directional values with ▲ or ▼. If no specific numbers appear in the articles, output: STATS: (none)
Example: STATS: Fed Rate=5.25% | S&P 500=▼0.8% | 10Y Yield=4.62% | Oil=▲$83.20 | VIX=18.4 | DXY=104.2

NEWSLETTER STRUCTURE — use these exact section headers, in this order:
1. EXECUTIVE SUMMARY
2. FEDERAL RESERVE & MONETARY POLICY
3. LIQUIDITY & MONEY SUPPLY
4. WHITE HOUSE & FISCAL POLICY
5. GEOPOLITICS & WAR RISK
6. EARNINGS & GUIDANCE
7. ECONOMIC DATA RELEASES
8. MACRO TRENDS & OTHER

FORMATTING RULES:
- Use bullet points starting with • within each section
- Each bullet must be one or two sentences maximum — dense, no filler words
- After each bullet, include a bracketed source tag: [Reuters] [CNBC] [Fed] [AP] etc.
- State WHY each development matters for markets, not just what happened
- If a category has no relevant news, write a single bullet: • No significant developments.
- Do NOT hallucinate data — only use information from the articles provided
- Do NOT include URLs or links in the newsletter body
- Write time references as "this morning," "late yesterday," "earlier today" relative to {today}
- Tone: authoritative, direct, zero fluff — think Bloomberg Terminal briefing

OUTPUT FORMAT: Return the STATS line first, then the newsletter content using the section headers above. No preamble, no closing remarks."""

    message = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    "Here are today's raw news articles organized by category. "
                    "Synthesize them into the newsletter:\n\n" + raw_content
                ),
            }
        ],
    )

    usage = message.usage
    print(f"  Input tokens:  {usage.input_tokens}")
    print(f"  Output tokens: {usage.output_tokens}")
    cache_read    = getattr(usage, "cache_read_input_tokens", 0)
    cache_created = getattr(usage, "cache_creation_input_tokens", 0)
    if cache_read:
        print(f"  Cache read:    {cache_read} tokens (cheaper re-run)")
    if cache_created:
        print(f"  Cache created: {cache_created} tokens")

    return message.content[0].text

# ── Parse stats line ──────────────────────────────────────────────────────────

def parse_stats(text: str) -> tuple:
    """Strip the STATS: line from Claude output, return (stats_list, cleaned_text)."""
    stats = []
    remaining = []
    for line in text.strip().splitlines():
        if line.startswith("STATS:"):
            content = line[6:].strip()
            if content and content != "(none)":
                for part in content.split("|"):
                    part = part.strip()
                    if "=" in part:
                        label, value = part.split("=", 1)
                        stats.append({"label": label.strip(), "value": value.strip()})
        else:
            remaining.append(line)
    return stats, "\n".join(remaining)

# ── Parse Claude output into sections ─────────────────────────────────────────

def clean_bullet(line: str) -> str:
    """Strip markdown artifacts from a bullet line."""
    # Skip pure separator lines (---, ===, ###, etc.)
    if re.match(r'^[-=*_#]{2,}\s*$', line):
        return ""
    # Strip leading markdown headers
    line = re.sub(r'^#{1,6}\s+', '', line)
    # Strip leading bullet/dash/asterisk markers
    line = re.sub(r'^[•\-\*]\s+', '', line)
    # Strip bold/italic markers (**text** or *text*)
    line = re.sub(r'\*{1,3}([^*]*)\*{1,3}', r'\1', line)
    # Strip __underline__ markers
    line = re.sub(r'_{1,2}([^_]*)_{1,2}', r'\1', line)
    return line.strip()


def parse_newsletter_sections(text: str) -> list:
    pattern = "|".join(re.escape(h) for h in SECTION_HEADERS)
    parts   = re.split(f"({pattern})", text, flags=re.IGNORECASE)

    sections      = []
    current_title = None
    current_lines = []

    for part in parts:
        if part.strip().upper() in [h.upper() for h in SECTION_HEADERS]:
            if current_title is not None:
                sections.append({
                    "title":   current_title,
                    "bullets": [l for l in (clean_bullet(x) for x in current_lines if x.strip()) if l],
                })
            current_title = part.strip()
            current_lines = []
        else:
            for line in part.strip().splitlines():
                if line.strip():
                    current_lines.append(line.strip())

    if current_title is not None:
        sections.append({
            "title":   current_title,
            "bullets": [l for l in (clean_bullet(x) for x in current_lines if x.strip()) if l],
        })

    return sections

# ── Render HTML ────────────────────────────────────────────────────────────────

SOURCE_TAG_RE = re.compile(r"\[([A-Za-z0-9 /&.\-]+)\]")

def highlight_sources(text: str) -> str:
    """Wrap [Source] tags in styled spans. Used as a Jinja2 filter."""
    return SOURCE_TAG_RE.sub(
        r'<span class="source-tag">[\1]</span>', text
    )


def render_html(sections: list, date_str: str, categorized: dict, stats: list, market_data: dict) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(PROJECT_DIR)),
        autoescape=True,
    )
    env.filters["highlight_sources"] = lambda t: Markup(
        highlight_sources(str(t))
    )
    env.filters["tojson"] = lambda v: Markup(json.dumps(v))
    env.filters["count_articles"] = lambda d: sum(
        1 for articles in d.values() for a in articles if a.get("link")
    )
    template = env.get_template(TEMPLATE)
    return template.render(
        sections=sections,
        date=date_str,
        generated_at=datetime.now().strftime("%I:%M %p"),
        model=ANTHROPIC_MODEL,
        sources=categorized,
        stats=stats,
        market_data=market_data,
    )

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  DAILY FINANCIAL NEWSLETTER GENERATOR")
    print(f"  {datetime.now().strftime('%A, %B %d, %Y  %I:%M %p')}")
    print("=" * 60)

    # 1. Fetch market data (SPY, QQQ, Dow)
    print("\n[1/5] Fetching market data...")
    market_data = fetch_market_data()

    # 2. Fetch RSS feeds
    print("\n[2/5] Fetching RSS feeds...")
    categorized = fetch_all_feeds()
    total = sum(len(v) for v in categorized.values())
    print(f"\n  Total articles collected: {total}")
    if total == 0:
        print("\n  WARNING: No articles found. Check your network connection.")

    # 3. Build prompt
    print("\n[3/5] Building prompt content...")
    raw_content = build_prompt_content(categorized)

    # 4. Call Claude
    print("\n[4/5] Calling Claude API...")
    newsletter_text = call_claude_api(raw_content)

    # 5. Render and save
    print("\n[5/5] Rendering HTML...")
    stats, newsletter_text = parse_stats(newsletter_text)
    sections  = parse_newsletter_sections(newsletter_text)
    date_str  = datetime.now().strftime("%A, %B %d, %Y")
    html      = render_html(sections, date_str, categorized, stats, market_data)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filename = OUTPUT_DIR / f"newsletter_{datetime.now().strftime('%Y-%m-%d')}.html"
    filename.write_text(html, encoding="utf-8")

    print(f"\n  Saved: {filename}")
    print("  Opening in browser...")
    webbrowser.open(filename.as_uri())
    print("\nDone.")


if __name__ == "__main__":
    main()
