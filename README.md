# news-briefing — 하루 4회 뉴스 브리핑 (GitHub Actions)

로컬 PC 없이 GitHub Actions에서 하루 4회(KST 06:20 / 10:00 / 14:15 / 21:30, **토요일 생략**) 뉴스를 수집·선별해 Telegram으로 발송한다. 토요일 뉴스는 일요일 morning의 확장 윈도우(금 21:20~일 06:10)가 커버한다. 기존 Claude 데스크톱 "예정된 작업"(morning/midmorning/afternoon/evening)의 클라우드 이관판.

## 구조

```
scripts/collect.py        기계적 수집기 — 네이버 뉴스 API(방식1) + Google News RSS(방식3, morning만)
                          pubDate 기준 시간 윈도우 필터·경유 URL 디코딩·중복 제거 → pool.json
scripts/record_sent.py    발송 이력 기록기 — 프롬프트가 남긴 /tmp/sent.json을 state/sent.json에 병합·만료정리
config/slots.json         슬롯별 시간 윈도우·키워드·테마·허용 매체 도메인 (수집 설정의 단일 출처)
state/sent.json           최근 96h 발송 이력 (슬롯 간 중복 차단용, 워크플로가 매 발송 후 커밋)
prompts/<slot>.md         Claude 헤드리스 프롬프트 — 풀에서 선별 → G0 게이트 → TinyURL → Telegram 발송 → sent.json 기록
.github/workflows/briefing.yml  cron 4개(UTC) + 수동 실행(workflow_dispatch)
```

설계 원칙: **날짜 검증은 코드(collect.py), 선별·요약은 Claude.** Claude는 pool.json 밖 기사를 추가할 수 없고, 폴백 재검색도 `collect.py --adhoc-*` 경유로만 가능(동일 윈도우 필터 자동 적용).

### 슬롯 간 중복이 생기지 않는 이유 (2단 방어)

1. **윈도우가 맞물린다** — 각 슬롯의 수집 구간은 `[직전 실행 슬롯의 종료 시각, 이번 슬롯의 종료 시각]`이다. 06:10 → 09:50 → 14:05 → 21:20 → (다음날) 06:10 으로 끝과 시작이 정확히 붙어 어떤 발행 시각도 두 번 수집되지 않는다. 토요일 발송 생략은 `prev_run_day` 앵커가 흡수하므로(일요일이면 금요일로 2일 소급) 요일 특례가 따로 없다.
2. **이미 보낸 기사는 다시 수집하지 않는다** — 윈도우만으로는 *같은 사건을 다른 매체가 다음 구간에 재보도*하는 경우를 막지 못한다. 그래서 발송할 때마다 기사 제목·원본 URL을 `state/sent.json`에 남기고, 다음 슬롯의 collect.py가 이를 읽어 수집 단계에서 제외한다. 제목 지문은 `[속보]`·`[단독]` 류 말머리와 공백·문장부호를 제거한 뒤 비교하므로 매체별 표기 차이도 잡힌다. 보존 기간은 96시간(금요일 저녁분이 월요일 아침까지 남도록).

## 필요한 GitHub Secrets (Settings → Secrets and variables → Actions)

| Secret | 내용 |
|---|---|
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | 네이버 개발자센터 애플리케이션 (검색 API 사용 설정) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram 봇 토큰·수신 채팅 ID |
| `CLAUDE_CODE_OAUTH_TOKEN` **또는** `ANTHROPIC_API_KEY` | Claude 실행 인증 — 구독(Pro/Max)이면 로컬에서 `claude setup-token`으로 생성한 OAuth 토큰, 아니면 API 키 |

## 운영

- **수동 실행/테스트**: Actions 탭 → news-briefing → Run workflow → 슬롯 선택.
- **키워드/테마 변경**: `config/slots.json` 수정 (수집), 우선순위·제외 규칙은 `prompts/*.md` 수정 (선별).
- **실행 로그·수집 풀**: 각 run의 artifact `pool-<slot>-<runid>`에 pool.json 7일 보관. 발송 직전 메시지 전문은 run 로그의 발송 스텝에 그대로 출력되므로 실제 발송본 형식을 사후 확인할 수 있다.
- **메시지 형식 (4슬롯 공통 v7)**: 번호·화살표 없이 `제목` 줄 + `단축 URL` 줄, 기사 사이 빈 줄 1개 (morning 방식3만 그 사이에 `- 요약` 줄 추가). **개행은 반드시 임시 파일에 진짜 줄바꿈으로 쓰고 `--data-urlencode "text@/tmp/msgN.txt"`로 발송** — 셸 인자에 `\n` 두 글자를 넣으면 Telegram에 리터럴 `\n`이 텍스트로 찍힌다.
- **정시 발송**: GitHub `schedule`은 정시 보장이 없어 실측 수십 분~3시간 이상 지연됨. 그래서 1차 발송은 외부 스케줄러(Claude Code Routine, 분 단위 정확도)가 각 슬롯 정각에 `workflow_dispatch`를 호출하는 방식. `schedule` cron은 백업으로 남아 있고, 같은 슬롯이 이미 dispatch로 발송(또는 진행 중)이면 워크플로 내 중복 발송 가드가 생략시킨다. 시간 윈도우는 KST 고정 앵커(예: 06:10) 기준이라 어느 경로로 실행돼도 창은 동일.
- **월요일**: 주말 포함 확장 윈도우(금 14:00~) 자동 적용 (`collect.py`가 KST 요일 판단).
