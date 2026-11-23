/**
 * Cloudflare Workers 기반 Slack Webhook 수신 및 GitHub repository_dispatch 중계 게이트웨이.
 */

import { buildCloudHelpText, checkAuthorization, parseCloudCommand } from "./command_parser.js";
import { verifySlackSignature } from "./slack_verify.js";

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

function generateJobId() {
  return crypto.randomUUID().replace(/-/g, "").slice(0, 8);
}

async function callSlackApi(fetchFn, botToken, method, body) {
  if (!botToken) {
    return { ok: false, error: "SLACK_BOT_TOKEN 미설정" };
  }
  const resp = await fetchFn(`https://slack.com/api/${method}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${botToken}`,
      "Content-Type": "application/json; charset=utf-8",
    },
    body: JSON.stringify(body),
  });
  try {
    return await resp.json();
  } catch {
    return { ok: false, error: `Slack API HTTP ${resp.status}` };
  }
}

async function triggerGitHubDispatch(fetchFn, env, clientPayload) {
  const owner = env.GITHUB_OWNER || "kkubunge";
  const repo = env.GITHUB_REPO || "Slack_AIbot";
  const token = env.GITHUB_TOKEN || "";
  if (!token) {
    return { ok: false, error: "GITHUB_TOKEN 시크릿이 설정되지 않았습니다." };
  }

  const url = `https://api.github.com/repos/${owner}/${repo}/dispatches`;
  const resp = await fetchFn(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json; charset=utf-8",
      "User-Agent": "Slack-AIbot-Gateway/1.0",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({
      event_type: "slack_ai_command",
      client_payload: clientPayload,
    }),
  });

  if (resp.status === 204 || resp.ok) {
    return { ok: true };
  }
  const errText = await resp.text();
  return { ok: false, error: `GitHub API 오류 (${resp.status}): ${errText.slice(0, 300)}` };
}

function buildAckText(jobId, parsed) {
  if (parsed.action === "list") {
    return `:hourglass_flowing_sand: *클라우드 저장소/워크플로우 목록 조회 중...* (\`${jobId}\`)`;
  }
  if (parsed.action === "run") {
    return `:hourglass_flowing_sand: *원격 워크플로우 실행 요청 접수됨* (\`${jobId}\`)\n• 대상: \`${parsed.workflow}\` | GitHub Actions 워커를 시작합니다...`;
  }
  const title = parsed.mode === "plan" ? "클라우드 AI 분석 접수됨" : "클라우드 AI 개발 접수됨";
  return (
    `:hourglass_flowing_sand: *${title}* (\`${jobId}\`)\n` +
    `• 저장소: \`${parsed.project}\` | 모드: \`${parsed.mode}\`\n` +
    "• GitHub Actions 클라우드 워커에서 실행을 시작합니다..."
  );
}

async function dispatchToCloudWorker({
  fetchFn,
  env,
  parsed,
  userId,
  channelId,
  threadTs,
  responseUrl = "",
}) {
  const jobId = generateJobId();
  let statusTs = "";

  if (channelId && env.SLACK_BOT_TOKEN) {
    const postPayload = {
      channel: channelId,
      text: buildAckText(jobId, parsed),
    };
    if (threadTs) {
      postPayload.thread_ts = threadTs;
    }
    const ackRes = await callSlackApi(fetchFn, env.SLACK_BOT_TOKEN, "chat.postMessage", postPayload);
    if (ackRes && ackRes.ok && ackRes.ts) {
      statusTs = String(ackRes.ts);
    }
  }

  const clientPayload = {
    job_id: jobId,
    action: parsed.action,
    project: parsed.project,
    mode: parsed.mode,
    prompt: parsed.prompt,
    workflow: parsed.workflow || "",
    extra_args: parsed.extraArgs || "",
    channel_id: channelId || "",
    thread_ts: threadTs || statusTs || "",
    status_ts: statusTs,
    user_id: userId || "",
    response_url: responseUrl,
  };

  const ghRes = await triggerGitHubDispatch(fetchFn, env, clientPayload);
  if (!ghRes.ok) {
    const errText = `:x: *클라우드 워커 호출 실패* (\`${jobId}\`)\n\`\`\`\n${ghRes.error}\n\`\`\``;
    if (statusTs && env.SLACK_BOT_TOKEN) {
      await callSlackApi(fetchFn, env.SLACK_BOT_TOKEN, "chat.update", {
        channel: channelId,
        ts: statusTs,
        text: errText,
      });
    } else if (channelId && env.SLACK_BOT_TOKEN) {
      await callSlackApi(fetchFn, env.SLACK_BOT_TOKEN, "chat.postMessage", {
        channel: channelId,
        ...(threadTs ? { thread_ts: threadTs } : {}),
        text: errText,
      });
    }
  }
}

/**
 * 테스트 시 의존성(fetch, nowSec 등)을 주입할 수 있는 Cloudflare Worker 핸들러 생성기.
 */
