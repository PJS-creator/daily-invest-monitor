import os
import re
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
import feedparser
import yaml
import yfinance as yf
import pandas as pd
from bs4 import BeautifulSoup
from dateutil import tz


DEFAULT_TIMEOUT = 25

# 웹 스크래핑 대상(Newswire/IR)은 봇 차단/레이트리밋이 꽤 잦습니다.
# GitHub Actions/cron 환경에서 통과 확률을 높이기 위해 브라우저 UA를 기본으로 둡니다.
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# timeout은 (connect, read) 튜플을 쓰면 '연결은 빠르게 실패' + '응답은 조금 더 기다림'이 가능
HTTP_CONNECT_TIMEOUT = 10
HTTP_READ_TIMEOUT_DEFAULT = 25
HTTP_READ_TIMEOUT_SLOW = 45

HEADERS = {
    "User-Agent": os.getenv("HTTP_USER_AGENT", DEFAULT_UA),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# requests.Session()으로 커넥션 재사용 → 간헐적 timeout/지연을 줄이는 데 도움이 됩니다.
SESSION = requests.Session()

SEC_UA = os.getenv("SEC_USER_AGENT") or HEADERS["User-Agent"]

MONTHS = "(January|February|March|April|May|June|July|August|September|October|November|December)"
GLOBE_DATE_RE = re.compile(rf"{MONTHS}\\s+\\d{{1,2}},\\s+\\d{{4}}\\s+\\d{{1,2}}:\\d{{2}}\\s+ET")

# SEC ticker->CIK cache
_SEC_TICKER_CIK: Optional[Dict[str, str]] = None


@dataclass
class NewsItem:
    title: str
    url: str
    date_text: Optional[str] = None
    source: Optional[str] = None


def load_yaml(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {"last_run": None, "press_seen": {}, "sec_seen": {}, "discussion_id": None}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def ensure_parent_dir(path: str) -> None:
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: str, text: str) -> None:
    ensure_parent_dir(path)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp_path, path)


def atomic_write_json(path: str, payload: dict) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))


def save_state(path: str, state: dict) -> None:
    atomic_write_json(path, state)


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def kst_now_str(tz_name: str) -> str:
    tzone = tz.gettz(tz_name)
    return datetime.now(tzone).strftime("%Y-%m-%d %H:%M:%S %Z")


def today_in_tz(tz_name: str) -> date:
    tzone = tz.gettz(tz_name)
    return datetime.now(tzone).date()


def _choose_timeout(url: str) -> Tuple[int, int]:
    """도메인별로 read timeout을 약간 다르게 줍니다.

    - BusinessWire / IR 페이지는 간헐적으로 느려서 read timeout을 더 길게.
    - 나머지는 기본값.
    """
    u = (url or "").lower()
    if "businesswire.com" in u:
        return (HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT_SLOW)
    if any(k in u for k in ["ir.", "investors.", "gcs-web.com", "q4web", "q4inc"]):
        return (HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT_SLOW)
    return (HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT_DEFAULT)


def http_get(url: str, timeout: Optional[Tuple[int, int]] = None) -> requests.Response:
    """GET with small retries.

    사이트 자체가 느리거나 GitHub Actions IP에서만 지연되는 경우가 있어
    간단한 재시도 + 백오프가 안정성을 크게 올립니다.
    """

    # allow passing an int for backward compatibility
    if timeout is None:
        timeout = _choose_timeout(url)
    elif isinstance(timeout, (int, float)):
        timeout = (HTTP_CONNECT_TIMEOUT, int(timeout))

    last_exc = None
    for i in range(3):
        try:
            resp = SESSION.get(url, headers=HEADERS, timeout=timeout)

            # transient status → retry
            if resp.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {resp.status_code}")

            return resp
        except Exception as e:
            last_exc = e
            time.sleep(1 + i)  # 1s, 2s, 3s

    raise last_exc


def normalize_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    return url


def parse_globenewswire_page(url: str, limit: int = 8) -> List[NewsItem]:
    """GlobeNewswire 검색/organization 페이지에서 최근 PR 링크 추출."""
    resp = http_get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"GlobeNewswire fetch failed: {resp.status_code}")

    soup = BeautifulSoup(resp.text, "html.parser")

    items: List[NewsItem] = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = a.get_text(" ", strip=True)
        if not title:
            continue
        if "/news-release/" not in href:
            continue

        full_url = normalize_url(href)
        if full_url.startswith("/"):
            full_url = "https://www.globenewswire.com" + full_url

        if full_url in seen:
            continue

        # try to infer date from nearby text
        container = a.find_parent()
        date_text = None
        if container:
            text = container.get_text(" ", strip=True)
            m = GLOBE_DATE_RE.search(text)
            if m:
                date_text = m.group(0)

        items.append(NewsItem(title=title, url=full_url, date_text=date_text, source="GlobeNewswire"))
        seen.add(full_url)
        if len(items) >= limit:
            break

    return items


# BusinessWire는 로펌/집단소송 알림 PR이 매우 많이 섞입니다.
# '회사 공식 PR' 모니터링 목적이라면 아래 키워드 포함 제목은 노이즈로 보는 편이 유용합니다.
BW_SPAM_HINTS = [
    "investor alert",
    "investor reminder",
    "class action",
    "lawsuit",
    "deadline",
    "law firm",
    "rosen law",
    "pomerantz",
    "levi & korsinsky",
    "kahn swick",
    "bronstein",
    "faruqi",
    "glancy prongay",
    "gross law",
    "hagens berman",
]


def _is_bw_spam_title(title: str) -> bool:
    t = (title or "").strip().lower()
    if not t:
        return True
    return any(h in t for h in BW_SPAM_HINTS)


