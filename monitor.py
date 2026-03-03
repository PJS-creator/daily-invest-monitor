import os
import re
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
import feedparser
import yaml
import yfinance as yf
import pandas as pd
from bs4 import BeautifulSoup
from dateutil import tz


DEFAULT_TIMEOUT = 25
DEFAULT_UA = "Mozilla/5.0 (compatible; InvestMonitorBot/1.0; +https://github.com/)"
HEADERS = {
    "User-Agent": os.getenv("HTTP_USER_AGENT", DEFAULT_UA),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SEC_UA = os.getenv("SEC_USER_AGENT") or HEADERS["User-Agent"]

MONTHS = "(January|February|March|April|May|June|July|August|September|October|November|December)"
GLOBE_DATE_RE = re.compile(rf"{MONTHS}\s+\d{{1,2}},\s+\d{{4}}\s+\d{{1,2}}:\d{{2}}\s+ET")

# SEC ticker->CIK 캐시 (한 번만 다운로드)
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

        # 날짜는 주변 텍스트에서 추정
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

    # 검색 결과 리스트 (구버전 BW 페이지 기준)
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

    # fallback: 페이지 내에서 news/home 링크를 최대한 추출
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

    # 우선순위 1: h2/h3 안의 a 링크
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
            m1 = re.search(r"\b20\d{2}\b", text)
            m2 = re.search(rf"{MONTHS}\s+\d{{1,2}},\s+20\d{{2}}", text)
            if m2:
                date_text = m2.group(0)
            elif m1:
                date_text = m1.group(0)

        items.append(NewsItem(title=title, url=full_url, date_text=date_text, source=url))
        seen.add(full_url)
        if len(items) >= limit:
            return items

    # 우선순위 2: 모든 a 링크 중 'news'/'press' 키워드 포함
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


def fetch_press_items(company: dict) -> Tuple[List[NewsItem], List[str]]:
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
                items = parse_globenewswire_page(url)
            elif stype == "businesswire_search":
                items = parse_businesswire_search(url)
            else:
                items = parse_generic_html_news(url)

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

    return deduped, errors


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
    """추가 지표(시총/52주 등). 실패해도 report는 계속."""
    out = {}
    try:
        info = yf.Ticker(ticker).info or {}
        out["market_cap"] = info.get("marketCap")
        out["fifty_two_week_low"] = info.get("fiftyTwoWeekLow")
        out["fifty_two_week_high"] = info.get("fiftyTwoWeekHigh")
        out["avg_volume"] = info.get("averageVolume")
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


def format_money(n: Optional[int]) -> str:
    if not n:
        return "-"
    try:
        n = int(n)
    except Exception:
        return "-"
    if abs(n) >= 1_000_000_000:
        return f"${n/1_000_000_000:.2f}B"
    if abs(n) >= 1_000_000:
        return f"${n/1_000_000:.2f}M"
    return f"${n}"


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

    # 1) state에 저장된 discussion_id가 있으면 그걸 우선 사용
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

    # category 선택
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

    # 기존 discussion 찾기 (최근 50개에서 title 매칭)
    nodes = (r.get("discussions") or {}).get("nodes") or []
    for d in nodes:
        if str(d.get("title", "")).strip() == title.strip():
            state["discussion_id"] = d.get("id")
            return str(d.get("id"))

    # 없으면 생성
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
    companies = cfg.get("companies") or []
    tickers = [c["ticker"] for c in companies]

    state_path = os.path.join("data", "state.json")
    state = load_state(state_path)
    state.setdefault("press_seen", {})
    state.setdefault("sec_seen", {})

    # 1) 가격/거래량
    price_map = fetch_prices(tickers)

    # 2) 회사별 PR/SEC 수집 + Markdown 리포트
    now_kst = kst_now_str(tz_name)
    report_lines: List[str] = []
    report_lines.append(f"## Daily Investment Monitor ({now_kst})")
    report_lines.append("")

    total_new_press = 0
    total_new_sec = 0

    for c in companies:
        t = c["ticker"]
        name = c.get("name") or t

        report_lines.append(f"### {t} — {name}")

        # Price block
        p = price_map.get(t, {})
        if "error" in p:
            report_lines.append(f"- ⚠️ **가격 데이터 오류:** {p['error']}")
        else:
            fund = fetch_fundamentals(t)
            close = p.get("close")
            prev_close = p.get("prev_close")
            chg = p.get("chg")
            pct = p.get("pct")
            vol = p.get("volume")
            dt = p.get("last_date")

            report_lines.append(
                f"- **종가({dt})**: `{close:.2f}` (전일 `{prev_close:.2f}`) → **{chg:+.2f} ({pct:+.2f}%)**"
            )
            if vol and not pd.isna(vol):
                report_lines.append(f"- **거래량**: `{int(vol):,}`")
            if fund:
                report_lines.append(
                    f"- **시총**: {format_money(fund.get('market_cap'))} | **52주**: {fund.get('fifty_two_week_low', '-')}-{fund.get('fifty_two_week_high', '-') }"
                )
                av = fund.get("avg_volume")
                if av:
                    report_lines.append(f"- **평균거래량(avg)**: `{int(av):,}`")

        # Checklist
        checklist = c.get("checklist") or []
        if checklist:
            report_lines.append("")
            report_lines.append("**오늘 체크할 항목**")
            for item in checklist:
                report_lines.append(f"- {item}")

        # Press releases
        report_lines.append("")
        report_lines.append("**보도자료/뉴스 (Press Releases)**")
        press_items, press_errors = fetch_press_items(c)

        seen_urls = set(state["press_seen"].get(t, []))
        new_press = [it for it in press_items if it.url not in seen_urls]

        if new_press:
            total_new_press += len(new_press)
            for it in new_press[:10]:
                dtxt = f" — _{it.source}_, {it.date_text}" if it.date_text else (f" — _{it.source}_" if it.source else "")
                report_lines.append(f"- [{it.title}]({it.url}){dtxt}")
        else:
            report_lines.append("- (신규 없음)")

        # SEC filings
        report_lines.append("")
        report_lines.append("**SEC 공시 (EDGAR Atom)**")
        sec_items: List[NewsItem] = []
        try:
            time.sleep(0.25)
            sec_items = sec_fetch_atom_by_ticker(t)
            sec_seen = set(state["sec_seen"].get(t, []))
            new_sec = [it for it in sec_items if it.url not in sec_seen]
            if new_sec:
                total_new_sec += len(new_sec)
                for it in new_sec[:8]:
                    dtxt = f" — {it.date_text}" if it.date_text else ""
                    report_lines.append(f"- [{it.title}]({it.url}){dtxt}")
            else:
                report_lines.append("- (신규 없음)")
        except Exception as e:
            report_lines.append(f"- ⚠️ SEC 조회 실패: {e}")

        # Debug errors
        if press_errors:
            report_lines.append("")
            report_lines.append("<details><summary>DEBUG(보도자료 소스 오류)</summary>")
            for err in press_errors[:5]:
                report_lines.append(f"- {err}")
            report_lines.append("</details>")

        report_lines.append("")

        # Update state: mark fetched items as seen
        merged_press = list(dict.fromkeys([it.url for it in press_items] + list(seen_urls)))
        state["press_seen"][t] = merged_press[:400]

        try:
            merged_sec = list(dict.fromkeys([it.url for it in sec_items] + list(state["sec_seen"].get(t, []))))
            state["sec_seen"][t] = merged_sec[:400]
        except Exception:
            pass

    state["last_run"] = datetime.now(timezone.utc).isoformat()

    # Header summary
    summary = f"**Summary:** {total_new_press} new PR / {total_new_sec} new SEC"
    report_md = "\n".join([summary, ""] + report_lines)

    # 3) GitHub Discussions에 댓글로 게시
    post_report_to_discussions(state, report_md)

    # 4) state 저장
    save_state(state_path, state)

    print("DONE. Report posted to GitHub Discussions.")


if __name__ == "__main__":
    main()
