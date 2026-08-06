# news-briefing — 하루 4회 뉴스 브리핑 (GitHub Actions)

로컬 PC 없이 GitHub Actions에서 하루 4회(KST 06:20 / 10:00 / 14:40 / 21:30 실행, **토요일 생략**) 뉴스를 수집·선별해 Telegram으로 발송한다. 국내 브리핑(방식1)은 4회 모두, 글로벌 13테마 브리핑(방식3)은 morning·evening 2회 나간다 — morning이 밤사이, evening이 낮 시간대를 맡아 겹치지 않는다. 토요일 뉴스는 일요일 morning의 확장 윈도우(금 21:20~일 06:10)가 커버한다. 기존 Claude 데스크톱 "예정된 작업"(morning/midmorning/afternoon/evening)의 클라우드 이관판.

## 구조

```
scripts/collect.py        기계적 수집기 — 네이버 뉴스 API(방식1) + Google News RSS(방식3, morning·evening)
                          pubDate 기준 시간 윈도우 필터·경유 URL 디코딩·중복 제거 → pool.json
scripts/record_sent.py    발송 이력 기록기 — 프롬프트가 남긴 out/sent.json을 state/sent.json에 병합·만료정리
scripts/watchdog.py       누락 감시견 — 마감이 지났는데 성공 기록이 없는 슬롯을 재-dispatch
config/slots.json         슬롯별 시간 윈도우·키워드·테마·허용 매체 도메인 (수집 설정의 단일 출처)
state/sent.json           최근 96h 발송 이력 (슬롯 간 중복 차단용, 워크플로가 매 발송 후 커밋)
prompts/<slot>.md         Claude 헤드리스 프롬프트 — 풀에서 선별 → G0 게이트 → TinyURL → Telegram 발송 → sent.json 기록
.github/workflows/briefing.yml  cron 4개(UTC) + 수동 실행(workflow_dispatch)
.github/workflows/watchdog.yml  매시(KST 07~23) 누락 점검 → 자동 재발송
```

설계 원칙: **날짜 검증은 코드(collect.py), 선별·요약은 Claude.** Claude는 pool.json 밖 기사를 추가할 수 없고, 폴백 재검색도 `collect.py --adhoc-*` 경유로만 가능(동일 윈도우 필터 자동 적용).

### 슬롯 간 중복이 생기지 않는 이유 (3단 방어)

겹침에는 세 가지 종류가 있고, 각각 막는 층이 다르다.

**1층 — 같은 시각대를 두 번 수집하는 것 (윈도우)**
각 슬롯의 수집 구간은 `[직전 실행 슬롯의 종료 시각, 이번 슬롯의 종료 시각]`이다. 06:10 → 09:50 → 14:30 → 21:20 → (다음날) 06:10 으로 끝과 시작이 정확히 붙어 어떤 발행 시각도 두 번 수집되지 않는다. 토요일 발송 생략은 `prev_run_day` 앵커가 흡수하므로(일요일이면 금요일로 2일 소급) 요일 특례가 따로 없다.

**2층 — 같은 기사가 다시 나가는 것 (기계적 대조)**
발송할 때마다 기사 제목·원본 URL을 `state/sent.json`에 남기고, 다음 슬롯의 `collect.py`가 이를 읽어 **수집 단계에서** 제외한다. 제목 지문은 `[속보]`·`[단독]` 류 말머리와 공백·문장부호를 제거한 뒤 비교하므로 매체별 표기 차이를 흡수한다. 보존 96시간(금요일 저녁분이 월요일 아침까지 유효).

**3층 — 같은 사건을 다룬 *다른* 기사가 나가는 것 (LLM 판단)**
제목도 URL도 매체도 다르면 2층이 못 잡는다. 실제로 겹친다고 체감되는 대부분이 이 유형이고, 특히 **금융**에서 잦다(기관명 키워드 15개가 같은 사건을 여러 각도로 물어온다). 그래서 `collect.py`가 최근 48시간 발송 제목을 `pool.json`의 **`recently_sent`** 로 실어 보내고, 프롬프트 §7-1이 `(주체)+(조치)+(대상)` 세 축으로 같은 사건인지 판단해 제외한다. 애매하면 제외 쪽으로 기울고, 실질적 진전(새 결정·수치·당사자)이 있는 후속 보도만 예외로 포함하되 무엇이 새로운지 푸터에 밝힌다. 제외 건수도 푸터에 남는다.

