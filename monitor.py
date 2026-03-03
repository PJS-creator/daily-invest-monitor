import os
import re
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone, date
from typing import Dict, List, Optional, Tuple

import requests
import feedparser
import yaml
import yfinance as yf
import pandas as pd
from bs4 import BeautifulSoup
from dateutil import tz


DEFAULT_TIMEOUT = 25
DEFAULT_UA = "Mozilla/5.0 (compatible; InvestMonitorBot/2.0; +https://github.com/)"
HEADERS = {
    "User-Agent": os.getenv("HTTP_USER_AGENT", DEFAULT_UA),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

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


def save_state(path: str, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def kst_now_str(tz_name: str) -> str:
    tzone = tz.gettz(tz_name)
    return datetime.now(tzone).strftime("%Y-%m-%d %H:%M:%S %Z")


def today_in_tz(tz_name: str) -> date:
    tzone = tz.gettz(tz_name)
    return datetime.now(tzone).date()


def http_get(url: str, timeout: int = DEFAULT_TIMEOUT) -> requests.Response:
    last_exc = None
    for i in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            return resp
        except Exception as e:
            last_exc = e
            time.sleep(1 + i)
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

        full_url = normalize_url(href)
        if full_url.startswith("/"):
            full_url = "https://www.businesswire.com" + full_url

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

        full_url = normalize_url(href)
        if full_url.startswith("/"):
            full_url = "https://www.businesswire.com" + full_url
        if full_url in seen:
            continue

        items.append(NewsItem(title=title, url=full_url, source="Business Wire"))
        seen.add(full_url)
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


# ------------------------
# Main
# ------------------------


def main() -> None:
    cfg = load_yaml("config.yaml")
    tz_name = cfg.get("timezone") or "Asia/Seoul"

    report_cfg = cfg.get("report") or {}
    max_news = int(report_cfg.get("max_news_items") or 8)
    use_details = bool(report_cfg.get("use_collapsible") if "use_collapsible" in report_cfg else True)

    alert_cfg = cfg.get("alerts") or {}
    alert_price_pct = float(alert_cfg.get("price_move_pct") or 8.0)
    alert_volume_x = float(alert_cfg.get("volume_spike_x") or 2.5)

    companies = cfg.get("companies") or []
    tickers = [c["ticker"] for c in companies]

    state_path = os.path.join("data", "state.json")
    state = load_state(state_path)
    state.setdefault("press_seen", {})
    state.setdefault("sec_seen", {})

    # 1) price data
    price_map = fetch_prices(tickers)

    now_kst = kst_now_str(tz_name)
    today = today_in_tz(tz_name)

    dashboard_rows: List[dict] = []
    details_blocks: List[str] = []

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
        if flags:
            alert_tickers.append(t)

        # --- Dashboard row ---
        dashboard_rows.append(
            {
                "ticker": t,
                "close": close,
                "pct": pct,
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

        # --- Update state ---
        merged_press = list(dict.fromkeys([it.url for it in press_items] + list(seen_urls)))
        state["press_seen"][t] = merged_press[:600]

        try:
            merged_sec = list(dict.fromkeys([it.url for it in sec_items] + list(state["sec_seen"].get(t, []))))
            state["sec_seen"][t] = merged_sec[:600]
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
    lines.append("| Ticker | Close | Δ% | MktCap | EV | Net Cash (anchor) | Runway | Next Catalyst | PR | SEC | Alert |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---|")
    for r in dashboard_rows:
        close_s = format_price(r.get("close"))
        pct_s = format_pct(r.get("pct"))
        lines.append(
            f"| {r['ticker']} | {close_s} | {pct_s} | {r['mktcap']} | {r['ev']} | {r['net_cash']} | {r['runway']} | {r['next']} | {r['new_pr']} | {r['new_sec']} | {r['alert']} |"
        )

    lines.append("")
    lines.append("### Details")
    lines.append("")
    lines.extend(details_blocks)

    report_md = "\n".join(lines)

    # Post to GitHub Discussions
    post_report_to_discussions(state, report_md)

    # Save state
    save_state(state_path, state)

    print("DONE. Report posted to GitHub Discussions.")


if __name__ == "__main__":
    main()

