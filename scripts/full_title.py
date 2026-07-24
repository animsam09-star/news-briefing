# -*- coding: utf-8 -*-
"""네이버 API가 절단한 기사 제목을 원문 페이지의 og:title로 복원.

사용: python full_title.py <url1> [url2 ...]
출력: 한 줄당 JSON {"url": ..., "title": ... | null}

n.news.naver.com 링크를 우선 넘길 것 (봇 차단 없음·UTF-8 고정).
"""
import html
import json
import re
import sys
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

META_PATTERNS = [
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
    r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']',
    r"<title[^>]*>([^<]+)</title>",
]


def page_title(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read(200_000)
            ctype = r.headers.get("Content-Type", "")
    except Exception:
        return None
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    encodings = [m.group(1)] if m else []
    head = raw[:2000].decode("ascii", errors="ignore")
    m2 = re.search(r'charset=["\']?([\w-]+)', head, re.I)
    if m2:
        encodings.append(m2.group(1))
    encodings += ["utf-8", "cp949", "euc-kr"]
    text = None
    for enc in encodings:
        try:
            text = raw.decode(enc)
            break
        except Exception:
            continue
    if text is None:
        return None
    for pat in META_PATTERNS:
        m3 = re.search(pat, text, re.I | re.S)
        if m3:
            t = html.unescape(m3.group(1)).strip()
            if t:
                return re.sub(r"\s+", " ", t)
    return None


if __name__ == "__main__":
    for url in sys.argv[1:]:
        print(json.dumps({"url": url, "title": page_title(url)}, ensure_ascii=False))
