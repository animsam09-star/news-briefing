#!/usr/bin/env python3
"""out/msg*.txt 안의 URL을 단축 URL로 바꾼다.

원래는 Claude 세션이 프롬프트 지시에 따라 세션 안에서 TinyURL 을 부르고, 그 결과를
발송본에 적어 넣었다. 2026-09-07 evening 에서 45건 전부가 이렇게 실패했다:

  단축 실패 45건(TinyURL API가 모든 요청에 동일한 무관 URL 반환 — 원본 URL로 전량 대체)

TinyURL 이 에러를 주지 않고 **아무 URL이나 200으로 돌려주는** 형태로 망가졌다.
이건 단축기 하나에 매달린 구조의 문제라, 두 가지를 바꾼다.

1. 세션 밖으로 뺀다. 발송 자체를 세션 밖으로 뺐던 것과 같은 이유다 — 세션 안에서 한
   일은 로그에 안 남아서 무엇이 왜 실패했는지 사후에 볼 수가 없다.
2. 공급자를 셋으로 늘리고, 돌아온 값을 **검증**한다. 위 사고의 핵심은 실패가
   에러가 아니라 '그럴듯한 오답'이었다는 것이다. 그래서:
   - 형태 검사: 그 공급자의 호스트로 시작하는 짧은 http(s) URL인가
   - 유일성 검사: 이번 실행에서 다른 원본에 이미 준 적 있는 값은 아닌가
     (모든 요청에 같은 값을 주는 고장을 정확히 이 검사가 잡는다)
   - 왕복 검사: 표본 1건만 HEAD 로 따라가 원본으로 돌아오는지 본다
     (전량 검사하면 요청이 두 배가 되고, 고장은 어차피 전역적이다)

하나라도 어긋나면 그 공급자를 이번 실행에서 버리고 다음으로 넘어간다. 셋 다 실패하면
원본 URL을 그대로 둔다 — 링크가 길어질 뿐 기사는 열린다. 엉뚱한 데로 가는 짧은 링크가
훨씬 나쁘다.
"""

import glob
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# 진단은 화면과 파일 양쪽에 남긴다.
#
# 2026-09-08 morning 에서 42건이 전부 원본 URL로 나갔는데, 이 스크립트가 무엇을
# 왜 못 했는지 확인할 수가 없었다. 스텝 출력이 잡 로그 한가운데에 묻혀 있고,
# 그 구간만 로그 API 로 꺼낼 수가 없었다(tail 이 그 앞까지 닿지 않는다).
# 그래서 요약을 out/shorten-status.txt 에도 쓰고, 잡 끝의 '발송본 출력' 스텝이
# 그것을 찍는다 — 거긴 항상 읽을 수 있다.
LOG = []


def say(line):
    print(line)
    LOG.append(line)


UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"


_PROVIDERS_CACHE = []


