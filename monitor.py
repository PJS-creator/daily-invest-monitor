\
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
        return {"last_run": None, "press_seen": {}, "sec_seen": {}}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path: str, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def kst_now_str(tz_name: str) -> str:
    tzone = tz.gettz(tz_name)
    return datetime.now(tzone).strftime("%Y-%m-%d %H:%M:%S %Z")


def http_get(url: str, timeout: int = DEFAULT_TIMEOUT) -> requests.Response:
    # 간단한 재시도
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


def parse_globenewswire_org(url: str, limit: int = 8) -> List[NewsItem]:
    """GlobeNewswire 'search/organization/...' 페이지에서 최근 PR 목록을 파싱."""
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


def parse_businesswire_seed(url: str, limit: int = 8) -> List[NewsItem]:
    """Business Wire 기사 하단의 'More News From <Company>' 섹션 링크를 추출."""
    resp = http_get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"BusinessWire fetch failed: {resp.status_code}")
    soup = BeautifulSoup(resp.text, "html.parser")

    # 'More News From' 섹션은 보통 본문 하단에 링크가 모여있습니다.
    # 완벽하지 않지만, 새 PR이 생기면 이 섹션에 최신 몇 개가 뜨는 경우가 많아 daily 체크용으로 유용합니다.
    items: List[NewsItem] = []
    seen = set()

    # 기사 본문 내 링크 중 businesswire news/home 링크만 추출
    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = a.get_text(" ", strip=True)
        if not title:
            continue
        if "businesswire.com/news/home/" not in href:
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
            # 상대경로면 도메인 붙이기
            m = re.match(r"^(https?://[^/]+)", url)
            if m:
                full_url = m.group(1) + full_url
        if full_url in seen:
            continue

        # 날짜 추정: 같은 컨테이너 텍스트에서 YYYY 또는 Month 찾기
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
    """company.sources에 정의된 순서대로 시도해서 press release item 목록을 반환."""
    errors: List[str] = []
    sources = company.get("sources") or []
    all_items: List[NewsItem] = []

    for src in sources:
        stype = (src.get("type") or "").strip()
        url = (src.get("url") or "").strip()
        if not stype or not url:
            continue
        try:
            if stype == "globenewswire_org":
                items = parse_globenewswire_org(url)
            elif stype == "businesswire_seed":
                items = parse_businesswire_seed(url)
            else:
                items = parse_generic_html_news(url)

            if items:
                all_items = items
                break
        except Exception as e:
            errors.append(f"{stype} {url} -> {e}")

    return all_items, errors


def fetch_prices(tickers: List[str]) -> Dict[str, dict]:
    """yfinance로 최근 2개 거래일 종가/거래량을 받아서 일간 변동을 계산."""
    result: Dict[str, dict] = {}

    # 10d 정도 받으면 휴일/주말 있어도 2개 거래일 확보 가능
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
        # ticker 1개인 경우 단일 컬럼
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
        # 안전하게 필요한 것만
        out["market_cap"] = info.get("marketCap")
        out["fifty_two_week_low"] = info.get("fiftyTwoWeekLow")
        out["fifty_two_week_high"] = info.get("fiftyTwoWeekHigh")
        out["avg_volume"] = info.get("averageVolume")
    except Exception as e:
        out["fund_error"] = str(e)
    return out


def sec_fetch_atom_by_ticker(ticker: str, limit: int = 6) -> List[NewsItem]:
    """
    SEC Atom feed(공시) - press release가 막히는 경우 대체로 유용.
    내부적으로 SEC의 company_tickers.json을 사용해 ticker->CIK를 찾습니다.
    """
    # 1) ticker->CIK 매핑
    mapping_url = "https://www.sec.gov/files/company_tickers.json"
    resp = requests.get(mapping_url, headers={"User-Agent": SEC_UA}, timeout=DEFAULT_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"SEC mapping fetch failed: {resp.status_code}")
    mapping = resp.json()

    cik = None
    for _, row in mapping.items():
        if str(row.get("ticker", "")).upper() == ticker.upper():
            cik = str(row.get("cik_str")).zfill(10)
            break
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
    # 보기 좋게: B/M
    if abs(n) >= 1_000_000_000:
        return f"${n/1_000_000_000:.2f}B"
    if abs(n) >= 1_000_000:
        return f"${n/1_000_000:.2f}M"
    return f"${n}"


def send_email(subject: str, body: str) -> None:
    import smtplib
    from email.message import EmailMessage

    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT") or "587")
    user = os.getenv("SMTP_USER")
    pw = os.getenv("SMTP_PASS")
    to_addr = os.getenv("EMAIL_TO")
    from_addr = os.getenv("EMAIL_FROM") or user

    if not all([host, port, user, pw, to_addr, from_addr]):
        raise RuntimeError("Missing email env vars. Check GitHub Secrets.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)

    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, pw)
        s.send_message(msg)