def parse_businesswire_search(url: str, limit: int = 8) -> List[NewsItem]:
    """Business Wire 검색 결과 페이지에서 최근 기사 링크 추출."""
    resp = http_get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"BusinessWire fetch failed: {resp.status_code}")

    soup = BeautifulSoup(resp.text, "html.parser")

    items: List[NewsItem] = []
    seen = set()

    # Newer BW often uses list blocks; keep a conservative selector + fallback.
    for li in soup.select("div.bw-news-list li"):
        a = li.select_one("h3 a[href]")
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        href = a.get("href")
        if not title or not href:
            continue

        # 로펌/집단소송 노이즈 제거
        if _is_bw_spam_title(title):
            continue

        full_url = urljoin("https://www.businesswire.com", normalize_url(href))

        if full_url in seen:
            continue

        date_text = None
        t = li.find("time")
        if t:
            date_text = t.get_text(" ", strip=True)

        items.append(NewsItem(title=title, url=full_url, date_text=date_text, source="Business Wire"))
        seen.add(full_url)
        if len(items) >= limit:
            return items

    # fallback: scan for /news/home/
    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = a.get_text(" ", strip=True)
        if not title:
            continue
        if "businesswire.com/news/home/" not in href and "/news/home/" not in href:
            continue

        if _is_bw_spam_title(title):
            continue

        full_url = urljoin("https://www.businesswire.com", normalize_url(href))
        if full_url in seen:
            continue

        items.append(NewsItem(title=title, url=full_url, source="Business Wire"))
        seen.add(full_url)
        if len(items) >= limit:
            break

    return items


def parse_rss_feed(url: str, limit: int = 8, source_name: str = "RSS") -> List[NewsItem]:
    """RSS/Atom 피드를 파싱해서 최근 항목을 가져옵니다.

    IR 사이트가 HTML 페이지를 막거나(403) JS로 렌더링하는 경우가 많아,
    가능하면 RSS/Atom이 가장 안정적입니다.
    """

    # RSS는 XML 응답이라 Accept를 조금 넓게 주는 편이 안전
    resp = http_get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"RSS fetch failed: {resp.status_code}")

    feed = feedparser.parse(resp.content)
    items: List[NewsItem] = []
    seen = set()

    for entry in feed.entries:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        published = (entry.get("published") or entry.get("updated") or "").strip()
        if not title or not link:
            continue
        if link in seen:
            continue

        items.append(NewsItem(title=title, url=link, date_text=published or None, source=source_name))
        seen.add(link)
        if len(items) >= limit:
            break

    return items


def parse_generic_html_news(url: str, limit: int = 8) -> List[NewsItem]:
    """일반적인 IR/News 페이지에서 제목 링크를 최대한 보수적으로 추출."""
    resp = http_get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"HTML fetch failed: {resp.status_code}")
    soup = BeautifulSoup(resp.text, "html.parser")

    items: List[NewsItem] = []
    seen = set()

    # Priority 1: h2/h3 anchors
    for tag in soup.find_all(["h2", "h3"]):
        a = tag.find("a", href=True)
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        href = a["href"]
        if not title or not href:
            continue

        full_url = normalize_url(href)
        if full_url.startswith("/"):
            m = re.match(r"^(https?://[^/]+)", url)
            if m:
                full_url = m.group(1) + full_url

        if full_url in seen:
            continue

        container = tag.parent
        date_text = None
        if container:
            text = container.get_text(" ", strip=True)
            m2 = re.search(rf"{MONTHS}\\s+\\d{{1,2}},\\s+20\\d{{2}}", text)
            m1 = re.search(r"\\b20\\d{2}\\b", text)
            if m2:
                date_text = m2.group(0)
            elif m1:
                date_text = m1.group(0)

        items.append(NewsItem(title=title, url=full_url, date_text=date_text, source=url))
        seen.add(full_url)
        if len(items) >= limit:
            return items

    # Priority 2: anchors with press/news/release in href
    for a in soup.find_all("a", href=True):
        title = a.get_text(" ", strip=True)
        href = a["href"]
        if not title or not href:
            continue
        if len(title) < 12:
            continue
        if not any(k in href.lower() for k in ["press", "news", "release"]):
            continue

        full_url = normalize_url(href)
        if full_url.startswith("/"):
            m = re.match(r"^(https?://[^/]+)", url)
            if m:
                full_url = m.group(1) + full_url

        if full_url in seen:
            continue

        items.append(NewsItem(title=title, url=full_url, source=url))
        seen.add(full_url)
        if len(items) >= limit:
            break

    return items


def fetch_press_items(company: dict, limit: int) -> Tuple[List[NewsItem], List[str]]:
    """company.sources에 정의된 모든 소스에서 item을 모아 dedupe 후 반환."""
    errors: List[str] = []
    sources = company.get("sources") or []

    all_items: List[NewsItem] = []

    for src in sources:
        stype = (src.get("type") or "").strip()
        url = (src.get("url") or "").strip()
        if not stype or not url:
            continue

        try:
            if stype in ["globenewswire_org", "globenewswire_keyword"]:
                items = parse_globenewswire_page(url, limit=limit)
            elif stype == "businesswire_search":
                items = parse_businesswire_search(url, limit=limit)
            elif stype in ["rss", "atom"]:
                items = parse_rss_feed(url, limit=limit, source_name="RSS")
            else:
                items = parse_generic_html_news(url, limit=limit)

            all_items.extend(items)
        except Exception as e:
            errors.append(f"{stype} {url} -> {e}")

    # dedupe by URL (preserve order)
    deduped: List[NewsItem] = []
    seen = set()
    for it in all_items:
        if it.url in seen:
            continue
        deduped.append(it)
        seen.add(it.url)

    return deduped[:limit], errors


