#!/usr/bin/env python3
"""무료 단축기 후보들이 GitHub Actions 러너에서 실제로 되는지 찔러본다.

로컬(개발 컨테이너)에서는 답이 안 나온다. 프록시가 막고 있기도 하고, 무료 단축기는
**요청이 어느 IP 에서 오느냐**로 갈리기 때문이다 — 2026-09-07 에 같은 코드가 한
러너에서는 TinyURL 로 6/6 을 줄였고 다른 러너에서는 42건 전부 실패했다.

그래서 러너에서 돌려야 한다. 이 스크립트는 후보마다:
  - 서로 다른 URL 3개를 줄여 보고
  - 응답이 단축 URL 형태인지, 서로 다른 값인지(같은 값을 주는 고장 탐지)
  - 표본 1건이 원본으로 되돌아오는지(302 추적)
  - 그래서 최종 길이가 몇 자인지
를 표로 찍는다. 판단 근거를 남기는 게 목적이지 자동으로 뭘 바꾸지는 않는다.

**도메인 길이가 핵심이다.** 자체 Cloudflare Worker 는 잘 동작했지만
news-briefing-link.<계정>.workers.dev 라 57자였다 — 국내 매체 URL(중앙값 74자,
짧은 건 37자)에는 이득이 없거나 오히려 손해였고, 계정 아이디까지 링크에 실렸다.
그래서 짧은 도메인을 쓰는 서비스를 우선으로 본다.
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# 브리핑에 실제로 나가는 것과 같은 성격의 URL 로 시험한다. 짧은 것, 파라미터 붙은 것,
# 아주 긴 것을 섞었다 — 길이 제한이 있는 서비스가 있다.
SAMPLES = [
    "https://www.mk.co.kr/article/12146570",
    "https://n.news.naver.com/mnews/article/029/0003046683?sid=101",
    "https://www.paddleyourownkanoo.com/2026/09/07/delta-air-boeing-737-makes-"
    "emergency-descent-and-diverts-to-las-vegas-after-oxygen-masks-deploy/",
]


def get(url, timeout=15, method="GET", data=None, headers=None):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.geturl(), r.read(4096).decode("utf-8", errors="replace").strip()


def plain(template):
    """GET 한 방에 단축 URL 본문을 그대로 주는 흔한 형태."""
    def call(original):
        return get(template.format(urllib.parse.quote(original, safe="")))[2]
    return call


def spoo(original):
    body = urllib.parse.urlencode({"url": original}).encode()
    _, _, text = get("https://spoo.me/", method="POST", data=body,
                     headers={"Accept": "application/json",
                              "Content-Type": "application/x-www-form-urlencoded"})
    return json.loads(text).get("short_url", text)


def cleanuri(original):
    body = urllib.parse.urlencode({"url": original}).encode()
    _, _, text = get("https://cleanuri.com/api/v1/shorten", method="POST", data=body,
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    return json.loads(text).get("result_url", text)


def onept(original):
    body = json.dumps({"long": original}).encode()
    _, _, text = get("https://api.1pt.co/addURL", method="POST", data=body,
                     headers={"Content-Type": "application/json"})
    d = json.loads(text)
    return "https://1pt.co/" + d.get("short", "")


# (이름, 호출함수, 기대 도메인). 도메인이 짧은 순으로 놓았다.
CANDIDATES = [
    ("v.gd",      plain("https://v.gd/create.php?format=simple&url={}"),   "v.gd"),
    ("da.gd",     plain("https://da.gd/shorten?url={}"),                   "da.gd"),
    ("is.gd",     plain("https://is.gd/create.php?format=simple&url={}"),  "is.gd"),
    ("1pt.co",    onept,                                                   "1pt.co"),
    ("clck.ru",   plain("https://clck.ru/--?url={}"),                      "clck.ru"),
    ("spoo.me",   spoo,                                                    "spoo.me"),
    ("ulvis.net", plain("https://ulvis.net/api.php?url={}"),               "ulvis.net"),
    ("tinyurl",   plain("https://tinyurl.com/api-create.php?url={}"),      "tinyurl.com"),
    ("cleanuri",  cleanuri,                                                "cleanuri.com"),
]


def probe(name, call, domain):
    got = []
    for original in SAMPLES:
        try:
            short = call(original)
        except urllib.error.HTTPError as e:
            return f"HTTP {e.code}", None
        except Exception as e:
            return f"{type(e).__name__}: {str(e)[:60]}", None
        if not short.startswith("http"):
            return f"단축 URL 아님: {short[:60]!r}", None
        if domain not in short:
            return f"엉뚱한 도메인: {short[:60]!r}", None
        got.append(short)

    if len(set(got)) != len(got):
        return f"서로 다른 원본에 같은 값 ({got[0]})", None

    # 표본 왕복 — 첫 건만. 전량 검사하면 요청이 두 배가 되고 고장은 어차피 전역적이다.
    verdict = "왕복 확인 못 함"
    try:
        _, final, _ = get(got[0], method="HEAD")
        a, b = urllib.parse.urlsplit(final), urllib.parse.urlsplit(SAMPLES[0])
        verdict = "왕복 OK" if (a.netloc, a.path) == (b.netloc, b.path) else f"왕복 실패 → {final[:50]}"
    except Exception as e:
        verdict = f"왕복 확인 못 함 ({type(e).__name__})"

    return None, (got, verdict)


def main():
    print(f"{'후보':<12} {'결과':<10} {'길이':>4}  비고")
    print("-" * 78)
    ok = []
    for name, call, domain in CANDIDATES:
        err, res = probe(name, call, domain)
        if err:
            print(f"{name:<12} {'실패':<10} {'-':>4}  {err}")
            continue
        got, verdict = res
        n = len(got[0])
        print(f"{name:<12} {'성공':<10} {n:>4}  {verdict} | 예: {got[0]}")
        ok.append((n, name, got[0]))

    print()
    if not ok:
        print("쓸 수 있는 후보 없음 — 러너 IP 에서 무료 단축기가 전부 막혔다.")
        return 0
    ok.sort()
    print("짧은 순:")
    for n, name, sample in ok:
        print(f"  {n:>3}자  {name}  ({sample})")
    print()
    print("참고 — 자체 Cloudflare Worker 는 57자, 국내 매체 URL 중앙값은 74자, 최소 37자.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
