import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import test from "node:test";

import { checkAuthorization, parseCloudCommand } from "../src/command_parser.js";
import { createHandler } from "../src/index.js";
import { verifySlackSignature } from "../src/slack_verify.js";

function signSlackBody(secret, timestamp, rawBody) {
  const hmac = createHmac("sha256", secret);
  hmac.update(`v0:${timestamp}:${rawBody}`);
  return `v0=${hmac.digest("hex")}`;
}

test("verifySlackSignature: 정상 서명, 잘못된 서명, 만료 타임스탬프 검증", async () => {
  const secret = "test_signing_secret";
  const ts = "1700000000";
  const body = '{"type":"url_verification","challenge":"abc"}';
  const sig = signSlackBody(secret, ts, body);

  const valid = await verifySlackSignature({
    signingSecret: secret,
    timestamp: ts,
    signature: sig,
    rawBody: body,
    nowSec: 1700000010,
  });
  assert.equal(valid.ok, true);

  const invalid = await verifySlackSignature({
    signingSecret: secret,
    timestamp: ts,
    signature: "v0=deadbeef",
    rawBody: body,
    nowSec: 1700000010,
  });
  assert.equal(invalid.ok, false);

  const expired = await verifySlackSignature({
    signingSecret: secret,
    timestamp: ts,
    signature: sig,
    rawBody: body,
    nowSec: 1700000400,
  });
  assert.equal(expired.ok, false);
});

test("parseCloudCommand: 도움말, 목록, 워크플로우 실행, [프로젝트] 분기 파싱", () => {
  assert.equal(parseCloudCommand("<@U12345> !도움").kind, "help");

  const listProjects = parseCloudCommand("<@U12345> !프로젝트");
  assert.equal(listProjects.action, "list");
  assert.equal(listProjects.prompt, "projects");

  const runWf = parseCloudCommand("!실행 traillog --dry-run");
  assert.equal(runWf.action, "run");
  assert.equal(runWf.workflow, "traillog");
  assert.equal(runWf.extraArgs, "--dry-run");

  const planCmd = parseCloudCommand("!분석 [ondotrip] 글 생성 구조 파악해줘");
  assert.equal(planCmd.action, "ai");
  assert.equal(planCmd.mode, "plan");
  assert.equal(planCmd.project, "ondotrip");
  assert.equal(planCmd.prompt, "글 생성 구조 파악해줘");

  const devDefault = parseCloudCommand("<@U999> [traillog] 캡션 문구 고쳐줘");
  assert.equal(devDefault.action, "ai");
  assert.equal(devDefault.mode, "dev");
  assert.equal(devDefault.project, "traillog");
  assert.equal(devDefault.prompt, "캡션 문구 고쳐줘");

  assert.equal(parseCloudCommand("!예약").kind, "schedule");
  assert.equal(parseCloudCommand("!예약 목록").kind, "schedule");
  assert.equal(parseCloudCommand("!상태").kind, "status");
  assert.equal(parseCloudCommand("!작업").kind, "jobs");
  const selectGuide = parseCloudCommand("!선택 ondotrip");
  assert.equal(selectGuide.kind, "usage");
  assert.match(selectGuide.message, /\[ondotrip\]/);
});

test("checkAuthorization: 허용 목록 미설정/불일치 차단 및 ALLOW_ALL_USERS 허용", () => {
  const unconfigured = checkAuthorization({
    userId: "U_NEW",
    channelId: "C1",
    allowedUserIdsCsv: "",
    allowedChannelIdsCsv: "",
    allowAllUsers: "false",
  });
  assert.equal(unconfigured.allowed, false);
  assert.match(unconfigured.message, /U_NEW/);

  const denied = checkAuthorization({
    userId: "U_BAD",
    channelId: "C1",
    allowedUserIdsCsv: "U_ADMIN",
    allowedChannelIdsCsv: "",
    allowAllUsers: "false",
  });
  assert.equal(denied.allowed, false);

  const allowed = checkAuthorization({
    userId: "U_ADMIN",
    channelId: "C1",
    allowedUserIdsCsv: "U_ADMIN",
    allowedChannelIdsCsv: "C1",
    allowAllUsers: "false",
  });
  assert.equal(allowed.allowed, true);
});