def fetch_prices(tickers: List[str]) -> Dict[str, dict]:
    """yfinance로 최근 2개 거래일 종가/거래량을 받아서 일간 변동을 계산."""
    result: Dict[str, dict] = {}

    df = yf.download(
        tickers=" ".join(tickers),
        period="10d",
        interval="1d",
        auto_adjust=False,
        threads=True,
        group_by="ticker",
        progress=False,
    )

    def _one_ticker_frame(ticker: str) -> pd.DataFrame:
        if isinstance(df.columns, pd.MultiIndex):
            return df[ticker].dropna(how="all")
        return df.dropna(how="all")

    for t in tickers:
        try:
            f = _one_ticker_frame(t).dropna()
            if len(f) < 2:
                raise RuntimeError("Not enough price rows (need >= 2 trading days).")
            last = f.iloc[-1]
            prev = f.iloc[-2]
            close = float(last["Close"])
            prev_close = float(prev["Close"])
            vol = float(last.get("Volume", float("nan")))
            chg = close - prev_close
            pct = (close / prev_close - 1.0) * 100.0 if prev_close else float("nan")

            result[t] = {
                "close": close,
                "prev_close": prev_close,
                "chg": chg,
                "pct": pct,
                "volume": vol,
                "last_date": str(f.index[-1].date()),
            }
        except Exception as e:
            result[t] = {"error": str(e)}

    return result


def fetch_fundamentals(ticker: str) -> dict:
    """시총/EV/52주 등. 실패해도 report는 계속."""
    out = {}
    try:
        info = yf.Ticker(ticker).info or {}
        out["market_cap"] = info.get("marketCap")
        out["enterprise_value"] = info.get("enterpriseValue")
        out["shares_outstanding"] = info.get("sharesOutstanding")
        out["total_cash"] = info.get("totalCash")
        out["total_debt"] = info.get("totalDebt")
        out["fifty_two_week_low"] = info.get("fiftyTwoWeekLow")
        out["fifty_two_week_high"] = info.get("fiftyTwoWeekHigh")
        out["avg_volume"] = info.get("averageVolume")
        out["earnings_timestamp"] = info.get("earningsTimestamp")
        out["earnings_timestamp_start"] = info.get("earningsTimestampStart")
        out["earnings_timestamp_end"] = info.get("earningsTimestampEnd")
        # Analyst price targets (consensus, usually from Yahoo Finance)
        out["pt_mean"] = info.get("targetMeanPrice")
        out["pt_high"] = info.get("targetHighPrice")
        out["pt_low"] = info.get("targetLowPrice")
        out["pt_median"] = info.get("targetMedianPrice")
        out["pt_analysts"] = info.get("numberOfAnalystOpinions")
        out["recommendation_key"] = info.get("recommendationKey")
        out["recommendation_mean"] = info.get("recommendationMean")
    except Exception as e:
        out["fund_error"] = str(e)
    return out


def _sec_load_ticker_cik_map() -> Dict[str, str]:
    global _SEC_TICKER_CIK
    if _SEC_TICKER_CIK is not None:
        return _SEC_TICKER_CIK

    mapping_url = "https://www.sec.gov/files/company_tickers.json"
    resp = requests.get(mapping_url, headers={"User-Agent": SEC_UA}, timeout=DEFAULT_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"SEC mapping fetch failed: {resp.status_code}")

    mapping = resp.json()
    out: Dict[str, str] = {}
    for _, row in mapping.items():
        t = str(row.get("ticker", "")).upper().strip()
        cik = str(row.get("cik_str", "")).strip()
        if t and cik:
            out[t] = cik.zfill(10)

    _SEC_TICKER_CIK = out
    return out


def sec_fetch_atom_by_ticker(ticker: str, limit: int = 6) -> List[NewsItem]:
    """SEC Atom feed(공시) - ticker->CIK 매핑 후 회사 Atom feed 조회."""
    mapping = _sec_load_ticker_cik_map()
    cik = mapping.get(ticker.upper())
    if not cik:
        return []

    atom_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&count=40&output=atom"
    feed = feedparser.parse(atom_url, request_headers={"User-Agent": SEC_UA})

    items: List[NewsItem] = []
    for entry in feed.entries[:limit]:
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        published = entry.get("published", "").strip()
        if title and link:
            items.append(NewsItem(title=title, url=link, date_text=published, source="SEC"))
    return items


def format_money_usd(n: Optional[float]) -> str:
    if n is None:
        return "-"
    try:
        n = float(n)
    except Exception:
        return "-"

    sign = "-" if n < 0 else ""
    n = abs(n)

    if n >= 1_000_000_000:
        return f"{sign}${n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{sign}${n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{sign}${n/1_000:.1f}K"
    return f"{sign}${n:.0f}"


def format_price(n: Optional[float]) -> str:
    if n is None:
        return "-"
    try:
        return f"{float(n):.2f}"
    except Exception:
        return "-"


def format_pct(n: Optional[float]) -> str:
    if n is None:
        return "-"
    try:
        return f"{float(n):+.2f}%"
    except Exception:
        return "-"


def parse_iso_date(s: str) -> Optional[date]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def pick_next_catalyst(catalysts: List[dict], today: date) -> Tuple[str, Optional[int]]:
    """Return (label, days_until) for the nearest dated catalyst; fallback to first item."""
    if not catalysts:
        return ("-", None)

    dated: List[Tuple[date, dict]] = []
    for c in catalysts:
        d = parse_iso_date(str(c.get("date") or ""))
        if d:
            dated.append((d, c))

    future = [(d, c) for d, c in dated if d >= today]
    if future:
        future.sort(key=lambda x: x[0])
        d, c = future[0]
        days = (d - today).days
        when = str(c.get("when") or d.isoformat())
        ev = str(c.get("event") or "").strip()
        label = f"{when}: {ev}" if ev else when
        return (label, days)

    # no future dated items → fallback to first
    c = catalysts[0]
    when = str(c.get("when") or "").strip() or "TBD"
    ev = str(c.get("event") or "").strip()
    label = f"{when}: {ev}" if ev else when
    return (label, None)