## 필요한 GitHub Secrets (Settings → Secrets and variables → Actions)

| Secret | 내용 |
|---|---|
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | 네이버 개발자센터 애플리케이션 (검색 API 사용 설정) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram 봇 토큰·수신 채팅 ID |
| `CLAUDE_CODE_OAUTH_TOKEN` **또는** `ANTHROPIC_API_KEY` | Claude 실행 인증 — 구독(Pro/Max)이면 로컬에서 `claude setup-token`으로 생성한 OAuth 토큰, 아니면 API 키 |

## 운영

- **수동 실행/테스트**: Actions 탭 → news-briefing → Run workflow → 슬롯 선택.
- **키워드/테마 변경**: `config/slots.json` 수정 (수집), 우선순위·제외 규칙은 `prompts/*.md` 수정 (선별).
- **실행 로그·수집 풀·발송본**: 각 run의 artifact `pool-<slot>-<runid>`에 pool.json과 `out/`(실제 발송본 `msg*.txt`, 발송 목록 `sent.json`)이 7일 보관된다. 발송본은 run 로그의 **「실제 발송본 출력」 스텝**에도 그대로 찍힌다. 주의: `claude -p`는 최종 응답 텍스트만 stdout으로 내보내므로 **헤드리스 세션 안에서 `cat`을 찍어도 Actions 로그에는 남지 않는다** — 반드시 워크플로 스텝이 파일을 출력해야 한다.
- **메시지 형식 (4슬롯 공통 v7)**: 번호·화살표 없이 `제목` 줄 + `단축 URL` 줄, 기사 사이 빈 줄 1개 (morning 방식3만 그 사이에 `- 요약` 줄 추가). **개행은 반드시 임시 파일에 진짜 줄바꿈으로 쓰고 `--data-urlencode "text@/tmp/msgN.txt"`로 발송** — 셸 인자에 `\n` 두 글자를 넣으면 Telegram에 리터럴 `\n`이 텍스트로 찍힌다.
- **정시 발송**: GitHub `schedule`은 정시 보장이 없어 실측 수십 분~3시간 이상 지연됨. 그래서 1차 발송은 외부 스케줄러(Claude Code Routine, 분 단위 정확도)가 각 슬롯 정각에 `workflow_dispatch`를 호출하는 방식. `schedule` cron은 백업으로 남아 있고, 같은 슬롯이 이미 dispatch로 발송(또는 진행 중)이면 워크플로 내 중복 발송 가드가 생략시킨다. 시간 윈도우는 KST 고정 앵커(예: 06:10) 기준이라 어느 경로로 실행돼도 창은 동일.
- **월요일**: 주말 포함 확장 윈도우(금 14:00~) 자동 적용 (`collect.py`가 KST 요일 판단).
- **누락 자동 복구**: `watchdog` 워크플로가 KST 07:05~23:05 매시 돌며, 마감(슬롯 예정시각+30분)이 지났는데 그날 성공 기록이 없는 슬롯을 다시 dispatch한다. 이미 성공했거나 아직 실행 중이면 건드리지 않고, 하루 3회(최초 1+재시도 2)를 넘으면 멈춘다 — 시크릿 만료처럼 고쳐야 낫는 고장에서 재시도가 폭주하지 않도록.

  **왜 briefing.yml 안에 재시도를 넣지 않았나**: 잡이 GitHub 러너를 배정받지 못하면 잡 안의 **어떤 스텝도 실행되지 않는다** — `if: always()` 스텝조차. 2026-08-07 morning이 그 경우로, 15분간 큐에 머물다 취소됐고 과금 0ms·로그 404·아티팩트 0건이라 실패 알림조차 나갈 수 없었다. 판단은 반드시 그 잡 바깥에서 해야 한다.