def send_telegram(subject: str, body: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID secrets.")

    text = f"*{subject}*\n\n{body}"
    # Telegram message length limit (약 4096). 길면 잘라서 전송.
    if len(text) > 3900:
        text = text[:3900] + "\n\n...(truncated)"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=DEFAULT_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"Telegram send failed: {resp.status_code} {resp.text[:200]}")


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

    # 2) 회사별 PR/SEC 수집
    report_lines: List[str] = []
    report_lines.append(f"Daily Investment Monitor ({kst_now_str(tz_name)})")
    report_lines.append("=" * 60)
    report_lines.append("")

    total_new_press = 0
    total_new_sec = 0

    for c in companies:
        t = c["ticker"]
        name = c.get("name") or t

        report_lines.append(f"[{t}] {name}")
        report_lines.append("-" * 60)

        # Price block
        p = price_map.get(t, {})
        if "error" in p:
            report_lines.append(f"가격 데이터 오류: {p['error']}")
        else:
            fund = fetch_fundamentals(t)
            close = p.get("close")
            prev_close = p.get("prev_close")
            chg = p.get("chg")
            pct = p.get("pct")
            vol = p.get("volume")
            dt = p.get("last_date")

            report_lines.append(f"종가({dt}): {close:.2f}  (전일 {prev_close:.2f})  변동: {chg:+.2f} ({pct:+.2f}%)")
            if vol and not pd.isna(vol):
                report_lines.append(f"거래량: {int(vol):,}")
            if fund:
                report_lines.append(f"시총: {format_money(fund.get('market_cap'))} | 52주: {fund.get('fifty_two_week_low', '-')}-{fund.get('fifty_two_week_high', '-')}")
                av = fund.get("avg_volume")
                if av:
                    report_lines.append(f"평균거래량(avg): {int(av):,}")

        # Checklist
        checklist = c.get("checklist") or []
        if checklist:
            report_lines.append("")
            report_lines.append("오늘 체크할 항목:")
            for item in checklist:
                report_lines.append(f"  - {item}")

        # Press releases (from configured sources)
        report_lines.append("")
        report_lines.append("보도자료/뉴스(Press Releases):")
        press_items, press_errors = fetch_press_items(c)
        seen_urls = set(state["press_seen"].get(t, []))
        new_press = [it for it in press_items if it.url not in seen_urls]

        if new_press:
            total_new_press += len(new_press)
            for it in new_press[:8]:
                dt = f" ({it.date_text})" if it.date_text else ""
                report_lines.append(f"  + {it.title}{dt}")
                report_lines.append(f"    {it.url}")
        else:
            report_lines.append("  (신규 없음)")

        # SEC filings (always try; useful fallback)
        report_lines.append("")
        report_lines.append("SEC 공시(EDGAR Atom):")
        try:
            time.sleep(0.25)  # SEC에 너무 빠르게 요청하지 않기
            sec_items = sec_fetch_atom_by_ticker(t)
            sec_seen = set(state["sec_seen"].get(t, []))
            new_sec = [it for it in sec_items if it.url not in sec_seen]
            if new_sec:
                total_new_sec += len(new_sec)
                for it in new_sec[:6]:
                    dt = f" ({it.date_text})" if it.date_text else ""
                    report_lines.append(f"  + {it.title}{dt}")
                    report_lines.append(f"    {it.url}")
            else:
                report_lines.append("  (신규 없음)")
        except Exception as e:
            report_lines.append(f"  SEC 조회 실패: {e}")

        # Debug: errors
        if press_errors:
            report_lines.append("")
            report_lines.append("DEBUG(보도자료 소스 오류):")
            for err in press_errors[:3]:
                report_lines.append(f"  - {err}")

        report_lines.append("")
        report_lines.append("")

        # Update state: mark the fetched items as seen (press + sec)
        # press
        merged_press = list(dict.fromkeys([it.url for it in press_items] + list(seen_urls)))
        state["press_seen"][t] = merged_press[:300]
        # sec
        try:
            merged_sec = list(dict.fromkeys([it.url for it in sec_items] + list(sec_seen)))  # type: ignore
            state["sec_seen"][t] = merged_sec[:300]
        except Exception:
            # sec_items 없으면 그대로
            state["sec_seen"][t] = list(sec_seen)[:300]

    state["last_run"] = datetime.now(timezone.utc).isoformat()

    body = "\n".join(report_lines)
    subject = f"[Daily Monitor] {total_new_press} new PR / {total_new_sec} new SEC"

    # 3) 알림 전송
    method = (os.getenv("ALERT_METHOD") or "email").strip().lower()
    if method == "telegram":
        send_telegram(subject, body)
    else:
        send_email(subject, body)

    # 4) state 저장
    save_state(state_path, state)

    print("DONE. Report sent.")


if __name__ == "__main__":
    main()
