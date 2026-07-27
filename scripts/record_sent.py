# -*- coding: utf-8 -*-
"""발송 이력 기록기 — 슬롯 간 중복 발송 차단용 상태 갱신.

브리핑 프롬프트가 발송 직후 `/tmp/sent.json`에 이번에 내보낸 기사 목록을
`[{"title": ..., "url": ...}, ...]` 형태로 남긴다. 이 스크립트가 그것을
`state/sent.json`에 병합하고 보존기간이 지난 항목을 잘라낸다.
다음 슬롯의 collect.py가 이 파일을 읽어 같은 기사를 애초에 수집하지 않는다.

입력 파일이 없거나 깨져 있으면 경고만 남기고 정상 종료한다 — 이력이 비면
중복이 다시 늘어날 뿐 브리핑 자체는 계속 나가야 하기 때문이다(fail-open).

사용: python scripts/record_sent.py --slot morning --in /tmp/sent.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")
STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "state", "sent.json")
RETENTION_HOURS = 96  # collect.py의 SENT_RETENTION_HOURS와 동일하게 유지할 것


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data.get("sent"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"sent": []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", required=True)
    ap.add_argument("--in", dest="infile", default="/tmp/sent.json")
    args = ap.parse_args()

    now_kst = datetime.now(KST)
    stamp = now_kst.strftime("%Y-%m-%d %H:%M")

    try:
        with open(args.infile, encoding="utf-8") as f:
            incoming = json.load(f)
    except (OSError, ValueError) as e:
        print(f"[record_sent] 경고: {args.infile} 를 읽지 못함 ({e}). "
              f"이번 슬롯 발송분은 이력에 기록되지 않는다 — 다음 슬롯에서 중복 가능.",
              file=sys.stderr)
        return 0

    # 프롬프트가 {"sent": [...]} 로 감싸 보내는 경우도 허용
    if isinstance(incoming, dict):
        incoming = incoming.get("sent") or incoming.get("articles") or []

    state = load_state()
    added = 0
    for a in incoming:
        if not isinstance(a, dict):
            continue
        title = (a.get("title") or "").strip()
        url = (a.get("url") or "").strip()
        if not (title or url):
            continue
        state["sent"].append({"slot": args.slot, "sent_kst": stamp,
                              "title": title, "url": url})
        added += 1

    cutoff = now_kst - timedelta(hours=RETENTION_HOURS)
    kept = []
    for e in state["sent"]:
        try:
            ts = datetime.strptime(e["sent_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        except (KeyError, ValueError):
            continue
        if ts >= cutoff:
            kept.append(e)
    pruned = len(state["sent"]) - len(kept)
    state["sent"] = kept
    state["updated_kst"] = stamp

    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)

    print(f"[record_sent] {args.slot}: {added}건 기록, {pruned}건 만료 정리, "
          f"보존 {len(kept)}건 (최근 {RETENTION_HOURS}h)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
