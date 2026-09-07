# 브리핑 URL 단축기 (Cloudflare Worker)

## 왜 필요한가

무료 공개 단축기는 GitHub Actions 러너에서 못 쓴다. 2026-09-08 에 브리핑 URL
42건 그대로 러너에서 실측한 결과다:

| 단축기 | 응답 |
|---|---|
| is.gd | HTTP 200 인데 본문이 `Error, database insert failed` |
| v.gd | **글자 하나까지 같은 응답** — 같은 백엔드로 보인다 |
| TinyURL | 42건 **전부에 같은 단축 URL** 을 반환 |

셋 다 에러 코드가 아니라 '정상 응답인 척하는 거부'다. 데이터센터 IP 를 남용
소스로 취급하는 것이고, 공급자를 더 넣어도 같은 벽이다. 실제로 같은 스크립트가
7시간 전 다른 러너에서는 TinyURL 로 6/6 을 줄였다 — 러너 IP 에 따라 갈린다.

우리 것이면 우리 규칙만 지키면 된다.

## 배포 (한 번만)

Cloudflare 계정이 필요하다. 이미 스크리닝 사이트들에 Workers 를 쓰고 있으므로
추가 비용은 없다(무료 한도: 하루 10만 요청, KV 하루 1,000 쓰기 — 브리핑은
하루 최대 160건 정도라 여유가 크다).

```bash
cd worker

# 1) KV 네임스페이스 생성 → 출력된 id 를 wrangler.toml 의 id 에 붙여넣는다
npx wrangler kv namespace create LINKS

# 2) 발급용 토큰을 정해 Worker 시크릿으로 넣는다 (아무 긴 임의 문자열)
#    예: openssl rand -hex 32
npx wrangler secret put SHORTENER_TOKEN

# 3) 배포
npx wrangler deploy
```

배포가 끝나면 주소가 나온다: `https://news-briefing-link.<서브도메인>.workers.dev`

## GitHub 시크릿 등록

저장소 → Settings → Secrets and variables → Actions → New repository secret

| 이름 | 값 |
|---|---|
| `SHORTENER_URL` | 위에서 나온 Worker 주소 (끝에 `/` 없이) |
| `SHORTENER_TOKEN` | 2번에서 정한 토큰과 같은 값 |

둘 다 넣으면 다음 브리핑부터 자체 단축기를 먼저 씁니다. 하나라도 비어 있으면
공개 단축기만 시도하고, 로그에 `자체 단축기 미설정` 이 찍힌다.

## 확인

`resend` 워크플로를 `dry_run: true` 로 돌리면 발송 없이 단축만 시험할 수 있다.
`text` 에 URL 몇 개를 넣고 실행한 뒤 로그의 `[shorten]` 줄을 보면 된다:

```
[shorten] self(worker): 6건 시도
[shorten] self(worker): 6건 성공, 남은 0건
```

## 동작

- `POST /` + `Authorization: Bearer <토큰>`, 본문에 원본 URL → 단축 URL 반환
- `GET /<slug>` → 302 로 원본에 보낸다 (인증 없음 — 이게 링크의 본체다)
- `GET /` → `ok` (헬스체크)

slug 는 URL 의 sha-256 앞부분이라 **같은 기사는 항상 같은 slug** 가 된다. 슬롯마다
같은 기사가 겹치는 이 파이프라인에서 KV 쓰기가 중복되지 않는다.

http/https 가 아닌 URL 은 거부한다 — `javascript:` 같은 것을 담아 두면 이 도메인이
그걸 대신 실행해 주는 꼴이 된다.

## 주소가 길면

`workers.dev` 주소는 40자 안팎이라 뉴스 URL(60~145자)보다는 짧지만 아주 짧지는
않다. 더 줄이고 싶으면 Cloudflare 에 짧은 도메인을 붙여 Worker 에 라우트를
걸면 된다 — 코드는 그대로 두고 `SHORTENER_URL` 만 바꾸면 된다.
