# -*- coding: utf-8 -*-
"""브리핑 누락 감시견 — 슬롯이 통째로 안 나갔을 때 자동 재발송.

왜 briefing.yml 안에 재시도를 넣을 수 없나:
  잡이 GitHub 러너를 배정받지 못하면 잡 안의 **어떤 스텝도 실행되지 않는다**
  (`if: always()` 스텝조차). 2026-08-07 morning이 그 경우였다 — 15분간 큐에
  머물다 취소, 과금 0ms, 로그 404, 아티팩트 0건, 그래서 실패 알림도 못 나갔다.
  재시도는 반드시 그 잡 **바깥**에서 결과를 보고 판단해야 한다.

동작: 마감이 지난 오늘자 슬롯 중 성공 기록이 없는 것을 찾아 재-dispatch.
  - 이미 성공한 슬롯       → 건너뜀
  - 실행 중·대기 중인 슬롯 → 건너뜀 (곧 끝날 수 있다)
  - 시도 횟수 상한 초과    → 건너뜀 (시크릿 만료처럼 고쳐야 낫는 고장에서
                             재시도가 폭주하는 것을 막는다)

사용: python scripts/watchdog.py                 # 판단 + 재발송
      python scripts/watchdog.py --dry-run       # 판단만, 발송 안 함
환경변수: GH_TOKEN(필수, actions:write), GITHUB_REPOSITORY(없으면 --repo)
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")

# 슬롯별 (dispatch 예정 시각, 마감 시각). 마감 = 예정 + 30분.
# 마감을 넉넉히 잡을 필요는 없다 — 마감이 막는 것은 "아직 실행이 생기지도 않은
# 상태"뿐이고, 느리게 도는 실행은 아래 in_progress 검사가 따로 걸러낸다.
# 오히려 마감이 늦으면 감시 창이 좁아진다(특히 evening은 자정 전에 재시도
# 기회가 두 번뿐이라 22:00을 넘기면 안 된다).
SLOT_DEADLINE_KST = {
    "morning":    ((6, 20), (6, 50)),
    "midmorning": ((10, 0), (10, 30)),
    "afternoon":  ((14, 40), (15, 10)),
    "evening":    ((21, 30), (22, 0)),
}

# schedule 실행은 run-name이 "briefing <cron>"으로 찍힌다 (run-name은 스텝 실행
# 전에 평가되므로 슬롯명을 넣을 수 없다). 백업 schedule이 성공했는데 감시견이
# 그걸 못 알아보고 또 쏘면 중복이 되므로 cron → 슬롯 매핑이 필요하다.
CRON_TO_SLOT = {
    "20 21 * * 0-4,6": "morning",
    "0 1 * * 0-5": "midmorning",
    "40 5 * * 0-5": "afternoon",
    "30 12 * * 0-5": "evening",
}

MAX_ATTEMPTS = 3  # 최초 1회 + 재시도 2회
WORKFLOW = "briefing.yml"


def slot_of(display_title):
    """run-name에서 슬롯을 역산. dispatch는 'briefing morning', schedule은 'briefing <cron>'."""
    if not display_title.startswith("briefing "):
        return None
    rest = display_title[len("briefing "):].strip()
    if rest in SLOT_DEADLINE_KST:
        return rest
    return CRON_TO_SLOT.get(rest)


def gh(args, repo):
    """gh CLI 호출. 실패 시 stderr를 그대로 올려 원인을 로그에 남긴다."""
    cmd = ["gh"] + args + ["-R", repo]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"gh 실패: {' '.join(cmd)}\n{p.stderr.strip()}")
    return p.stdout


def todays_runs(repo, day_start_kst):
    """오늘(KST) 생성된 briefing 실행을 슬롯별로 묶는다."""
    since_utc = day_start_kst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw = gh(["run", "list", "-w", WORKFLOW, "-L", "100",
              "--created", f">={since_utc}",
              "--json", "displayTitle,status,conclusion,createdAt"], repo)
    by_slot = {}
    for r in json.loads(raw):
        slot = slot_of(r.get("displayTitle", ""))
        if slot:
            by_slot.setdefault(slot, []).append(r)
    return by_slot


def decide(slot, runs, now_kst, deadline_kst):
    """(재발송할까, 사유) 판단."""
    if now_kst < deadline_kst:
        return False, f"마감 전 (마감 {deadline_kst:%H:%M})"
    if any(r["conclusion"] == "success" for r in runs):
        return False, "이미 성공한 실행 있음"
    if any(r["status"] in ("in_progress", "queued", "requested", "waiting") for r in runs):
        return False, "실행 중 — 결과를 기다린다"
    if len(runs) >= MAX_ATTEMPTS:
        return False, (f"시도 {len(runs)}회로 상한({MAX_ATTEMPTS}) 도달 — "
                       f"일시적 장애가 아니라 고장일 가능성이 높다. 사람이 봐야 한다")
    return True, f"성공 기록 없음 (시도 {len(runs)}회)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.repo:
        sys.exit("GITHUB_REPOSITORY 환경변수 또는 --repo 필요")

    now = datetime.now(KST)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # 토요일(KST)은 네 슬롯 모두 발송을 생략하므로 감시 대상이 아니다.
    if now.weekday() == 5:
        print(f"[watchdog] {now:%Y-%m-%d %H:%M} KST 토요일 — 발송 없는 날, 점검 생략")
        return 0

    by_slot = todays_runs(args.repo, day_start)
    retried = []
    for slot, (_, (dh, dm)) in SLOT_DEADLINE_KST.items():
        deadline = day_start.replace(hour=dh, minute=dm)
        runs = by_slot.get(slot, [])
        need, why = decide(slot, runs, now, deadline)
        mark = "재발송" if need else "정상"
        print(f"[watchdog] {slot:11} {mark} — {why}")
        if not need:
            continue
        if args.dry_run:
            print(f"[watchdog]   (dry-run) {slot} dispatch 생략")
            continue
        gh(["workflow", "run", WORKFLOW, "--ref", "main", "-f", f"slot={slot}"], args.repo)
        retried.append(slot)
        print(f"[watchdog]   {slot} 재발송 요청함")

    if not retried:
        return 0

    # GITHUB_TOKEN으로 쏜 dispatch가 실제로 실행을 만들었는지 확인한다.
    # 만들어지지 않으면 감시견이 조용히 무력해지므로 반드시 눈에 띄게 남긴다.
    after = todays_runs(args.repo, day_start)
    for slot in retried:
        before_n = len(by_slot.get(slot, []))
        if len(after.get(slot, [])) <= before_n:
            print(f"::warning::{slot} 재발송을 요청했으나 새 실행이 생기지 않았다 — "
                  f"GITHUB_TOKEN의 workflow_dispatch 권한(actions:write)을 확인할 것")
    return 0


if __name__ == "__main__":
    sys.exit(main())