def render_catalyst_table(catalysts: List[dict], today: date) -> List[str]:
    if not catalysts:
        return ["- (등록된 촉매 없음)"]

    lines: List[str] = []
    lines.append("| When | D-day | Event | Importance |")
    lines.append("|---|---:|---|---:|")

    for c in catalysts[:6]:
        when = str(c.get("when") or "TBD")
        d = parse_iso_date(str(c.get("date") or ""))
        dday = "-"
        if d:
            delta = (d - today).days
            dday = f"D-{delta}" if delta >= 0 else f"D+{abs(delta)}"
        ev = str(c.get("event") or "").replace("|", "/")
        imp = c.get("importance")
        imp_str = str(imp) if isinstance(imp, int) else (str(imp) if imp else "-")
        lines.append(f"| {when} | {dday} | {ev} | {imp_str} |")

    return lines


# ------------------------
# GitHub Discussions Posting (GraphQL)
# ------------------------


def _gh_token() -> str:
    token = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Missing GITHUB_TOKEN (set it in the workflow env).")
    return token


def github_graphql(query: str, variables: Optional[dict] = None) -> dict:
    token = _gh_token()
    resp = requests.post(
        "https://api.github.com/graphql",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        json={"query": query, "variables": variables or {}},
        timeout=DEFAULT_TIMEOUT,
    )

    if resp.status_code != 200:
        raise RuntimeError(f"GitHub GraphQL failed: {resp.status_code} {resp.text[:200]}")

    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(f"GitHub GraphQL errors: {payload['errors']}")

    return payload.get("data") or {}


def ensure_daily_discussion(state: dict, title: str, category_name: str) -> str:
    """Daily Report discussion id를 확보(없으면 생성)하고 state에 저장."""

    if state.get("discussion_id"):
        return str(state["discussion_id"])

    repo_full = os.getenv("GITHUB_REPOSITORY", "")
    if "/" not in repo_full:
        raise RuntimeError("Missing GITHUB_REPOSITORY env (should be set by GitHub Actions).")
    owner, repo = repo_full.split("/", 1)

    q = """
    query($owner:String!, $repo:String!) {
      repository(owner:$owner, name:$repo) {
        id
        discussionCategories(first:50) { nodes { id name } }
        discussions(first:50, orderBy:{field:UPDATED_AT, direction:DESC}) {
          nodes { id title url number }
        }
      }
    }
    """
    data = github_graphql(q, {"owner": owner, "repo": repo})
    r = data.get("repository") or {}

    repo_id = r.get("id")
    if not repo_id:
        raise RuntimeError("Failed to resolve repository id. Is the token valid?")

    cats = (r.get("discussionCategories") or {}).get("nodes") or []
    cat_id = None
    for c in cats:
        if str(c.get("name", "")).strip().lower() == category_name.strip().lower():
            cat_id = c.get("id")
            break
    if not cat_id and cats:
        cat_id = cats[0].get("id")

    if not cat_id:
        raise RuntimeError("No discussion categories found. Is Discussions enabled?")

    nodes = (r.get("discussions") or {}).get("nodes") or []
    for d in nodes:
        if str(d.get("title", "")).strip() == title.strip():
            state["discussion_id"] = d.get("id")
            return str(d.get("id"))

    m = """
    mutation($repoId:ID!, $catId:ID!, $title:String!, $body:String!) {
      createDiscussion(input:{repositoryId:$repoId, categoryId:$catId, title:$title, body:$body}) {
        discussion { id url }
      }
    }
    """
    body = (
        "이 글은 GitHub Actions가 매일 자동으로 댓글을 달아 업데이트합니다.\n\n"
        "(처음 생성된 스레드입니다.)"
    )
    out = github_graphql(m, {"repoId": repo_id, "catId": cat_id, "title": title, "body": body})
    disc = ((out.get("createDiscussion") or {}).get("discussion") or {})
    disc_id = disc.get("id")
    if not disc_id:
        raise RuntimeError("Failed to create discussion.")

    state["discussion_id"] = disc_id
    return str(disc_id)


def add_discussion_comment(discussion_id: str, body: str) -> str:
    m = """
    mutation($discussionId:ID!, $body:String!) {
      addDiscussionComment(input:{discussionId:$discussionId, body:$body}) {
        comment { url }
      }
    }
    """
    out = github_graphql(m, {"discussionId": discussion_id, "body": body})
    url = (((out.get("addDiscussionComment") or {}).get("comment") or {}).get("url"))
    return str(url or "")


def post_report_to_discussions(state: dict, report_md: str) -> None:
    title = (os.getenv("DISCUSSION_TITLE") or "Daily Report").strip()
    category = (os.getenv("DISCUSSION_CATEGORY") or "General").strip()

    discussion_id = ensure_daily_discussion(state, title=title, category_name=category)
    comment_url = add_discussion_comment(discussion_id, report_md)

    if comment_url:
        print(f"Posted comment: {comment_url}")
    else:
        print("Posted comment.")


def choose_overall_judgement(alert_tickers: List[str], total_new_press: int, total_new_sec: int) -> str:
    if alert_tickers:
        return "행동 고려"
    if total_new_sec or total_new_press:
        return "추가 확인 필요"
    return "감시만"


def compact_titles(items: List[dict], limit: int = 2) -> str:
    titles: List[str] = []
    for it in items[:limit]:
        title = str(it.get("title") or "").strip()
        if title:
            titles.append(title)
    return "; ".join(titles)


