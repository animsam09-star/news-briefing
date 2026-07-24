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

stats = {"naver_queries": 0, "rss_queries": 0, "rejected_out_of_window": 0,
         "decode_fail": 0, "fetch_errors": [], "no_pubdate": 0}


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
    """'yesterday 22:00' / 'today 06:10' / 'last_friday 14:00' -> aware datetime(KST)"""
    day_expr, hm = expr.split(" ")
    hh, mm = map(int, hm.split(":"))
    base = now_kst.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if day_expr == "today":
        return base
    if day_expr == "yesterday":
        return base - timedelta(days=1)
    if day_expr == "last_friday":
        # 월요일 실행 기준 직전 금요일
        delta = (now_kst.weekday() - 4) % 7 or 7
        return base - timedelta(days=delta)
    raise ValueError(expr)


def compute_windows(slot_cfg, now_kst):
    is_monday = now_kst.weekday() == 0
    key = "monday" if is_monday else "weekday"
    w1 = slot_cfg["window_m1"][key]
    windows = {"m1": (parse_kst_anchor(w1[0], now_kst), parse_kst_anchor(w1[1], now_kst))}
    if "window_m3_hours" in slot_cfg:
        end = parse_kst_anchor(slot_cfg["window_m3_end"], now_kst)
        windows["m3"] = (end - timedelta(hours=slot_cfg["window_m3_hours"]), end)
    return windows, is_monday


def in_window(dt, window):
    return window[0] <= dt.astimezone(KST) <= window[1]


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
        src, allowed = media_name(orig, domain_map)
        # 네이버뉴스 링크만 있으면 네이버뉴스로 인정
        if not allowed and "naver.com" in (it.get("link") or ""):
            src2, ok2 = media_name(it["link"], domain_map)
            if ok2:
                src, allowed, orig = src2, True, it["link"]
        out.append({
            "title": strip_tags(it["title"]),
            "url": orig,
            "source": src,
            "allowed_media": allowed,
            "pub_kst": pub.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
            "matched_query": query,
        })
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
    windows, is_monday = compute_windows(slot_cfg, now_kst)

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
        "is_monday": is_monday,
        "windows_kst": {k: [w[0].strftime("%Y-%m-%d %H:%M"), w[1].strftime("%Y-%m-%d %H:%M")]
                        for k, w in windows.items()},
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
