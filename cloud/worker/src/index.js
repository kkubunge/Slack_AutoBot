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

async function triggerWorkflowDispatch(
  fetchFn,
  env,
  { repoFullName, workflowFile, ref = "main", inputs = {} },
) {
  const token = env.GITHUB_TOKEN || "";
  if (!token) {
    return { ok: false, error: "GITHUB_TOKEN 시크릿이 설정되지 않았습니다." };
  }
  const url = `https://api.github.com/repos/${repoFullName}/actions/workflows/${workflowFile}/dispatches`;
  const resp = await fetchFn(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json; charset=utf-8",
      "User-Agent": "Slack-AIbot-Gateway/1.0",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({ ref, inputs }),
  });

  if (resp.status === 204 || resp.ok) {
    return {
      ok: true,
      actionsUrl: `https://github.com/${repoFullName}/actions/workflows/${workflowFile}`,
    };
  }
  const errText = await resp.text();
  return { ok: false, error: `Workflow Dispatch 오류 (${resp.status}): ${errText.slice(0, 300)}` };
}

function formatKstTime(isoStr) {
  if (!isoStr) return "-";
  const ms = Date.parse(isoStr);
  if (!Number.isFinite(ms)) return isoStr;
  const kst = new Date(ms + 9 * 3600 * 1000);
  const yyyy = kst.getUTCFullYear();
  const mm = String(kst.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(kst.getUTCDate()).padStart(2, "0");
  const hh = String(kst.getUTCHours()).padStart(2, "0");
  const mi = String(kst.getUTCMinutes()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd} ${hh}:${mi} KST`;
}

function buildCloudScheduleText(env, args = "") {
  const ondoRepo = env.ONDOTRIP_REPO || "kkubunge/Google_OndoTrip";
  const ondoCount = String(env.ONDOTRIP_DAILY_COUNT || "5");
  const lines = [];

  if (args) {
    lines.push(
      ":information_source: *클라우드 무인 모드*의 정기 스케줄은 `wrangler.toml` (`[triggers]`)과 워크플로우 YAML로 관리됩니다. (`!예약 추가/삭제`는 로컬 PC 모드 전용)\n",
    );
  }

  lines.push(
    ":alarm_clock: *클라우드 정기 자동 실행 예약 현황*",
    `• *Google_OndoTrip (\`ondotrip\`)*: Cloudflare Workers Cron (\`0 1 * * *\`, *매일 오전 10:00 KST*)`,
    `  - 동작: \`${ondoRepo}\` 자동 공개 발행 ${ondoCount}건 및 Slack 보고`,
    "• *Instagram_TrailLog (`traillog`)*: GitHub Actions Cron (*매일 09:00 · 11:00 · 13:00 · 15:00 · 17:00 KST*)",
    "  - 동작: `kkubunge/Instagram_TrailLog` 미게시 코스 카드뉴스 자동 게시 및 Slack 보고 (일일 상한 5건)",
    "",
    "_수동 즉시 실행: `!실행 ondotrip` · `!실행 ondotrip-status` · `!실행 traillog-dryrun` · `!실행 traillog`_",
  );
  return lines.join("\n");
}

async function fetchRecentWorkflowRuns(fetchFn, env, perRepo = 2) {
  const token = env.GITHUB_TOKEN || "";
  if (!token) {
    return { ok: false, error: "GITHUB_TOKEN 시크릿이 설정되지 않았습니다.", items: [] };
  }

  const owner = env.GITHUB_OWNER || "kkubunge";
  const targets = [
    { key: "ondotrip", repo: env.ONDOTRIP_REPO || "kkubunge/Google_OndoTrip" },
    { key: "traillog", repo: "kkubunge/Instagram_TrailLog" },
    { key: "slack_aibot", repo: `${owner}/${env.GITHUB_REPO || "Slack_AIbot"}` },
  ];

  const items = await Promise.all(
    targets.map(async (target) => {
      try {
        const resp = await fetchFn(
          `https://api.github.com/repos/${target.repo}/actions/runs?per_page=${perRepo}`,
          {
            method: "GET",
            headers: {
              Authorization: `Bearer ${token}`,
              Accept: "application/vnd.github+json",
              "User-Agent": "Slack-AIbot-Gateway/1.0",
              "X-GitHub-Api-Version": "2022-11-28",
            },
          },
        );
        if (!resp.ok) {
          return { ...target, error: `HTTP ${resp.status}`, runs: [] };
        }
        const data = await resp.json();
        return { ...target, error: "", runs: Array.isArray(data.workflow_runs) ? data.workflow_runs : [] };
      } catch (err) {
        return { ...target, error: String(err), runs: [] };
      }
    }),
  );

  return { ok: true, items };
}

function formatRunOutcome(run) {
  if (!run) return "실행 이력 없음";
  const state = run.status === "completed" ? (run.conclusion || "completed") : (run.status || "running");
  const icon = state === "success" ? ":white_check_mark:" : state === "failure" ? ":x:" : ":hourglass_flowing_sand:";
  return `${icon} \`${state}\` (${formatKstTime(run.created_at)}) — <${run.html_url}|#${run.run_number} 로그>`;
}

async function buildCloudStatusText(fetchFn, env) {
  const res = await fetchRecentWorkflowRuns(fetchFn, env, 1);
  const lines = [
    "*AutoBot(오토봇) 클라우드 게이트웨이 상태*",
    `• 게이트웨이: \`온라인\` (Cloudflare Workers)`,
    `• 기본 저장소: \`${env.DEFAULT_PROJECT || "slack_aibot"}\` | 일일 Cron: \`0 1 * * *\` (매일 10:00 KST \`ondotrip\`)`,
    "",
    "*저장소별 최근 실행 상태*",
  ];

  if (!res.ok) {
    lines.append ? lines.append(`• 오류: ${res.error}`) : lines.push(`• 오류: ${res.error}`);
    return lines.join("\n");
  }

  for (const item of res.items) {
    if (item.error) {
      lines.push(`• \`${item.key}\` (\`${item.repo}\`): 조회 오류 (${item.error})`);
    } else {
      lines.push(`• \`${item.key}\` (\`${item.repo}\`): ${formatRunOutcome(item.runs[0])}`);
    }
  }
  return lines.join("\n");
}

async function buildCloudJobsText(fetchFn, env) {
  const res = await fetchRecentWorkflowRuns(fetchFn, env, 2);
  if (!res.ok) {
    return `:x: 작업 이력 조회 실패: ${res.error}`;
  }

  const lines = ["*클라우드 최근 GitHub Actions 작업 이력*"];
  for (const item of res.items) {
    lines.push(`\n*[${item.key}]* (\`${item.repo}\`)`);
    if (item.error) {
      lines.push(`• 조회 오류: ${item.error}`);
      continue;
    }
    if (item.runs.length === 0) {
      lines.push("• 실행 이력 없음");
      continue;
    }
    for (const run of item.runs) {
      lines.push(`• ${run.name || "Workflow"} (${run.event || "-"}) — ${formatRunOutcome(run)}`);
    }
  }
  return lines.join("\n");
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
    slack: {
      channel_id: channelId || "",
      thread_ts: threadTs || "",
      status_ts: statusTs,
      user_id: userId || "",
      response_url: responseUrl,
    },
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
        // DM에서는 스레드 없이 본문에 바로 응답하고, 채널에서는 기존 스레드 안의 호출일 때만 스레드를 유지한다.
        const threadTs = isDmMessage ? "" : String(evt.thread_ts || "");
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
              ...(threadTs ? { thread_ts: threadTs } : {}),
              text: auth.message,
            });
            return;
          }

          const parsed = parseCloudCommand(text, env.DEFAULT_PROJECT || "slack_aibot");
          if (parsed.kind === "help" || parsed.kind === "usage" || parsed.kind === "schedule") {
            const replyText =
              parsed.kind === "help"
                ? buildCloudHelpText()
                : parsed.kind === "schedule"
                  ? buildCloudScheduleText(env, parsed.args)
                  : parsed.message;
            await callSlackApi(fetchFn, env.SLACK_BOT_TOKEN, "chat.postMessage", {
              channel: channelId,
              ...(threadTs ? { thread_ts: threadTs } : {}),
              text: replyText,
            });
            return;
          }

          if (parsed.kind === "status" || parsed.kind === "jobs") {
            const reportText =
              parsed.kind === "status"
                ? await buildCloudStatusText(fetchFn, env)
                : await buildCloudJobsText(fetchFn, env);
            await callSlackApi(fetchFn, env.SLACK_BOT_TOKEN, "chat.postMessage", {
              channel: channelId,
              ...(threadTs ? { thread_ts: threadTs } : {}),
              text: reportText,
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
        if (parsed.kind === "schedule") {
          return jsonResponse({
            response_type: "ephemeral",
            text: buildCloudScheduleText(env, parsed.args),
          });
        }
        if (parsed.kind === "usage") {
          return jsonResponse({ response_type: "ephemeral", text: parsed.message });
        }
        if (parsed.kind === "status" || parsed.kind === "jobs") {
          const queryWork = (async () => {
            const reportText =
              parsed.kind === "status"
                ? await buildCloudStatusText(fetchFn, env)
                : await buildCloudJobsText(fetchFn, env);
            const postRes = await callSlackApi(fetchFn, env.SLACK_BOT_TOKEN, "chat.postMessage", {
              channel: channelId,
              text: reportText,
            });
            if ((!postRes || !postRes.ok) && responseUrl) {
              await fetchFn(responseUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json; charset=utf-8" },
                body: JSON.stringify({ response_type: "ephemeral", text: reportText }),
              });
            }
          })();
          if (ctx && typeof ctx.waitUntil === "function") {
            ctx.waitUntil(queryWork);
          } else {
            await queryWork;
          }
          return jsonResponse({
            response_type: "ephemeral",
            text: ":mag: 클라우드 상태 및 실행 이력을 조회 중입니다...",
          });
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
          response_type: "ephemeral",
          text: `:rocket: 클라우드 작업 요청을 접수했습니다 (\`${parsed.project || parsed.workflow}\`). 진행 상태와 결과를 채널에 보고합니다.`,
        });
      }

      return jsonResponse({ ok: false, error: "Not Found" }, 404);
    },

    async scheduled(controller, env, ctx) {
      const repoFullName = env.ONDOTRIP_REPO || "kkubunge/Google_OndoTrip";
      const workflowFile = env.ONDOTRIP_WORKFLOW || "daily_post.yml";
      const ref = env.ONDOTRIP_REF || "main";
      const count = String(env.ONDOTRIP_DAILY_COUNT || "5");
      const reportChannel = env.REPORT_CHANNEL_ID || "";

      const task = (async () => {
        const res = await triggerWorkflowDispatch(fetchFn, env, {
          repoFullName,
          workflowFile,
          ref,
          inputs: {
            count,
            publish: "true",
            preview_only: "false",
            status_only: "false",
            slack_channel_id: reportChannel,
          },
        });

        if (reportChannel && env.SLACK_BOT_TOKEN) {
          const text = res.ok
            ? `:alarm_clock: *OndoTrip 일일 자동 실행 시작* (Cloudflare Cron \`${controller?.cron || "0 1 * * *"}\`)\n• 대상: \`${repoFullName}\` (${count}건 자동 공개 발행)\n• <${res.actionsUrl}|GitHub Actions 진행 상황 보기>`
            : `:x: *OndoTrip 일일 자동 실행 트리거 실패*\n• 대상: \`${repoFullName}\`\n\`\`\`\n${res.error}\n\`\`\``;
          await callSlackApi(fetchFn, env.SLACK_BOT_TOKEN, "chat.postMessage", {
            channel: reportChannel,
            text,
          });
        }
      })();

      if (ctx && typeof ctx.waitUntil === "function") {
        ctx.waitUntil(task);
      } else {
        await task;
      }
    },
  };
}

export default createHandler();
