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

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"

# 순서가 곧 우선순위다. TinyURL 을 마지막에 둔 것은 위 사고 때문이다.
PROVIDERS = [
    ("is.gd", "https://is.gd/create.php?format=simple&url={}", "https://is.gd/"),
    ("v.gd", "https://v.gd/create.php?format=simple&url={}", "https://v.gd/"),
    ("tinyurl", "https://tinyurl.com/api-create.php?url={}", "https://tinyurl.com/"),
]

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
    for name, template, prefix in PROVIDERS:
        if not remaining:
            break
        print(f"[shorten] {name}: {len(remaining)}건 시도")
        seen = {}
        got = {}
        broken = None
        for original in remaining:
            target = template.format(urllib.parse.quote(original, safe=""))
            try:
                _, _, body = fetch(target)
            except Exception as e:
                print(f"[shorten]   실패({original[:60]}…): {e}")
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

        if broken:
            print(f"[shorten] {name} 버림 — {broken}")
            continue
        if not got:
            print(f"[shorten] {name}: 한 건도 못 줄임 — 다음 공급자로")
            continue

        sample_orig, sample_short = next(iter(got.items()))
        ok = resolves_back(sample_short, sample_orig)
        if ok is False:
            print(f"[shorten] {name} 버림 — 표본 {sample_short} 가 원본으로 돌아오지 않음")
            continue
        if ok is None:
            print(f"[shorten] {name}: 표본 왕복 확인 불가(네트워크) — 형태·유일성 검사만으로 채택")

        out.update(got)
        remaining = [u for u in remaining if u not in out]
        print(f"[shorten] {name}: {len(got)}건 성공, 남은 {len(remaining)}건")

    return out


def main():
    files = sorted(glob.glob("out/msg*.txt"))
    if not files:
        print("[shorten] out/msg*.txt 없음 — 할 일 없음")
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
        print("[shorten] 발송본에 URL 줄이 없음 — 할 일 없음")
        return 0

    already = [u for u in urls if any(u.startswith(p) for _, _, p in PROVIDERS)]
    todo = [u for u in urls if u not in already]
    if already:
        print(f"[shorten] 이미 단축된 URL {len(already)}건은 건너뛴다")
    print(f"[shorten] 대상 {len(todo)}건")

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
    print(f"[shorten] 완료 — {changed}줄 교체, 단축 {len(mapping)}/{len(todo)}건, 실패 {failed}건")
    if failed:
        # 잡을 실패시키지 않는다. 원본 URL로도 기사는 열린다 — 링크가 길 뿐이다.
        print(f"::warning::URL 단축 실패 {failed}건 — 해당 건은 원본 URL로 발송된다")
    if not mapping:
        print("::warning::단축기 3곳 모두 실패 — 이번 발송은 전부 원본 URL이다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
