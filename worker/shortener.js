/**
 * 브리핑 URL 단축기 (Cloudflare Worker + KV)
 *
 * 왜 자체 단축기인가 — 2026-09-08 에 무료 공개 단축기 세 곳이 GitHub Actions
 * 러너에서 전부 거부됐다. 42건 실측:
 *
 *   is.gd   → HTTP 200 인데 본문이 'Error, database insert failed'
 *   v.gd    → 글자 하나까지 같은 응답 (같은 백엔드로 보인다)
 *   tinyurl → 42건 전부에 같은 단축 URL 을 반환
 *
 * 데이터센터 IP 를 남용 소스로 취급하는 것이고, 공급자를 더 넣어도 같은 벽이다.
 * 우리 것이면 우리 규칙만 지키면 된다.
 *
 * 엔드포인트
 *   POST /              Authorization: Bearer <SHORTENER_TOKEN>
 *                       본문: 원본 URL 한 줄. 응답: 단축 URL 한 줄.
 *   GET  /<slug>        302 로 원본에 보낸다. 인증 없음 — 이게 링크의 본체다.
 *   GET  /              헬스체크. 'ok' 한 줄.
 *
 * slug 는 URL 의 sha-256 앞부분이다. 같은 기사를 다시 줄이면 같은 slug 가 나오고
 * KV 쓰기도 일어나지 않는다 — 슬롯마다 같은 기사가 겹치는 이 파이프라인에서
 * 무료 한도(하루 1,000 쓰기)를 아끼는 데 그대로 도움이 된다.
 */

const SLUG_LEN = 8;          // 32비트 남짓. 기사 수천 건 규모에서 충돌은 사실상 없다.
const MAX_URL = 2048;
const TTL_DAYS = 400;        // 브리핑 링크를 1년 넘게 눌러볼 일은 없다고 본다.

async function slugFor(url) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(url));
  const bytes = new Uint8Array(digest);
  // base36 로 접는다 — 대소문자 구분이 없어 손으로 옮겨 적어도 안전하다.
  let n = 0n;
  for (const b of bytes.slice(0, 8)) n = (n << 8n) | BigInt(b);
  return n.toString(36).slice(0, SLUG_LEN).padStart(SLUG_LEN, "0");
}

function text(body, status = 200) {
  return new Response(body, {
    status,
    headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/^\/+/, "");

    if (request.method === "GET" && path === "") return text("ok");

    if (request.method === "GET") {
      if (!/^[0-9a-z]{1,16}$/.test(path)) return text("not found", 404);
      const target = await env.LINKS.get(path);
      if (!target) return text("not found", 404);
      // 302 — 영구가 아니다. 브라우저가 캐시해 버리면 나중에 고칠 수가 없다.
      return Response.redirect(target, 302);
    }

    if (request.method === "POST" && path === "") {
      const auth = request.headers.get("authorization") || "";
      const want = `Bearer ${env.SHORTENER_TOKEN}`;
      // 길이가 다르면 조기 반환되는 비교는 타이밍을 흘린다. 길이부터 맞춘다.
      if (auth.length !== want.length || !timingSafeEqual(auth, want)) {
        return text("unauthorized", 401);
      }

      const body = (await request.text()).trim();
      if (!body || body.length > MAX_URL) return text("bad request", 400);
      let parsed;
      try {
        parsed = new URL(body);
      } catch {
        return text("bad request", 400);
      }
      // http(s) 만 받는다. javascript: 같은 것을 담아 두면 이 도메인이
      // 그걸 대신 실행해 주는 꼴이 된다.
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
        return text("bad request", 400);
      }

      const slug = await slugFor(body);
      const existing = await env.LINKS.get(slug);
      if (existing !== body) {
        await env.LINKS.put(slug, body, { expirationTtl: TTL_DAYS * 24 * 3600 });
      }
      return text(`${url.origin}/${slug}`);
    }

    return text("method not allowed", 405);
  },
};

function timingSafeEqual(a, b) {
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