test("Worker HTTP 핸들러: health, url_verification, retry 무시, app_mention 및 /slack/commands", async () => {
  const calls = [];
  const fakeFetch = async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body) });
    if (url.includes("slack.com/api/chat.postMessage")) {
      return new Response(JSON.stringify({ ok: true, ts: "1700000100.123456" }), { status: 200 });
    }
    if (url.includes("api.github.com/repos/kkubunge/Slack_AIbot/dispatches")) {
      return new Response(null, { status: 204 });
    }
    return new Response("{}", { status: 200 });
  };

  const secret = "worker_secret";
  const nowSec = 1700000000;
  const handler = createHandler({ fetch: fakeFetch, nowSec });
  const env = {
    SLACK_SIGNING_SECRET: secret,
    SLACK_BOT_TOKEN: "xoxb-test",
    GITHUB_TOKEN: "ghp-test",
    GITHUB_OWNER: "kkubunge",
    GITHUB_REPO: "Slack_AIbot",
    ALLOWED_USER_IDS: "U_OWNER",
    ALLOW_ALL_USERS: "false",
    DEFAULT_PROJECT: "slack_aibot",
  };
  const pending = [];
  const ctx = { waitUntil: (p) => pending.push(p) };

  // 1. /health
  const healthRes = await handler.fetch(new Request("https://worker.dev/health"), env, ctx);
  assert.equal(healthRes.status, 200);

  // 2. url_verification
  const challengeBody = JSON.stringify({ type: "url_verification", challenge: "xyz-999" });
  const challengeReq = new Request("https://worker.dev/slack/events", {
    method: "POST",
    headers: {
      "X-Slack-Request-Timestamp": String(nowSec),
      "X-Slack-Signature": signSlackBody(secret, String(nowSec), challengeBody),
    },
    body: challengeBody,
  });
  const challengeRes = await handler.fetch(challengeReq, env, ctx);
  assert.equal((await challengeRes.json()).challenge, "xyz-999");

  // 3. Retry 무시
  const retryReq = new Request("https://worker.dev/slack/events", {
    method: "POST",
    headers: { "X-Slack-Retry-Num": "1" },
    body: "{}",
  });
  const retryRes = await handler.fetch(retryReq, env, ctx);
  assert.equal((await retryRes.json()).ignored, "retry");
  assert.equal(calls.length, 0);

  // 4. app_mention -> Slack 접수 + GitHub dispatch
  const eventBody = JSON.stringify({
    type: "event_callback",
    event: {
      type: "app_mention",
      user: "U_OWNER",
      channel: "C_DEV",
      ts: "1700000050.000100",
      text: "<@U_BOT> [ondotrip] 글 요약 로직 확인해줘",
    },
  });
  const eventReq = new Request("https://worker.dev/slack/events", {
    method: "POST",
    headers: {
      "X-Slack-Request-Timestamp": String(nowSec),
      "X-Slack-Signature": signSlackBody(secret, String(nowSec), eventBody),
    },
    body: eventBody,
  });
  const eventRes = await handler.fetch(eventReq, env, ctx);
  assert.equal(eventRes.status, 200);
  await Promise.all(pending.splice(0));

  assert.equal(calls.length, 2);
  assert.match(calls[0].url, /chat\.postMessage/);
  assert.match(calls[1].url, /dispatches$/);
  assert.equal(calls[1].body.event_type, "slack_ai_command");
  assert.ok(Object.keys(calls[1].body.client_payload).length <= 10);
  assert.equal(calls[1].body.client_payload.project, "ondotrip");
  assert.equal(calls[1].body.client_payload.slack.status_ts, "1700000100.123456");

  // 5. /slack/commands (/ai)
  const formBody = new URLSearchParams({
    user_id: "U_OWNER",
    channel_id: "C_DEV",
    text: "!실행 traillog-dryrun",
    response_url: "https://hooks.slack.com/commands/123",
  }).toString();
  const cmdReq = new Request("https://worker.dev/slack/commands", {
    method: "POST",
    headers: {
      "X-Slack-Request-Timestamp": String(nowSec),
      "X-Slack-Signature": signSlackBody(secret, String(nowSec), formBody),
    },
    body: formBody,
  });
  const cmdRes = await handler.fetch(cmdReq, env, ctx);
  assert.equal(cmdRes.status, 200);
  const cmdJson = await cmdRes.json();
  assert.equal(cmdJson.response_type, "ephemeral");
  await Promise.all(pending.splice(0));

  assert.equal(calls.length, 4);
  assert.ok(Object.keys(calls[3].body.client_payload).length <= 10);
  assert.equal(calls[3].body.client_payload.action, "run");
  assert.equal(calls[3].body.client_payload.workflow, "traillog-dryrun");
});