export function createHandler(deps = {}) {
  const fetchFn = deps.fetch || globalThis.fetch;

  return {
    async fetch(request, env, ctx) {
      const url = new URL(request.url);

      if (request.method === "GET" && url.pathname === "/health") {
        return jsonResponse({ ok: true, service: "slack-aibot-gateway" });
      }

      if (request.method !== "POST") {
        return jsonResponse({ ok: false, error: "Method Not Allowed" }, 405);
      }

      // Slack 재시도 요청(타임아웃 재전송)은 중복 실행 방지를 위해 즉시 200으로 무시
      if (request.headers.get("X-Slack-Retry-Num")) {
        return jsonResponse({ ok: true, ignored: "retry" });
      }

      const rawBody = await request.text();
      const verify = await verifySlackSignature({
        signingSecret: env.SLACK_SIGNING_SECRET,
        timestamp: request.headers.get("X-Slack-Request-Timestamp") || "",
        signature: request.headers.get("X-Slack-Signature") || "",
        rawBody,
        nowSec: typeof deps.nowSec === "function" ? deps.nowSec() : deps.nowSec,
      });
      if (!verify.ok) {
        return jsonResponse({ ok: false, error: verify.reason || "Unauthorized" }, 401);
      }

      if (url.pathname === "/slack/events") {
        let body;
        try {
          body = JSON.parse(rawBody);
        } catch {
          return jsonResponse({ ok: false, error: "Invalid JSON" }, 400);
        }

        if (body.type === "url_verification") {
          return jsonResponse({ challenge: body.challenge });
        }

        if (body.type !== "event_callback" || !body.event) {
          return jsonResponse({ ok: true });
        }

        const evt = body.event;
        const isAppMention = evt.type === "app_mention";
        const isDmMessage = evt.type === "message" && evt.channel_type === "im";
        if ((!isAppMention && !isDmMessage) || evt.bot_id || evt.subtype) {
          return jsonResponse({ ok: true });
        }

        const userId = String(evt.user || "");
        const channelId = String(evt.channel || "");
        const threadTs = String(evt.thread_ts || evt.ts || "");
        const text = String(evt.text || "");

        const work = async () => {
          const auth = checkAuthorization({
            userId,
            channelId,
            allowedUserIdsCsv: env.ALLOWED_USER_IDS,
            allowedChannelIdsCsv: env.ALLOWED_CHANNEL_IDS,
            allowAllUsers: env.ALLOW_ALL_USERS,
          });
          if (!auth.allowed) {
            await callSlackApi(fetchFn, env.SLACK_BOT_TOKEN, "chat.postMessage", {
              channel: channelId,
              thread_ts: threadTs,
              text: auth.message,
            });
            return;
          }

          const parsed = parseCloudCommand(text, env.DEFAULT_PROJECT || "slack_aibot");
          if (parsed.kind === "help" || parsed.kind === "usage") {
            await callSlackApi(fetchFn, env.SLACK_BOT_TOKEN, "chat.postMessage", {
              channel: channelId,
              thread_ts: threadTs,
              text: parsed.kind === "help" ? buildCloudHelpText() : parsed.message,
            });
            return;
          }

          await dispatchToCloudWorker({
            fetchFn,
            env,
            parsed,
            userId,
            channelId,
            threadTs,
          });
        };

        if (ctx && typeof ctx.waitUntil === "function") {
          ctx.waitUntil(work());
        } else {
          await work();
        }
        return jsonResponse({ ok: true });
      }

      if (url.pathname === "/slack/commands") {
        const params = new URLSearchParams(rawBody);
        const userId = params.get("user_id") || "";
        const channelId = params.get("channel_id") || "";
        const text = params.get("text") || "";
        const responseUrl = params.get("response_url") || "";

        const auth = checkAuthorization({
          userId,
          channelId,
          allowedUserIdsCsv: env.ALLOWED_USER_IDS,
          allowedChannelIdsCsv: env.ALLOWED_CHANNEL_IDS,
          allowAllUsers: env.ALLOW_ALL_USERS,
        });
        if (!auth.allowed) {
          return jsonResponse({ response_type: "ephemeral", text: auth.message });
        }

        const parsed = parseCloudCommand(text, env.DEFAULT_PROJECT || "slack_aibot");
        if (parsed.kind === "help") {
          return jsonResponse({ response_type: "ephemeral", text: buildCloudHelpText() });
        }
        if (parsed.kind === "usage") {
          return jsonResponse({ response_type: "ephemeral", text: parsed.message });
        }

        const work = dispatchToCloudWorker({
          fetchFn,
          env,
          parsed,
          userId,
          channelId,
          threadTs: "",
          responseUrl,
        });

        if (ctx && typeof ctx.waitUntil === "function") {
          ctx.waitUntil(work);
        } else {
          await work;
        }

        return jsonResponse({
          response_type: "in_channel",
          text: `:rocket: 클라우드 작업 요청을 접수했습니다 (\`${parsed.project || parsed.workflow}\`). 결과를 채널 스레드로 보고합니다.`,
        });
      }

      return jsonResponse({ ok: false, error: "Not Found" }, 404);
    },
  };
}

export default createHandler();
