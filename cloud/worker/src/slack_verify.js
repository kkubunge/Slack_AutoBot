/**
 * Slack Webhook 요청의 서명(HMAC-SHA256) 및 타임스탬프 리플레이 공격 검증 유틸리티.
 */

const textEncoder = new TextEncoder();

/**
 * 두 문자열을 상수 시간(Constant-Time)으로 비교해 타이밍 공격을 방지한다.
 */
function timingSafeEqualString(a, b) {
  const bytesA = textEncoder.encode(a);
  const bytesB = textEncoder.encode(b);
  if (bytesA.length !== bytesB.length) {
    return false;
  }
  let diff = 0;
  for (let i = 0; i < bytesA.length; i += 1) {
    diff |= bytesA[i] ^ bytesB[i];
  }
  return diff === 0;
}

/**
 * ArrayBuffer를 소문자 16진수 문자열로 변환한다.
 */
function toHex(buffer) {
  const bytes = new Uint8Array(buffer);
  let hex = "";
  for (let i = 0; i < bytes.length; i += 1) {
    hex += bytes[i].toString(16).padStart(2, "0");
  }
  return hex;
}

/**
 * Slack 요청 헤더의 X-Slack-Request-Timestamp 및 X-Slack-Signature를 검증한다.
 */
export async function verifySlackSignature({
  signingSecret,
  timestamp,
  signature,
  rawBody,
  nowSec,
}) {
  if (!signingSecret) {
    return { ok: false, reason: "SLACK_SIGNING_SECRET이 설정되지 않았습니다." };
  }
  if (!timestamp || !signature) {
    return { ok: false, reason: "Slack 서명 헤더가 누락되었습니다." };
  }

  const tsNum = Number(timestamp);
  if (!Number.isFinite(tsNum)) {
    return { ok: false, reason: "타임스탬프 형식이 올바르지 않습니다." };
  }

  const currentSec = typeof nowSec === "number" ? nowSec : Math.floor(Date.now() / 1000);
  if (Math.abs(currentSec - tsNum) > 300) {
    return { ok: false, reason: "요청 타임스탬프 유효 시간(5분)을 초과했습니다." };
  }

  const key = await crypto.subtle.importKey(
    "raw",
    textEncoder.encode(signingSecret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const baseString = `v0:${timestamp}:${rawBody}`;
  const macBuffer = await crypto.subtle.sign("HMAC", key, textEncoder.encode(baseString));
  const expected = `v0=${toHex(macBuffer)}`;

  if (!timingSafeEqualString(expected, signature)) {
    return { ok: false, reason: "Slack 서명 불일치" };
  }

  return { ok: true };
}