def render_pulse_report(payload: dict) -> str:
    meta = payload.get("meta") or {}
    summary = payload.get("summary") or {}
    companies = payload.get("companies") or []

    changed = [c for c in companies if c.get("new_pr_count") or c.get("new_sec_count") or c.get("alerts")]
    changed.sort(
        key=lambda c: (
            len(c.get("alerts") or []),
            c.get("new_sec_count") or 0,
            c.get("new_pr_count") or 0,
            abs(float(c.get("pct") or 0.0)),
        ),
        reverse=True,
    )

    unchanged = [c.get("ticker") for c in companies if not (c.get("new_pr_count") or c.get("new_sec_count") or c.get("alerts"))]

    lines: List[str] = []
    lines.append(f"# Pulse Input ({meta.get('generated_at_local')})")
    lines.append("")
    lines.append(f"- 전체 판단: **{summary.get('overall_judgement', '-')}**")
    lines.append(
        f"- 요약: PR {summary.get('total_new_press', 0)}건 / SEC {summary.get('total_new_sec', 0)}건 / Alert {', '.join(summary.get('alert_tickers') or []) if summary.get('alert_tickers') else '-'}"
    )
    lines.append("")
    lines.append("## Must Watch")

    if changed:
        for c in changed[:8]:
            headline_bits: List[str] = []
            if c.get("alerts"):
                headline_bits.append(", ".join(c.get("alerts") or []))
            if c.get("new_sec_count"):
                headline_bits.append(f"SEC {c.get('new_sec_count')}건")
            if c.get("new_pr_count"):
                headline_bits.append(f"PR {c.get('new_pr_count')}건")
            if c.get("pct") is not None:
                headline_bits.append(f"주가 {format_pct(c.get('pct'))}")

            lines.append(f"- **{c.get('ticker')}** ({c.get('name')}) — {' | '.join(headline_bits) if headline_bits else '변화 감지'}")

            reason_parts: List[str] = []
            sec_titles = compact_titles(c.get("new_sec") or [], limit=2)
            pr_titles = compact_titles(c.get("new_press") or [], limit=2)
            if sec_titles:
                reason_parts.append(f"SEC: {sec_titles}")
            if pr_titles:
                reason_parts.append(f"PR: {pr_titles}")
            if reason_parts:
                lines.append(f"  - 변화: {' / '.join(reason_parts)}")
            if c.get("next_catalyst"):
                lines.append(f"  - 다음 촉매: {c.get('next_catalyst')}")
            checklist = c.get("checklist") or []
            if checklist:
                lines.append(f"  - 체크포인트: {checklist[0]}")
    else:
        lines.append("- 오늘은 즉시 대응할 내용 없음")

    lines.append("")
    lines.append("## Ignore / Noise")
    if unchanged:
        lines.append("- 큰 변화 없음: " + ", ".join(unchanged))
    else:
        lines.append("- 제외할 잡음 없음")

    lines.append("")
    lines.append("## Source Files")
    lines.append("- reports/latest.md : 전체 리포트")
    lines.append("- reports/latest.json : 구조화 JSON")
    lines.append("- reports/latest_pulse.md : Pulse/Task 입력용 요약")

    return "\n".join(lines)


def write_repo_outputs(payload: dict, report_md: str, pulse_md: str, tz_name: str, storage_cfg: dict) -> dict:
    reports_dir = str(storage_cfg.get("reports_dir") or "reports")
    archive_enabled = bool(storage_cfg.get("archive_by_run") if "archive_by_run" in storage_cfg else True)
    latest_enabled = bool(storage_cfg.get("latest_aliases") if "latest_aliases" in storage_cfg else True)

    now_local = datetime.now(tz.gettz(tz_name))
    run_id = now_local.strftime("%Y%m%d_%H%M%S_%Z")
    archive_dir = os.path.join(reports_dir, "archive", now_local.strftime("%Y"), now_local.strftime("%m"), now_local.strftime("%d"))

    written = {}

    if latest_enabled:
        latest_md = os.path.join(reports_dir, "latest.md")
        latest_json = os.path.join(reports_dir, "latest.json")
        latest_pulse = os.path.join(reports_dir, "latest_pulse.md")
        atomic_write_text(latest_md, report_md)
        atomic_write_json(latest_json, payload)
        atomic_write_text(latest_pulse, pulse_md)
        written["latest_md"] = latest_md
        written["latest_json"] = latest_json
        written["latest_pulse"] = latest_pulse

    if archive_enabled:
        archive_md = os.path.join(archive_dir, f"{run_id}_report.md")
        archive_json = os.path.join(archive_dir, f"{run_id}_report.json")
        archive_pulse = os.path.join(archive_dir, f"{run_id}_pulse.md")
        atomic_write_text(archive_md, report_md)
        atomic_write_json(archive_json, payload)
        atomic_write_text(archive_pulse, pulse_md)
        written["archive_md"] = archive_md
        written["archive_json"] = archive_json
        written["archive_pulse"] = archive_pulse

    manifest = {
        "generated_at_local": payload.get("meta", {}).get("generated_at_local"),
        "generated_at_utc": payload.get("meta", {}).get("generated_at_utc"),
        "paths": written,
    }
    manifest_path = os.path.join(reports_dir, "manifest.json")
    atomic_write_json(manifest_path, manifest)
    written["manifest"] = manifest_path

    return written


# ------------------------
# Main
# ------------------------