def providers():
    """(이름, 호출함수, 접두어) 목록. 순서가 곧 우선순위다.

    자체 단축기(Cloudflare Worker)가 설정돼 있으면 그것을 먼저 쓴다.

    무료 공개 단축기는 GitHub Actions 러너에서 못 쓴다. 2026-09-08 에 42건으로
    실측했다 — is.gd 와 v.gd 는 HTTP 200 에 본문이 'Error, database insert failed'
    로 **글자 하나까지 같았고**(같은 백엔드로 보인다), TinyURL 은 42건 전부에
    같은 단축 URL 을 돌려줬다. 데이터센터 IP 를 남용 소스로 보는 것이라 공급자를
    더 넣어도 같은 벽이다.

    그래도 공개 단축기를 지우지는 않는다. 자체 단축기가 아직 없거나(시크릿 미설정)
    잠깐 죽었을 때 어쩌다 걸리면 그것대로 이득이고, 실패해도 원본 URL 로 나갈 뿐이다.
    """
    if _PROVIDERS_CACHE:
        return _PROVIDERS_CACHE
    out = []

    base = (os.environ.get("SHORTENER_URL") or "").strip().rstrip("/")
    token = (os.environ.get("SHORTENER_TOKEN") or "").strip()
    if base and token:
        def call_self(original, _base=base, _token=token):
            req = urllib.request.Request(
                _base + "/",
                data=original.encode("utf-8"),
                headers={"User-Agent": UA,
                         "Authorization": "Bearer " + _token,
                         "Content-Type": "text/plain; charset=utf-8"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read(2048).decode("utf-8", errors="replace").strip()
        out.append(("self(worker)", call_self, base + "/"))
    else:
        say("[shorten] 자체 단축기 미설정 (SHORTENER_URL/SHORTENER_TOKEN 없음) — 공개 단축기만 시도한다")

    for name, template, prefix in (
        ("is.gd", "https://is.gd/create.php?format=simple&url={}", "https://is.gd/"),
        ("v.gd", "https://v.gd/create.php?format=simple&url={}", "https://v.gd/"),
        ("tinyurl", "https://tinyurl.com/api-create.php?url={}", "https://tinyurl.com/"),
    ):
        def call_get(original, _t=template):
            return fetch(_t.format(urllib.parse.quote(original, safe="")))[2]
        out.append((name, call_get, prefix))

    _PROVIDERS_CACHE.extend(out)
    return out

# 발송본에서 URL 은 항상 줄 전체를 차지한다(§8 템플릿). 본문 중간의 괄호·따옴표를
# 주워 담지 않도록 줄 단위로만 바꾼다.
URL_LINE = re.compile(r"^(\s*)(https?://\S+)(\s*)$")


def fetch(url, timeout=15, method="GET"):
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.geturl(), r.read(2048).decode("utf-8", errors="replace").strip()


def looks_like_short(value, prefix):
    if not value.startswith(prefix):
        return False
    slug = value[len(prefix):]
    # 슬러그는 짧고 단순하다. 긴 값이 오면 원본을 되돌려준 것이거나 에러 문구다.
    return 0 < len(slug) <= 24 and "/" not in slug and " " not in slug


def resolves_back(short, original, timeout=15):
    """표본 검증: 단축 URL을 따라가 원본에 닿는지 본다."""
    try:
        _, final, _ = fetch(short, timeout=timeout, method="HEAD")
    except Exception:
        return None  # 판정 불가 — 검증 실패로 치지 않는다
    a = urllib.parse.urlsplit(final)
    b = urllib.parse.urlsplit(original)
    return (a.netloc, a.path) == (b.netloc, b.path)


def shorten_all(urls):
    """{원본: 단축} 를 돌려준다. 못 줄인 것은 키에 없다."""
    out = {}
    remaining = list(urls)
    for name, call, prefix in providers():
        if not remaining:
            break
        say(f"[shorten] {name}: {len(remaining)}건 시도")
        seen = {}
        got = {}
        broken = None
        # 예외 사유를 종류별로 센다. 42건이 같은 이유로 죽으면 42줄이 아니라
        # 한 줄로 보여야 원인이 보인다.
        errs = {}
        for original in remaining:
            body = None
            for attempt in (1, 2):
                try:
                    body = call(original)
                    break
                except Exception as e:
                    if attempt == 2:
                        key = f"{type(e).__name__}: {str(e)[:80]}"
                        errs[key] = errs.get(key, 0) + 1
                    else:
                        time.sleep(0.5)
            if body is None:
                continue
            if not looks_like_short(body, prefix):
                broken = f"단축 URL 형태가 아닌 응답: {body[:80]!r}"
                break
            if body in seen:
                broken = (f"서로 다른 원본에 같은 값을 반환 ({body}) "
                          f"— 2026-09-07 TinyURL 과 같은 고장")
                break
            seen[body] = original
            got[original] = body
            time.sleep(0.15)  # 무료 단축기는 대체로 초당 몇 건이 상한이다

        for reason, n in sorted(errs.items(), key=lambda kv: -kv[1]):
            say(f"[shorten]   {name} 요청 실패 {n}건 — {reason}")
        if broken:
            say(f"[shorten] {name} 버림 — {broken}")
            continue
        if not got:
            say(f"[shorten] {name}: 한 건도 못 줄임 — 다음 공급자로")
            continue

        sample_orig, sample_short = next(iter(got.items()))
        ok = resolves_back(sample_short, sample_orig)
        if ok is False:
            say(f"[shorten] {name} 버림 — 표본 {sample_short} 가 원본으로 돌아오지 않음")
            continue
        if ok is None:
            say(f"[shorten] {name}: 표본 왕복 확인 불가(네트워크) — 형태·유일성 검사만으로 채택")

        out.update(got)
        remaining = [u for u in remaining if u not in out]
        say(f"[shorten] {name}: {len(got)}건 성공, 남은 {len(remaining)}건")

    return out


def flush():
    """요약을 out/shorten-status.txt 에 남긴다. 발송본 출력 스텝이 이걸 찍는다."""
    try:
        os.makedirs("out", exist_ok=True)
        with open("out/shorten-status.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(LOG) + "\n")
    except Exception as e:
        print(f"[shorten] 요약 파일 쓰기 실패(무시): {e}")


def main():
    files = sorted(glob.glob("out/msg*.txt"))
    if not files:
        say("[shorten] out/msg*.txt 없음 — 할 일 없음")
        return 0

    # 파일별 줄을 미리 읽어두고, 등장한 URL을 순서대로 모은다(중복 제거).
    docs = {p: open(p, encoding="utf-8").read().splitlines(keepends=True) for p in files}
    urls = []
    for lines in docs.values():
        for line in lines:
            m = URL_LINE.match(line)
            if m and m.group(2) not in urls:
                urls.append(m.group(2))

    if not urls:
        say("[shorten] 발송본에 URL 줄이 없음 — 할 일 없음")
        return 0

    prefixes = [p for _, _, p in providers()]
    already = [u for u in urls if any(u.startswith(p) for p in prefixes)]
    todo = [u for u in urls if u not in already]
    if already:
        say(f"[shorten] 이미 단축된 URL {len(already)}건은 건너뛴다")
    say(f"[shorten] 대상 {len(todo)}건")

    mapping = shorten_all(todo)

    changed = 0
    for path, lines in docs.items():
        new = []
        for line in lines:
            m = URL_LINE.match(line)
            if m and m.group(2) in mapping:
                nl = "\n" if line.endswith("\n") else ""
                new.append(f"{m.group(1)}{mapping[m.group(2)]}{nl}")
                changed += 1
            else:
                new.append(line)
        if new != lines:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(new)

    failed = len(todo) - len(mapping)
    say(f"[shorten] 완료 — {changed}줄 교체, 단축 {len(mapping)}/{len(todo)}건, 실패 {failed}건")
    if failed:
        # 잡을 실패시키지 않는다. 원본 URL로도 기사는 열린다 — 링크가 길 뿐이다.
        say(f"::warning::URL 단축 실패 {failed}건 — 해당 건은 원본 URL로 발송된다")
    if not mapping:
        say(f"::warning::단축기 {len(providers())}곳 모두 실패 — 이번 발송은 전부 원본 URL이다")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        flush()
    sys.exit(rc)