test("Worker scheduled 핸들러: Cron 트리거 시 Google_OndoTrip 워크플로우 실행 및 Slack 알림", async () => {
  const calls = [];
  const fakeFetch = async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body) });
    if (url.includes("/actions/workflows/daily_post.yml/dispatches")) {
      return new Response(null, { status: 204 });
    }
    if (url.includes("slack.com/api/chat.postMessage")) {
      return new Response(JSON.stringify({ ok: true, ts: "1700000999.1" }), { status: 200 });
    }
    return new Response("{}", { status: 200 });
  };

  const handler = createHandler({ fetch: fakeFetch });
  const env = {
    GITHUB_TOKEN: "ghp-test",
    SLACK_BOT_TOKEN: "xoxb-test",
    REPORT_CHANNEL_ID: "D_OWNER",
    ONDOTRIP_REPO: "kkubunge/Google_OndoTrip",
    ONDOTRIP_WORKFLOW: "daily_post.yml",
    ONDOTRIP_REF: "main",
    ONDOTRIP_DAILY_COUNT: "5",
  };
  const pending = [];
  await handler.scheduled(
    { cron: "0 1 * * *" },
    env,
    { waitUntil: (p) => pending.push(p) },
  );
  await Promise.all(pending);

  assert.equal(calls.length, 2);
  assert.match(calls[0].url, /kkubunge\/Google_OndoTrip\/actions\/workflows\/daily_post\.yml\/dispatches$/);
  assert.equal(calls[0].body.ref, "main");
  assert.equal(calls[0].body.inputs.count, "5");
  assert.equal(calls[0].body.inputs.publish, "true");
  assert.equal(calls[0].body.inputs.slack_channel_id, "D_OWNER");
  assert.match(calls[1].url, /chat\.postMessage$/);
  assert.equal(calls[1].body.channel, "D_OWNER");
  assert.match(calls[1].body.text, /OndoTrip 일일 자동 실행 시작/);
  assert.match(calls[1].body.text, /자동 공개 발행/);
});

test("Worker !예약, !상태, !작업 명령 응답 검증", async () => {
  const slackPosts = [];
  const fakeFetch = async (url, init) => {
    if (url.includes("/actions/runs")) {
      return new Response(
        JSON.stringify({
          workflow_runs: [
            {
              name: "Daily Auto",
              event: "workflow_dispatch",
              status: "completed",
              conclusion: "success",
              run_number: 12,
              created_at: "2026-11-23T01:00:00Z",
              html_url: "https://github.com/kkubunge/Google_OndoTrip/actions/runs/1",
            },
          ],
        }),
        { status: 200 },
      );
    }
    if (url.includes("slack.com/api/chat.postMessage")) {
      const body = JSON.parse(init.body);
      slackPosts.push(body);
      return new Response(JSON.stringify({ ok: true, ts: "1700001000.1" }), { status: 200 });
    }
    return new Response("{}", { status: 200 });
  };

  const secret = "worker_secret";
  const nowSec = 1700000000;
  const handler = createHandler({ fetch: fakeFetch, nowSec });
  const env = {
    SLACK_SIGNING_SECRET: secret,
    SLACK_BOT_TOKEN: "xoxb-test",
    GITHUB_TOKEN: "ghp-test",
    ALLOWED_USER_IDS: "U_OWNER",
    ALLOW_ALL_USERS: "false",
    ONDOTRIP_REPO: "kkubunge/Google_OndoTrip",
    ONDOTRIP_DAILY_COUNT: "5",
  };
  const pending = [];
  const ctx = { waitUntil: (p) => pending.push(p) };

  // 1. /slack/commands !예약 -> 즉시 클라우드 스케줄 현황 반환
  const schedBody = new URLSearchParams({
    user_id: "U_OWNER",
    channel_id: "C_DEV",
    text: "!예약",
  }).toString();
  const schedRes = await handler.fetch(
    new Request("https://worker.dev/slack/commands", {
      method: "POST",
      headers: {
        "X-Slack-Request-Timestamp": String(nowSec),
        "X-Slack-Signature": signSlackBody(secret, String(nowSec), schedBody),
      },
      body: schedBody,
    }),
    env,
    ctx,
  );
  const schedJson = await schedRes.json();
  assert.equal(schedRes.status, 200);
  assert.match(schedJson.text, /오전 10:00 KST/);
  assert.match(schedJson.text, /Google_OndoTrip/);
  assert.match(schedJson.text, /Instagram_TrailLog/);
  assert.match(schedJson.text, /자동 공개 발행/);
  assert.match(schedJson.text, /Slack 보고/);

  // 2. /slack/events !상태 & !작업 -> GitHub runs 조회 후 Slack 전송
  for (const cmdText of ["!상태", "!작업"]) {
    const evtBody = JSON.stringify({
      type: "event_callback",
      event: {
        type: "message",
        channel_type: "im",
        user: "U_OWNER",
        channel: "D_OWNER",
        ts: "1700000055.000100",
        text: cmdText,
      },
    });
    const res = await handler.fetch(
      new Request("https://worker.dev/slack/events", {
        method: "POST",
        headers: {
          "X-Slack-Request-Timestamp": String(nowSec),
          "X-Slack-Signature": signSlackBody(secret, String(nowSec), evtBody),
        },
        body: evtBody,
      }),
      env,
      ctx,
    );
    assert.equal(res.status, 200);
  }
  await Promise.all(pending.splice(0));

  assert.equal(slackPosts.length, 2);
  assert.match(slackPosts[0].text, /클라우드 게이트웨이 상태/);
  assert.match(slackPosts[0].text, /2026-11-23 10:00 KST/);
  assert.match(slackPosts[1].text, /GitHub Actions 작업 이력/);
});