def main() -> None:
    cfg = load_yaml("config.yaml")
    tz_name = cfg.get("timezone") or "Asia/Seoul"

    report_cfg = cfg.get("report") or {}
    max_news = int(report_cfg.get("max_news_items") or 8)
    use_details = bool(report_cfg.get("use_collapsible") if "use_collapsible" in report_cfg else True)

    storage_cfg = cfg.get("storage") or {}
    save_repo_outputs = bool(storage_cfg.get("save_repo_outputs") if "save_repo_outputs" in storage_cfg else True)
    post_to_discussions = env_flag("POST_TO_DISCUSSIONS", bool(storage_cfg.get("post_to_discussions") if "post_to_discussions" in storage_cfg else True))

    alert_cfg = cfg.get("alerts") or {}
    alert_price_pct = float(alert_cfg.get("price_move_pct") or 8.0)
    alert_volume_x = float(alert_cfg.get("volume_spike_x") or 2.5)

    # Analyst price targets (consensus)
    # - 기본은 yfinance(info)에서 targetMean/High/Low를 가져옵니다.
    # - show/alert 옵션은 config.yaml의 price_targets 섹션으로 제어할 수 있습니다.
    pt_cfg = cfg.get("price_targets") or {}
    pt_show_dashboard = bool(pt_cfg.get("show_in_dashboard") if "show_in_dashboard" in pt_cfg else True)
    pt_show_details = bool(pt_cfg.get("show_in_details") if "show_in_details" in pt_cfg else True)
    pt_alert_on_change = bool(pt_cfg.get("alert_on_change") if "alert_on_change" in pt_cfg else True)
    pt_change_pct = float(pt_cfg.get("change_pct") or 5.0)
    pt_change_abs = float(pt_cfg.get("change_abs") or 0.5)

    companies = cfg.get("companies") or []
    tickers = [c["ticker"] for c in companies]

    state_path = os.path.join("data", "state.json")
    state = load_state(state_path)
    state.setdefault("press_seen", {})
    state.setdefault("sec_seen", {})
    state.setdefault("pt_last", {})

    # 1) price data
    price_map = fetch_prices(tickers)

    now_kst = kst_now_str(tz_name)
    today = today_in_tz(tz_name)

    dashboard_rows: List[dict] = []
    details_blocks: List[str] = []
    company_payloads: List[dict] = []

    total_new_press = 0
    total_new_sec = 0
    alert_tickers: List[str] = []

    for c in companies:
        t = c["ticker"]
        name = c.get("name") or t
        anchors = c.get("anchors") or {}
        catalysts = c.get("catalysts") or []

        # --- Price ---
        p = price_map.get(t, {})
        close = None
        pct = None
        vol = None
        last_dt = None
        price_error = None
        if "error" in p:
            price_error = p.get("error")
        else:
            close = p.get("close")
            pct = p.get("pct")
            vol = p.get("volume")
            last_dt = p.get("last_date")

        # --- Fundamentals ---
        fund = fetch_fundamentals(t)
        mktcap = fund.get("market_cap")
        avg_vol = fund.get("avg_volume")

        # --- Analyst price targets (consensus) ---
        pt_mean = fund.get("pt_mean")
        pt_high = fund.get("pt_high")
        pt_low = fund.get("pt_low")
        pt_median = fund.get("pt_median")
        pt_n = fund.get("pt_analysts")
        reco_key = fund.get("recommendation_key")
        reco_mean = fund.get("recommendation_mean")

        pt_upside = None
        if close is not None and pt_mean is not None:
            try:
                pt_upside = (float(pt_mean) / float(close) - 1.0) * 100.0
            except Exception:
                pt_upside = None

        # Detect PT change vs last run (optional alert)
        pt_changed = False
        prev_pt = (state.get("pt_last") or {}).get(t) or {}
        prev_mean = prev_pt.get("mean")
        if pt_mean is not None and prev_mean is not None:
            try:
                abs_ch = abs(float(pt_mean) - float(prev_mean))
                pct_ch = abs_ch / float(prev_mean) * 100.0 if float(prev_mean) != 0 else abs_ch
                if abs_ch >= pt_change_abs or pct_ch >= pt_change_pct:
                    pt_changed = True
            except Exception:
                pt_changed = False

        # --- Anchors: Net cash / Burn / Runway / Buyout per share ---
        asof = anchors.get("as_of") or anchors.get("asof") or anchors.get("as_of_date")
        net_cash_m = anchors.get("net_cash_m")
        if net_cash_m is None:
            # allow alternative key names
            net_cash_m = anchors.get("net_cash")
        buyout_ps = anchors.get("buyout_per_share") or {}
        base_ps = buyout_ps.get("base")

        # EV calc (uses net cash anchor if available)
        ev_calc = None
        if mktcap is not None and net_cash_m is not None:
            try:
                ev_calc = float(mktcap) - float(net_cash_m) * 1_000_000
            except Exception:
                ev_calc = None

        # volume ratio
        vol_x = None
        if vol is not None and avg_vol:
            try:
                if not pd.isna(vol) and float(avg_vol) > 0:
                    vol_x = float(vol) / float(avg_vol)
            except Exception:
                vol_x = None

        # upside to base (simple math)
        upside = None
        if close is not None and base_ps is not None:
            try:
                upside = (float(base_ps) / float(close) - 1.0) * 100.0
            except Exception:
                upside = None

        # next catalyst
        next_label, next_dd = pick_next_catalyst(catalysts, today)
        next_cell = next_label
        if next_dd is not None:
            next_cell = f"{next_label} (D-{next_dd})"

        # --- Press releases ---
        press_items, press_errors = fetch_press_items(c, limit=max_news)
        seen_urls = set(state["press_seen"].get(t, []))
        new_press = [it for it in press_items if it.url not in seen_urls]
        total_new_press += len(new_press)

        # --- SEC ---
        sec_items: List[NewsItem] = []
        new_sec: List[NewsItem] = []
        sec_error = None
        try:
            time.sleep(0.25)
            sec_items = sec_fetch_atom_by_ticker(t, limit=max_news)
            sec_seen = set(state["sec_seen"].get(t, []))
            new_sec = [it for it in sec_items if it.url not in sec_seen]
            total_new_sec += len(new_sec)
        except Exception as e:
            sec_error = str(e)

        # --- Alerts ---
        flags: List[str] = []
        if pct is not None:
            try:
                if abs(float(pct)) >= alert_price_pct:
                    flags.append(f"PRICE {float(pct):+.1f}%")
            except Exception:
                pass
        if vol_x is not None:
            try:
                if float(vol_x) >= alert_volume_x:
                    flags.append(f"VOL {float(vol_x):.1f}x")
            except Exception:
                pass
        if pt_changed and pt_alert_on_change:
            flags.append(f"PT {format_price(prev_mean)}→{format_price(pt_mean)}")
        if flags:
            alert_tickers.append(t)

        # --- Dashboard row ---
        dashboard_rows.append(
            {
                "ticker": t,
                "close": close,
                "pct": pct,
                "pt": pt_mean,
                "pt_upside": pt_upside,
                "mktcap": format_money_usd(mktcap),
                "ev": format_money_usd(ev_calc if ev_calc is not None else fund.get("enterprise_value")),
                "net_cash": (format_money_usd(float(net_cash_m) * 1_000_000) + (f" ({asof})" if asof else ""))
                if net_cash_m is not None
                else "-",
                "runway": (str(anchors.get("runway_months")) + "m") if anchors.get("runway_months") else "-",
                "next": next_cell.replace("|", "/"),
                "new_pr": len(new_press),
                "new_sec": len(new_sec),
                "alert": "; ".join(flags) if flags else "-",
            }
        )

        # --- Details block ---
        block: List[str] = []
        summary_bits: List[str] = []
        if close is not None and pct is not None:
            summary_bits.append(f"Close {close:.2f} ({pct:+.2f}%)")
        if flags:
            summary_bits.append("⚠️ " + ", ".join(flags))
        if len(new_press) or len(new_sec):
            summary_bits.append(f"PR {len(new_press)} / SEC {len(new_sec)}")
        if next_label != "-":
            summary_bits.append(f"Next: {next_cell}")

        header = f"{t} — {name}"
        summary_line = " | ".join(summary_bits)

        if use_details:
            block.append(f"<details>\n<summary><strong>{header}</strong> — {summary_line}</summary>\n")
        else:
            block.append(f"### {header}\n")

        # Key metrics
        block.append("\n#### Key Metrics")
        if price_error:
            block.append(f"- ⚠️ **가격 데이터 오류:** {price_error}")
        else:
            dt_str = f" ({last_dt})" if last_dt else ""
            block.append(f"- **Close{dt_str}**: `{format_price(close)}` ({format_pct(pct)})")
            if vol is not None and not pd.isna(vol):
                v_line = f"- **Volume**: `{int(vol):,}`"
                if vol_x is not None:
                    v_line += f" (≈ `{vol_x:.2f}x` avg)"
                block.append(v_line)

        block.append(f"- **Market Cap**: {format_money_usd(mktcap)}")
        if ev_calc is not None:
            note = f" (calc; net cash as-of {asof})" if asof else " (calc)"
            block.append(f"- **EV**: {format_money_usd(ev_calc)}{note}")
        else:
            block.append(f"- **EV**: {format_money_usd(fund.get('enterprise_value'))} (yfinance)")

        # Analyst price target (Street consensus)
        if pt_show_details:
            if pt_mean is not None or pt_high is not None or pt_low is not None:
                n_str = '-'
                try:
                    if pt_n is not None:
                        n_str = str(int(pt_n))
                except Exception:
                    n_str = str(pt_n) if pt_n is not None else '-'

                block.append(
                    f"- **Price Target (consensus)**: mean `{format_price(pt_mean)}` (low `{format_price(pt_low)}` / high `{format_price(pt_high)}`; n={n_str})"
                )
                if pt_upside is not None:
                    block.append(f"  - Upside to PT (mean): `{pt_upside:+.1f}%`")
            else:
                block.append("- **Price Target (consensus)**: -")

        if net_cash_m is not None:
            block.append(
                f"- **Net Cash (anchor)**: {format_money_usd(float(net_cash_m) * 1_000_000)}"
                + (f" (as-of {asof})" if asof else "")
            )

        burn = anchors.get("burn_m_per_month")
        if burn is not None:
            block.append(f"- **Burn (anchor)**: ~${float(burn):.1f}M / month")
        burn_q = anchors.get("burn_m_per_quarter")
        if burn_q is not None:
            block.append(f"- **Burn (anchor)**: ~${float(burn_q):.1f}M / quarter")
        burn_y = anchors.get("burn_m_per_year")
        if burn_y is not None:
            block.append(f"- **Burn (anchor)**: ~${float(burn_y):.0f}M / year")

        if anchors.get("runway_months") is not None:
            block.append(f"- **Runway (anchor)**: ~{anchors.get('runway_months')} months")

        if base_ps is not None:
            block.append(
                f"- **Buyout/SOTP (anchor)**: Bear `{buyout_ps.get('bear','-')}` / Base `{buyout_ps.get('base','-')}` / Bull `{buyout_ps.get('bull','-')}` (USD/share)"
            )
            if upside is not None:
                block.append(f"  - Upside to Base (simple): `{upside:+.1f}%`")

        # Catalysts
        block.append("\n#### Catalyst Calendar")
        block.extend(render_catalyst_table(catalysts, today))

        # Checklist
        checklist = c.get("checklist") or []
        block.append("\n#### Today’s Checklist")
        if checklist:
            for item in checklist:
                block.append(f"- {item}")
        else:
            block.append("- (없음)")

        # Press
        block.append("\n#### New Press Releases")
        if new_press:
            for it in new_press[:max_news]:
                dtxt = f" — _{it.source}_, {it.date_text}" if it.date_text else (f" — _{it.source}_" if it.source else "")
                block.append(f"- [{it.title}]({it.url}){dtxt}")
        else:
            block.append("- (신규 없음)")

        # SEC
        block.append("\n#### New SEC Filings (EDGAR)")
        if sec_error:
            block.append(f"- ⚠️ SEC 조회 실패: {sec_error}")
        else:
            if new_sec:
                for it in new_sec[:max_news]:
                    dtxt = f" — {it.date_text}" if it.date_text else ""
                    block.append(f"- [{it.title}]({it.url}){dtxt}")
            else:
                block.append("- (신규 없음)")

        # Debug errors
        if press_errors:
            block.append("\n<details><summary>DEBUG(Press source errors)</summary>")
            for err in press_errors[:5]:
                block.append(f"- {err}")
            block.append("</details>")

        if use_details:
            block.append("\n</details>")

        details_blocks.append("\n".join(block))

        company_payloads.append(
            {
                "ticker": t,
                "name": name,
                "close": close,
                "pct": pct,
                "volume": vol,
                "volume_x": vol_x,
                "last_price_date": last_dt,
                "market_cap": mktcap,
                "enterprise_value": ev_calc if ev_calc is not None else fund.get("enterprise_value"),
                "net_cash_m": net_cash_m,
                "runway_months": anchors.get("runway_months"),
                "alerts": flags,
                "new_pr_count": len(new_press),
                "new_sec_count": len(new_sec),
                "new_press": [
                    {"title": it.title, "url": it.url, "date_text": it.date_text, "source": it.source}
                    for it in new_press[:max_news]
                ],
                "new_sec": [
                    {"title": it.title, "url": it.url, "date_text": it.date_text, "source": it.source}
                    for it in new_sec[:max_news]
                ],
                "next_catalyst": next_cell,
                "checklist": checklist,
                "price_target": {
                    "mean": pt_mean,
                    "low": pt_low,
                    "high": pt_high,
                    "median": pt_median,
                    "analyst_count": pt_n,
                    "recommendation_key": reco_key,
                    "recommendation_mean": reco_mean,
                    "upside_pct": pt_upside,
                },
                "anchors": {
                    "as_of": asof,
                    "net_cash_m": net_cash_m,
                    "burn_m_per_month": anchors.get("burn_m_per_month"),
                    "burn_m_per_quarter": anchors.get("burn_m_per_quarter"),
                    "burn_m_per_year": anchors.get("burn_m_per_year"),
                    "runway_months": anchors.get("runway_months"),
                    "buyout_per_share": buyout_ps,
                },
                "errors": {
                    "price_error": price_error,
                    "press_errors": press_errors,
                    "sec_error": sec_error,
                },
            }
        )

        # --- Update state ---
        merged_press = list(dict.fromkeys([it.url for it in press_items] + list(seen_urls)))
        state["press_seen"][t] = merged_press[:600]

        try:
            merged_sec = list(dict.fromkeys([it.url for it in sec_items] + list(state["sec_seen"].get(t, []))))
            state["sec_seen"][t] = merged_sec[:600]
        except Exception:
            pass

        # --- Store last-known price target snapshot ---
        try:
            state.setdefault("pt_last", {})
            if any(x is not None for x in [pt_mean, pt_high, pt_low, pt_median, pt_n]):
                state["pt_last"][t] = {
                    "mean": pt_mean,
                    "high": pt_high,
                    "low": pt_low,
                    "median": pt_median,
                    "n": pt_n,
                    "ts": now_kst,
                }
        except Exception:
            pass

    state["last_run"] = datetime.now(timezone.utc).isoformat()

    # --- Render report ---
    lines: List[str] = []
    lines.append(f"## Daily Report ({now_kst})")
    lines.append("")

    alert_str = ", ".join(alert_tickers) if alert_tickers else "-"
    lines.append(f"**Summary:** {total_new_press} new PR / {total_new_sec} new SEC | **Alerts:** {alert_str}")
    lines.append("")

    # Dashboard table
    lines.append("### Dashboard")
    if pt_show_dashboard:
        lines.append("| Ticker | Close | Δ% | PT | Upside to PT | MktCap | EV | Net Cash (anchor) | Runway | Next Catalyst | PR | SEC | Alert |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---|")
    else:
        lines.append("| Ticker | Close | Δ% | MktCap | EV | Net Cash (anchor) | Runway | Next Catalyst | PR | SEC | Alert |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---|")

    for r in dashboard_rows:
        close_s = format_price(r.get("close"))
        pct_s = format_pct(r.get("pct"))
        if pt_show_dashboard:
            pt_s = format_price(r.get("pt"))
            pt_u = format_pct(r.get("pt_upside"))
            lines.append(
                f"| {r['ticker']} | {close_s} | {pct_s} | {pt_s} | {pt_u} | {r['mktcap']} | {r['ev']} | {r['net_cash']} | {r['runway']} | {r['next']} | {r['new_pr']} | {r['new_sec']} | {r['alert']} |"
            )
        else:
            lines.append(
                f"| {r['ticker']} | {close_s} | {pct_s} | {r['mktcap']} | {r['ev']} | {r['net_cash']} | {r['runway']} | {r['next']} | {r['new_pr']} | {r['new_sec']} | {r['alert']} |"
            )

    lines.append("")
    lines.append("### Details")
    lines.append("")
    lines.extend(details_blocks)

    report_md = "\n".join(lines)

    overall_judgement = choose_overall_judgement(alert_tickers, total_new_press, total_new_sec)
    payload = {
        "meta": {
            "generated_at_local": now_kst,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "timezone": tz_name,
            "repository": os.getenv("GITHUB_REPOSITORY"),
            "ref_name": os.getenv("GITHUB_REF_NAME"),
        },
        "summary": {
            "total_new_press": total_new_press,
            "total_new_sec": total_new_sec,
            "alert_tickers": alert_tickers,
            "overall_judgement": overall_judgement,
        },
        "dashboard": dashboard_rows,
        "companies": company_payloads,
    }

    pulse_md = render_pulse_report(payload)

    written = {}
    if save_repo_outputs:
        written = write_repo_outputs(payload, report_md, pulse_md, tz_name, storage_cfg)
        payload.setdefault("meta", {})["written_files"] = written
        state["last_written_files"] = written

    discussion_status = "skipped"
    if post_to_discussions:
        try:
            post_report_to_discussions(state, report_md)
            discussion_status = "posted"
        except Exception as e:
            discussion_status = f"failed: {e}"
            print(f"WARNING: discussion posting failed, but repo outputs were saved. -> {e}")

    state["last_discussion_status"] = discussion_status

    # Save state
    save_state(state_path, state)

    print(f"DONE. Repo outputs saved. Discussion status: {discussion_status}")


if __name__ == "__main__":
    main()
