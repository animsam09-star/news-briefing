# -*- coding: utf-8 -*-
"""뉴스 브리핑 기계적 수집기 (GitHub Actions용).

방식1: 네이버 뉴스 검색 API (pubDate 기반 시간 윈도우 필터, 원문 URL 즉시 확보)
방식3: Google News RSS (when:1d 필터 + pubDate 재검증, 경유 URL -> 원문 디코딩)

날짜 검증은 전부 이 스크립트가 수행한다 (LLM 판단 개입 금지 원칙의 코드화).
출력: pool.json — 프롬프트(Claude)는 이 풀에서 선별만 한다.

사용:
  python collect.py --slot morning --out pool.json
  python collect.py --slot morning --adhoc-naver "PF 대출" --category 건설      # 폴백 재검색(방식1)
  python collect.py --slot morning --adhoc-rss "natural gas exports" --theme "#LNG"  # 폴백 재검색(방식3)

환경변수: NAVER_CLIENT_ID, NAVER_CLIENT_SECRET (방식1에 필수)
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from datetime import timezone
from email.utils import parsedate_to_datetime

KST = timezone(timedelta(hours=9), "KST")  # 한국은 DST 없음 — 고정 오프셋으로 tzdata 의존성 제거
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "slots.json")
SENT_PATH = os.path.join(os.path.dirname(__file__), "..", "state", "sent.json")
SENT_RETENTION_HOURS = 96  # 금요일 저녁 발송분이 월요일 아침까지 남도록

stats = {"naver_queries": 0, "rss_queries": 0, "rejected_out_of_window": 0,
         "decode_fail": 0, "fetch_errors": [], "no_pubdate": 0,
         "rejected_already_sent": 0, "sent_history_size": 0}

# 직전 슬롯들이 이미 발송한 기사 (state/sent.json에서 로드) — 슬롯 간 중복 차단용
# SENT_URLS/SENT_TITLES: 기계적 하드 필터 (거의 동일한 제목·URL을 수집 단계에서 제거)
# SENT_RECENT: 프롬프트에 그대로 주입 — "같은 사건인가" 판단은 코드가 못 하므로 LLM에 맡긴다
SENT_URLS, SENT_TITLES = set(), set()
SENT_RECENT = []
SENT_INJECT_HOURS = 48   # 프롬프트에 보여줄 범위 (직전 3~4개 슬롯)
SENT_INJECT_MAX = 80     # 프롬프트 길이 방어


def fetch(url, data=None, headers=None, timeout=25):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def parse_kst_anchor(expr, now_kst):
    """'yesterday 22:00' / 'today 06:10' / 'prev_run_day 21:20' -> aware datetime(KST)"""
    day_expr, hm = expr.split(" ")
    hh, mm = map(int, hm.split(":"))
    base = now_kst.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if day_expr == "today":
        return base
    if day_expr == "yesterday":
        return base - timedelta(days=1)
    if day_expr == "prev_run_day":
        # 직전에 실제로 발송이 있었던 날. 토요일(KST)은 발송을 생략하므로
        # 어제가 토요일이면(=오늘이 일요일) 하루 더 거슬러 금요일이 직전 실행일이다.
        # 이 앵커 덕에 월요일·일요일 특례가 따로 필요 없다 — 주말 확장 윈도우는
        # 일요일 morning 한 곳에만 자동으로 생긴다.
        back = 2 if (now_kst - timedelta(days=1)).weekday() == 5 else 1
        return base - timedelta(days=back)
    raise ValueError(expr)


def compute_windows(slot_cfg, now_kst):
    """슬롯 윈도우 = [직전 실행 슬롯의 종료 시각, 이번 슬롯의 종료 시각].

    각 슬롯의 시작점이 직전 슬롯의 끝과 정확히 맞물리므로 어떤 시각도
    두 번 수집되지 않는다(중첩 0). 요일별 특례는 prev_run_day 앵커가 흡수한다.
    """
    w1 = slot_cfg["window_m1"]
    windows = {"m1": (parse_kst_anchor(w1[0], now_kst), parse_kst_anchor(w1[1], now_kst))}
    if "window_m3_hours" in slot_cfg:
        end = parse_kst_anchor(slot_cfg["window_m3_end"], now_kst)
        windows["m3"] = (end - timedelta(hours=slot_cfg["window_m3_hours"]), end)
    return windows


def in_window(dt, window):
    return window[0] <= dt.astimezone(KST) <= window[1]


# 제목 앞의 [속보]·[단독]·(종합)·【…】 류 말머리 — 같은 사건인데 지문이 어긋나는 주범
_LEAD_TAG_RE = re.compile(r"^\s*(?:[\[\(<【][^\]\)>】]{0,20}[\]\)>】]\s*)+")


def norm_title(t):
    """매체별 표기 차이를 흡수한 제목 지문 (말머리·공백·문장부호 제거 후 앞 40자)."""
    t = _LEAD_TAG_RE.sub("", t or "")
    return re.sub(r"[\s\W_]+", "", t, flags=re.UNICODE).lower()[:40]


def load_sent_history(now_kst):
    """직전 슬롯들이 발송한 기사 지문을 로드. 파일이 없으면 조용히 빈 집합."""
    global SENT_URLS, SENT_TITLES
    try:
        with open(SENT_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    cutoff = now_kst - timedelta(hours=SENT_RETENTION_HOURS)
    inject_cutoff = now_kst - timedelta(hours=SENT_INJECT_HOURS)
    kept, recent = 0, []
    for e in data.get("sent", []):
        try:
            ts = datetime.strptime(e["sent_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        except (KeyError, ValueError):
            continue
        if ts < cutoff:
            continue
        kept += 1
        u = (e.get("url") or "").split("?")[0]
        if u:
            SENT_URLS.add(u)
        t = norm_title(e.get("title"))
        if t:
            SENT_TITLES.add(t)
        if ts >= inject_cutoff and e.get("title"):
            recent.append((ts, {"slot": e.get("slot", ""),
                                "sent_kst": e["sent_kst"],
                                "title": e["title"]}))
    recent.sort(key=lambda p: p[0], reverse=True)
    SENT_RECENT[:] = [r for _, r in recent[:SENT_INJECT_MAX]]
    stats["sent_history_size"] = kept


def already_sent(url, title):
    """직전 슬롯에서 이미 나간 기사인가. 같은 사건의 타 매체 재보도도 제목 지문으로 잡힌다."""
    if url and url.split("?")[0] in SENT_URLS:
        return True
    return norm_title(title) in SENT_TITLES


def domain_of(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def media_name(url, domain_map):
    d = domain_of(url)
    if d in domain_map:
        return domain_map[d], True
    # 서브도메인 매칭 (예: realestate.mk.co.kr -> mk.co.kr)
    for known, name in domain_map.items():
        if d.endswith("." + known):
            return name, True
    return d, False


# ─── 방식1: 네이버 뉴스 API ───────────────────────────────────────

def naver_search(query, window, domain_map, display=50):
    cid = os.environ.get("NAVER_CLIENT_ID", "")
    csec = os.environ.get("NAVER_CLIENT_SECRET", "")
    if not (cid and csec):
        raise RuntimeError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수 필요")
    url = ("https://openapi.naver.com/v1/search/news.json?query="
           + urllib.parse.quote(query) + f"&display={display}&sort=date")
    stats["naver_queries"] += 1
    try:
        body = json.loads(fetch(url, headers={
            "X-Naver-Client-Id": cid, "X-Naver-Client-Secret": csec}))
    except Exception as e:
        stats["fetch_errors"].append(f"naver:{query}:{e}")
        return []
    out = []
    for it in body.get("items", []):
        try:
            pub = parsedate_to_datetime(it["pubDate"])
        except Exception:
            stats["no_pubdate"] += 1
            continue
        if not in_window(pub, window):
            stats["rejected_out_of_window"] += 1
            continue
        orig = it.get("originallink") or it.get("link")
        if already_sent(orig, strip_tags(it["title"])):
            stats["rejected_already_sent"] += 1
            continue
        src, allowed = media_name(orig, domain_map)
        # 네이버뉴스 링크만 있으면 네이버뉴스로 인정
        if not allowed and "naver.com" in (it.get("link") or ""):
            src2, ok2 = media_name(it["link"], domain_map)
            if ok2:
                src, allowed, orig = src2, True, it["link"]
        title = strip_tags(it["title"])
        rec = {
            "title": title,
            "url": orig,
            "source": src,
            "allowed_media": allowed,
            "pub_kst": pub.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
            "matched_query": query,
        }
        # 네이버 API는 긴 제목을 "..."로 절단해 반환 → 복원 대상 표시 + 네이버뉴스 링크 보존
        if title.endswith("..."):
            rec["title_truncated"] = True
        nlink = it.get("link") or ""
        if "naver.com" in nlink and nlink != orig:
            rec["naver_link"] = nlink
        out.append(rec)
    return out


# ─── 방식3: Google News RSS + 경유 URL 디코딩 ─────────────────────

def gnews_rss(query, window, lang="en-US", gl="US", ceid="US:en", when="1d"):
    q = urllib.parse.quote(f"{query} when:{when}")
    url = f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={gl}&ceid={ceid}"
    stats["rss_queries"] += 1
    try:
        xml_text = fetch(url)
        root = ET.fromstring(xml_text)
    except Exception as e:
        stats["fetch_errors"].append(f"rss:{query}:{e}")
        return []
    out = []
    for item in root.iter("item"):
        title = strip_tags(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        pub_raw = item.findtext("pubDate") or ""
        src_el = item.find("source")
        source = strip_tags(src_el.text if src_el is not None else "")
        try:
            pub = parsedate_to_datetime(pub_raw)
        except Exception:
            stats["no_pubdate"] += 1
            continue
        if not in_window(pub, window):
            stats["rejected_out_of_window"] += 1
            continue
        # 이 시점에는 경유 URL뿐이라 URL 대조는 불가 — 제목 지문으로만 걸러진다
        if already_sent(None, title):
            stats["rejected_already_sent"] += 1
            continue
        out.append({"title": title, "gnews_url": link, "source": source,
                    "pub_kst": pub.astimezone(KST).strftime("%Y-%m-%d %H:%M")})
    return out


def decode_gnews_url(source_url):
    """news.google.com/rss/articles/<id> -> 원문 URL (batchexecute 방식)"""
    m = re.search(r"articles/([^?/]+)", source_url)
    if not m:
        return None
    art_id = m.group(1)
    try:
        page = fetch(f"https://news.google.com/rss/articles/{art_id}")
        sg = re.search(r'data-n-a-sg="([^"]+)"', page)
        ts = re.search(r'data-n-a-ts="([^"]+)"', page)
        if not (sg and ts):
            return None
        payload = [
            "Fbv4je",
            f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{art_id}",{ts.group(1)},"{sg.group(1)}"]',
        ]
        body = urllib.parse.urlencode({"f.req": json.dumps([[payload]])}).encode()
        resp = fetch("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                     data=body,
                     headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
        for line in resp.splitlines():
            if "garturlres" in line or "Fbv4je" in line:
                try:
                    arr = json.loads(line)
                    return json.loads(arr[0][2])[1]
                except Exception:
                    continue
    except Exception:
        pass
    return None


def dedup(articles, key="url"):
    seen_url, seen_title, out = set(), set(), []
    for a in articles:
        u = (a.get(key) or a.get("gnews_url") or "").split("?")[0]
        t = re.sub(r"\s+", "", a["title"])[:40]
        if u and u in seen_url or t in seen_title:
            continue
        seen_url.add(u)
        seen_title.add(t)
        out.append(a)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", required=True,
                    choices=["morning", "midmorning", "afternoon", "evening"])
    ap.add_argument("--out", default="pool.json")
    ap.add_argument("--adhoc-naver", help="방식1 폴백: 단일 쿼리 실행 후 stdout JSON")
    ap.add_argument("--adhoc-rss", help="방식3 폴백: 단일 쿼리 실행 후 stdout JSON")
    ap.add_argument("--category", default="건설")
    ap.add_argument("--theme", default="")
    ap.add_argument("--decode-per-theme", type=int, default=4,
                    help="테마당 URL 디코딩 상한 (시간 절약)")
    args = ap.parse_args()

    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    slot_cfg = cfg["slots"][args.slot]
    domain_map = cfg["allowed_media_domains"]
    now_kst = datetime.now(KST)
    windows = compute_windows(slot_cfg, now_kst)
    load_sent_history(now_kst)

    # ── 폴백(adhoc) 모드: 결과를 stdout으로만 출력 ──
    if args.adhoc_naver:
        arts = dedup(naver_search(args.adhoc_naver, windows["m1"], domain_map))
        print(json.dumps({"category": args.category, "articles": arts,
                          "stats": stats}, ensure_ascii=False, indent=1))
        return
    if args.adhoc_rss:
        w = windows.get("m3") or windows["m1"]
        arts = gnews_rss(args.adhoc_rss, w)
        for a in arts[: args.decode_per_theme]:
            u = decode_gnews_url(a["gnews_url"])
            if u:
                a["url"] = u
            else:
                stats["decode_fail"] += 1
        arts = [a for a in arts if a.get("url")]
        print(json.dumps({"theme": args.theme, "articles": dedup(arts),
                          "stats": stats}, ensure_ascii=False, indent=1))
        return

    pool = {
        "slot": args.slot,
        "generated_kst": now_kst.strftime("%Y-%m-%d %H:%M"),
        "windows_kst": {k: [w[0].strftime("%Y-%m-%d %H:%M"), w[1].strftime("%Y-%m-%d %H:%M")]
                        for k, w in windows.items()},
        "sent_history_size": stats["sent_history_size"],
        # 직전 슬롯들이 이미 발송한 기사 제목. 제목·URL이 거의 같은 건은 위에서 이미
        # 걸러졌고, 여기 남은 건 "같은 사건을 다룬 다른 기사"를 프롬프트가 판단해
        # 제외하라고 넘기는 목록이다 (§7 슬롯 간 중복 제거).
        "recently_sent": SENT_RECENT,
    }

    # ── 방식1: 네이버 API ──
    if "method1" in slot_cfg["methods"]:
        kw = cfg["method1_keywords"]
        m1 = {"건설": [], "금융": []}
        for q in kw["건설"]:
            m1["건설"].extend(naver_search(q, windows["m1"], domain_map))
            time.sleep(0.15)
        for tier in ("금융_1차", "금융_2차", "금융_3차"):
            for q in kw[tier]:
                arts = naver_search(q, windows["m1"], domain_map)
                for a in arts:
                    a["tier"] = tier
                m1["금융"].extend(arts)
                time.sleep(0.15)
        pool["method1"] = {k: dedup(v) for k, v in m1.items()}

    # ── 방식3: Google News RSS ──
    if "method3" in slot_cfg["methods"]:
        m3 = {}
        for theme, queries in cfg["method3_themes"].items():
            acc = []
            for q in queries:
                acc.extend(gnews_rss(q, windows["m3"]))
            acc = dedup(acc, key="gnews_url")
            # 최신순 상위 N건만 원문 URL 디코딩 (2건 목표 + 여유분)
            acc.sort(key=lambda a: a["pub_kst"], reverse=True)
            decoded = []
            for a in acc[: args.decode_per_theme]:
                u = decode_gnews_url(a["gnews_url"])
                if u:
                    a["url"] = u
                    a.pop("gnews_url", None)
                    decoded.append(a)
                else:
                    stats["decode_fail"] += 1
            m3[theme] = decoded
        pool["method3"] = m3

    pool["collector_stats"] = stats
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(pool, f, ensure_ascii=False, indent=1)

    # 요약 로그
    if "method1" in pool:
        print(f"[방식1] 건설 {len(pool['method1']['건설'])}건 / 금융 {len(pool['method1']['금융'])}건", file=sys.stderr)
    if "method3" in pool:
        n = sum(len(v) for v in pool["method3"].values())
        nonzero = sum(1 for v in pool["method3"].values() if v)
        print(f"[방식3] {nonzero}/13테마 {n}건", file=sys.stderr)
    print(f"[stats] {json.dumps(stats, ensure_ascii=False)}", file=sys.stderr)


if __name__ == "__main__":
    main()
