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
  const repo = env.GITHUB_REPO || "Slack_AutoBot";
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
      "User-Agent": "Slack-AutoBot-Gateway/1.0",
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
      "User-Agent": "Slack-AutoBot-Gateway/1.0",
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

function formatKstDate(dateOrIso) {
  let ms;
  if (typeof dateOrIso === "number") {
    ms = dateOrIso;
  } else {
    const normalized = String(dateOrIso || "").replace(/([+-]\d{2})(\d{2})$/, "$1:$2");
    ms = Date.parse(normalized);
  }
  if (!Number.isFinite(ms)) return "";
  const kst = new Date(ms + 9 * 3600 * 1000);
  const yyyy = kst.getUTCFullYear();
  const mm = String(kst.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(kst.getUTCDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
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
  const trailRepo = env.TRAILLOG_REPO || "kkubunge/Instagram_TrailLog";
  const trailTarget = String(env.TRAILLOG_DAILY_TARGET || "5");
  const lines = [];

  if (args) {
    lines.push(
      ":information_source: *클라우드 무인 모드*의 정기 스케줄은 `wrangler.toml` (`[triggers]`)로 통합 관리됩니다. (`!예약 추가/삭제`는 로컬 PC 모드 전용)\n",
    );
  }

  lines.push(
    ":alarm_clock: *클라우드 정기 자동 실행 예약 현황*",
    `• *Google_OndoTrip (\`온도트립\`, \`ondotrip\`)*: Cloudflare Workers Cron (\`0 1 * * *\`, *매일 오전 10:00 KST*)`,
    `  - 동작: \`${ondoRepo}\` 자동 공개 발행 ${ondoCount}건 및 Slack 보고`,
    `• *Instagram_TrailLog (\`인스타\`, \`traillog\`)*: Cloudflare Workers Cron (\`0 0,2,4,6,8 * * *\`, *매일 09:00 · 11:00 · 13:00 · 15:00 · 17:00 KST*)`,
    `  - 동작: \`${trailRepo}\` 미게시 코스 카드뉴스 자동 게시 및 Slack 보고 (목표 ${trailTarget}건, 트리거 실패 시 즉시 알림)`,
    "• *일일 누락·지연·실패 감시 (Watchdog)*: Cloudflare Workers Cron (`30 10 * * *`, *매일 19:30 KST*)",
    "  - 동작: 당일 실행 누락·20분 이상 지연·실패·목표 미달 감지 시 Slack 경고 알림",
    "",
    "_수동 즉시 실행: `!실행 온도트립` · `!실행 온도트립 상태` · `!실행 인스타 드라이런` · `!실행 인스타`_",
  );
  return lines.join("\n");
}

async function runDailyHealthWatchdog(fetchFn, env, nowMs = Date.now()) {
  const ghToken = env.GITHUB_TOKEN || "";
  const botToken = env.SLACK_BOT_TOKEN || "";
  const reportChannel = env.REPORT_CHANNEL_ID || "";
  const todayKst = formatKstDate(nowMs);
  const ondoRepo = env.ONDOTRIP_REPO || "kkubunge/Google_OndoTrip";
  const trailRepo = env.TRAILLOG_REPO || "kkubunge/Instagram_TrailLog";
  const trailTarget = Math.max(1, Number.parseInt(String(env.TRAILLOG_DAILY_TARGET || "5"), 10) || 5);

  const issues = [];
  if (!ghToken) {
    issues.push("• `GITHUB_TOKEN` 시크릿이 설정되지 않아 일일 실행 점검을 수행할 수 없습니다.");
  } else {
    const ghHeaders = {
      Authorization: `Bearer ${ghToken}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "Slack-AutoBot-Gateway/1.0",
      "X-GitHub-Api-Version": "2022-11-28",
    };

    const targets = [
      { label: "Google_OndoTrip (온도트립)", repo: ondoRepo },
      { label: "Instagram_TrailLog (인스타)", repo: trailRepo },
    ];

    for (const target of targets) {
      try {
        const resp = await fetchFn(
          `https://api.github.com/repos/${target.repo}/actions/runs?per_page=10`,
          { method: "GET", headers: ghHeaders },
        );
        if (!resp.ok) {
          issues.push(`• *${target.label}*: GitHub 실행 이력 조회 실패 (HTTP ${resp.status})`);
          continue;
        }
        const data = await resp.json();
        const runs = Array.isArray(data.workflow_runs) ? data.workflow_runs : [];
        const todayRuns = runs.filter((r) => formatKstDate(r.created_at) === todayKst);

        if (todayRuns.length === 0) {
          issues.push(
            `• *${target.label}*: 오늘(\`${todayKst}\`) 실행 이력이 없습니다 (스케줄 드롭/미실행 의심). — <https://github.com/${target.repo}/actions|Actions 확인>`,
          );
          continue;
        }

        const latestCompleted = todayRuns.find((r) => r.status === "completed");
        if (
          latestCompleted &&
          ["failure", "cancelled", "timed_out"].includes(latestCompleted.conclusion)
        ) {
          issues.push(
            `• *${target.label}*: 오늘 최근 실행이 \`${latestCompleted.conclusion}\` 상태로 종료되었습니다. — <${latestCompleted.html_url}|#${latestCompleted.run_number} 로그>`,
          );
        }

        const stuckRuns = todayRuns.filter((r) => {
          if (r.status !== "queued" && r.status !== "in_progress") return false;
          const createdMs = Date.parse(r.created_at || "");
          return Number.isFinite(createdMs) && nowMs - createdMs >= 20 * 60 * 1000;
        });
        if (stuckRuns.length > 0) {
          const r = stuckRuns[0];
          const elapsedMin = Math.floor((nowMs - Date.parse(r.created_at)) / 60000);
          issues.push(
            `• *${target.label}*: 실행이 \`${r.status}\` 상태로 ${elapsedMin}분째 지연/정체 중입니다. — <${r.html_url}|#${r.run_number} 로그>`,
          );
        }
      } catch (err) {
        issues.push(`• *${target.label}*: 실행 이력 점검 예외 (${String(err)})`);
      }
    }

    // TrailLog 일일 게시 목표 건수 확인
    try {
      const histResp = await fetchFn(
        `https://api.github.com/repos/${trailRepo}/contents/published_history.json`,
        { method: "GET", headers: ghHeaders },
      );
      if (histResp.ok) {
        const fileJson = await histResp.json();
        if (fileJson && fileJson.content) {
          const binStr = atob(String(fileJson.content).replace(/\s+/g, ""));
          const bytes = Uint8Array.from(binStr, (ch) => ch.charCodeAt(0));
          const parsed = JSON.parse(new TextDecoder("utf-8").decode(bytes));
          const published = Array.isArray(parsed?.published) ? parsed.published : [];
          const todayPublished = published.filter(
            (item) => formatKstDate(item?.published_at) === todayKst,
          ).length;
          if (todayPublished < trailTarget) {
            issues.push(
              `• *Instagram_TrailLog (인스타)*: 오늘(\`${todayKst}\`) 게시 건수가 목표(${trailTarget}건)에 미달했습니다 (\`${todayPublished}/${trailTarget}건\`). — <https://github.com/${trailRepo}/actions|Actions 확인>`,
            );
          }
        }
      }
    } catch {
      // 이력 파일 조회 예외 시 runs 기반 검사 결과만 유지
    }
  }

  if (issues.length > 0 && reportChannel && botToken) {
    const text = [
      ":warning: *일일 자동화 지연·누락·실패 감지 알림*",
      `• 기준 일자: \`${todayKst}\` (19:30 KST 일일 점검)`,
      ...issues,
      "",
      "_필요 시 `!실행 온도트립` 또는 `!실행 인스타`로 즉시 재실행할 수 있습니다._",
    ].join("\n");
    await callSlackApi(fetchFn, botToken, "chat.postMessage", {
      channel: reportChannel,
      text,
    });
  }

  return { ok: issues.length === 0, issues };
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
    { key: "slack_autobot", repo: `${owner}/${env.GITHUB_REPO || "Slack_AutoBot"}` },
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
              "User-Agent": "Slack-AutoBot-Gateway/1.0",
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
    `• 기본 저장소: \`${env.DEFAULT_PROJECT || "slack_autobot"}\` | 일일 Cron: \`0 1 * * *\` (매일 10:00 KST \`ondotrip\`)`,
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
  const nowMsFn =
    typeof deps.nowMs === "function"
      ? deps.nowMs
      : () => (typeof deps.nowMs === "number" ? deps.nowMs : Date.now());

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

          const parsed = parseCloudCommand(text, env.DEFAULT_PROJECT || "slack_autobot");
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

        const parsed = parseCloudCommand(text, env.DEFAULT_PROJECT || "slack_autobot");
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
      const cron = String(controller?.cron || "0 1 * * *");
      const reportChannel = env.REPORT_CHANNEL_ID || "";

      const task = (async () => {
        // 1) 매일 09/11/13/15/17시 KST: Instagram_TrailLog 자동 게시 실행
        if (cron === "0 0,2,4,6,8 * * *") {
          const trailRepo = env.TRAILLOG_REPO || "kkubunge/Instagram_TrailLog";
          const trailWf = env.TRAILLOG_WORKFLOW || "auto_publish.yml";
          const trailRef = env.TRAILLOG_REF || "main";
          const res = await triggerWorkflowDispatch(fetchFn, env, {
            repoFullName: trailRepo,
            workflowFile: trailWf,
            ref: trailRef,
            inputs: {
              brd: "걷기",
              style: "auto",
              dry_run: "false",
              slack_channel_id: reportChannel,
            },
          });

          // 성공 시에는 워크플로우 완료 단계에서 게시물 링크와 함께 DM을 보고하고, 트리거 실패 시에만 즉시 경고한다.
          if (!res.ok && reportChannel && env.SLACK_BOT_TOKEN) {
            await callSlackApi(fetchFn, env.SLACK_BOT_TOKEN, "chat.postMessage", {
              channel: reportChannel,
              text: `:x: *TrailLog 자동 실행 트리거 실패* (Cloudflare Cron \`${cron}\`)\n• 대상: \`${trailRepo}\`\n\`\`\`\n${res.error}\n\`\`\``,
            });
          }
          return;
        }

        // 2) 매일 19:30 KST: 당일 실행 누락·지연·실패·목표 미달 감시 워치독
        if (cron === "30 10 * * *") {
          await runDailyHealthWatchdog(fetchFn, env, nowMsFn());
          return;
        }

        // 3) 기본(매일 10:00 KST, 0 1 * * *): Google_OndoTrip 일일 자동 공개 발행
        const repoFullName = env.ONDOTRIP_REPO || "kkubunge/Google_OndoTrip";
        const workflowFile = env.ONDOTRIP_WORKFLOW || "daily_post.yml";
        const ref = env.ONDOTRIP_REF || "main";
        const count = String(env.ONDOTRIP_DAILY_COUNT || "5");
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
            ? `:alarm_clock: *OndoTrip 일일 자동 실행 시작* (Cloudflare Cron \`${cron}\`)\n• 대상: \`${repoFullName}\` (${count}건 자동 공개 발행)\n• <${res.actionsUrl}|GitHub Actions 진행 상황 보기>`
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
