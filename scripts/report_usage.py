# -*- coding: utf-8 -*-
"""`claude -p --output-format json` 의 출력을 사람이 읽는 형태로 되돌리고,
그 실행이 먹은 토큰을 한 줄로 남긴다.

왜 필요한가: 브리핑은 하루 4슬롯 × 주 6일 = 주 24세션을 돌리는데, 지금까지
어느 슬롯이 얼마를 먹는지 볼 방법이 없었다. 주간 한도에 걸려 9/19~9/21 브리핑이
통째로 누락됐을 때도(로그에 "You've hit your weekly limit"만 남았다) 무엇을
줄여야 하는지 판단할 근거가 없었다. 측정값이 없으면 줄일 곳도 고를 수 없다.

stdin 이 JSON 이 아니면(설치 실패·한도 초과 등으로 claude 가 평문을 뱉는 경우)
그대로 통과시킨다 — 진단 메시지를 삼키면 안 된다.
"""
import json
import sys


def human(n):
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except ValueError:
        # JSON 이 아니다 — claude 가 평문으로 죽은 경우다. 원문을 그대로 보여준다.
        sys.stdout.write(raw)
        return 0
    if not isinstance(data, dict):
        sys.stdout.write(raw)
        return 0

    # 세션의 최종 응답(발송 검증 푸터 등) — 기존 로그에 보이던 그 내용이다.
    result = data.get("result")
    if result:
        print(result)

    u = data.get("usage") or {}
    inp = u.get("input_tokens")
    out = u.get("output_tokens")
    cw = u.get("cache_creation_input_tokens")
    cr = u.get("cache_read_input_tokens")
    total_in = sum(v for v in (inp, cw, cr) if isinstance(v, int))

    parts = [
        f"입력 {human(total_in)}",
        f"(신규 {human(inp)} / 캐시생성 {human(cw)} / 캐시읽기 {human(cr)})",
        f"출력 {human(out)}",
        f"턴 {data.get('num_turns', '?')}",
    ]
    dur = data.get("duration_ms")
    if isinstance(dur, int):
        parts.append(f"{dur / 1000:.0f}초")
    cost = data.get("total_cost_usd")
    if isinstance(cost, (int, float)):
        parts.append(f"${cost:.3f}")

    print()
    print("───────── 이번 세션 사용량 ─────────")
    print(" · ".join(parts))

    # claude 는 한도 초과·중단도 is_error 로 알린다. 잡을 실패시켜 워치독이 보게 한다.
    if data.get("is_error"):
        print(f"::error::claude 세션이 오류로 끝났다 — subtype={data.get('subtype', '?')}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
