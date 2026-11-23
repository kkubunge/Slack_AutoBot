/**
 * Slack 클라우드 게이트웨이 명령어 파서 및 접근 권한 검증 모듈.
 */

const MENTION_PREFIX_RE = /^(?:<@[A-Za-z0-9_]+>\s*)+/;
const PROJECT_PREFIX_RE = /^\[([a-zA-Z0-9_-]+)\]\s*(.+)$/s;

/**
 * 메시지 앞부분의 봇 멘션(<@U...>)을 제거한다.
 */
export function stripMentions(text) {
  return String(text || "").replace(MENTION_PREFIX_RE, "").trim();
}

/**
 * 쉼표로 구분된 ID 목록 문자열을 배열로 변환한다.
 */
function parseCsvList(csv) {
  if (!csv) return [];
  return String(csv)
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/**
 * 허용된 Slack 사용자 및 채널인지 검증한다.
 */
export function checkAuthorization({
  userId,
  channelId,
  allowedUserIdsCsv,
  allowedChannelIdsCsv,
  allowAllUsers,
}) {
  const allowAll = String(allowAllUsers || "").toLowerCase() === "true";
  const allowedUsers = parseCsvList(allowedUserIdsCsv);
  const allowedChannels = parseCsvList(allowedChannelIdsCsv);

  if (!allowAll) {
    if (allowedUsers.length === 0) {
      return {
        allowed: false,
        message:
          ":lock: *접근 권한 설정이 필요합니다.*\n" +
          `• 현재 Worker Secret \`ALLOWED_USER_IDS\`가 비어 있어 명령 실행이 차단되었습니다.\n` +
          `• 본인의 Slack User ID(\`${userId}\`)를 \`wrangler secret put ALLOWED_USER_IDS\`로 등록해 주세요.`,
      };
    }
    if (!allowedUsers.includes(userId)) {
      return {
        allowed: false,
        message: `:no_entry: 허용되지 않은 사용자입니다 (\`${userId}\`).`,
      };
    }
  }

  if (allowedChannels.length > 0 && channelId && !allowedChannels.includes(channelId)) {
    return {
      allowed: false,
      message: `:no_entry: 이 채널(\`${channelId}\`)에서는 클라우드 봇 명령이 허용되지 않았습니다.`,
    };
  }

  return { allowed: true, message: "" };
}

/**
 * [프로젝트] 접두어를 분리한다.
 */
function extractProjectPrefix(text, fallbackProject) {
  const matched = text.match(PROJECT_PREFIX_RE);
  if (!matched) {
    return { project: fallbackProject, prompt: text.trim() };
  }
  return {
    project: matched[1].trim().toLowerCase(),
    prompt: matched[2].trim(),
  };
}

/**
 * Slack 입력 텍스트를 클라우드 실행 명령 구조로 파싱한다.
 */
export function parseCloudCommand(rawText, defaultProject = "slack_aibot") {
  const cleaned = stripMentions(rawText);
  if (!cleaned) {
    return { kind: "help" };
  }

  if (cleaned.startsWith("!")) {
    const firstSpace = cleaned.search(/\s/);
    const cmd = (firstSpace === -1 ? cleaned : cleaned.slice(0, firstSpace)).toLowerCase();
    const rest = firstSpace === -1 ? "" : cleaned.slice(firstSpace).trim();

    if (cmd === "!도움" || cmd === "!help") {
      return { kind: "help" };
    }

    if (cmd === "!예약" || cmd === "!schedule") {
      return { kind: "schedule", args: rest };
    }

    if (cmd === "!상태" || cmd === "!status") {
      return { kind: "status" };
    }

    if (cmd === "!작업" || cmd === "!jobs") {
      return { kind: "jobs" };
    }

    if (cmd === "!선택" || cmd === "!use") {
      return {
        kind: "usage",
        message:
          ":information_source: 클라우드 모드에서는 기본 프로젝트 전환 대신 메시지 앞에 `[저장소]` 접두어를 붙여서 사용합니다.\n" +
          "• 예) `[ondotrip] 글 요약 로직 점검해줘`, `[traillog] 캡션 문구 고쳐줘`, `[slack_aibot] ...`\n" +
          "• 전체 저장소 목록은 `!프로젝트`로 확인하세요.",
      };
    }

    if (cmd === "!로그" || cmd === "!log" || cmd === "!중단" || cmd === "!stop") {
      return {
        kind: "usage",
        message:
          ":information_source: 클라우드 모드에서는 `!작업` 명령으로 실행 이력과 GitHub Actions 로그 링크를 확인할 수 있으며,\n" +
          "진행 중인 작업 중단은 해당 GitHub Actions 링크 페이지의 *Cancel workflow* 버튼으로 수행합니다.",
      };
    }

    if (cmd === "!스케줄러" || cmd === "!winscheduler") {
      return {
        kind: "usage",
        message:
          ":information_source: 현재는 로컬 PC가 아닌 클라우드 무인 모드로 동작 중입니다.\n" +
          "• 클라우드 정기 예약 현황은 `!예약`으로 확인하세요.\n" +
          "• 기존 PC Windows 작업 스케줄러 조회(`!스케줄러`)는 로컬 PC 모드(`run_bot.bat`) 전용입니다.",
      };
    }

    if (cmd === "!모델" || cmd === "!model" || cmd === "!모드" || cmd === "!mode" || cmd === "!새프로젝트" || cmd === "!new") {
      return {
        kind: "usage",
        message:
          `:information_source: \`${cmd}\` 명령은 로컬 PC 모드(\`run_bot.bat\`) 전용입니다.\n` +
          "• 클라우드 모드에서는 일회성 모드 지정(`!분석` / `!개발`)과 `cloud/cloud_repos.yaml` 저장소 설정을 사용합니다.",
      };
    }

    if (cmd === "!프로젝트" || cmd === "!projects") {
      return {
        kind: "dispatch",
        action: "list",
        project: defaultProject,
        mode: "plan",
        prompt: "projects",
        workflow: "",
        extraArgs: "",
      };
    }

    if (cmd === "!실행" || cmd === "!run") {
      if (!rest) {
        return {
          kind: "dispatch",
          action: "list",
          project: defaultProject,
          mode: "plan",
          prompt: "workflows",
          workflow: "",
          extraArgs: "",
        };
      }
      const spaceIdx = rest.search(/\s/);
      const workflow = (spaceIdx === -1 ? rest : rest.slice(0, spaceIdx)).toLowerCase();
      const extraArgs = spaceIdx === -1 ? "" : rest.slice(spaceIdx).trim();
      return {
        kind: "dispatch",
        action: "run",
        project: defaultProject,
        mode: "dev",
        prompt: "",
        workflow,
        extraArgs,
      };
    }

    if (cmd === "!분석" || cmd === "!plan") {
      if (!rest) {
        return { kind: "usage", message: "사용법: `!분석 [프로젝트] <분석 또는 설계 요청>`" };
      }
      const { project, prompt } = extractProjectPrefix(rest, defaultProject);
      return {
        kind: "dispatch",
        action: "ai",
        project,
        mode: "plan",
        prompt,
        workflow: "",
        extraArgs: "",
      };
    }

    if (cmd === "!개발" || cmd === "!dev") {
      if (!rest) {
        return { kind: "usage", message: "사용법: `!개발 [프로젝트] <개발 지시 내용>`" };
      }
      const { project, prompt } = extractProjectPrefix(rest, defaultProject);
      return {
        kind: "dispatch",
        action: "ai",
        project,
        mode: "dev",
        prompt,
        workflow: "",
        extraArgs: "",
      };
    }

    return {
      kind: "usage",
      message: `:question: 알 수 없는 클라우드 명령어입니다: \`${cmd}\`\n\`!도움\`으로 사용 가능한 명령어를 확인하세요.`,
    };
  }

  const { project, prompt } = extractProjectPrefix(cleaned, defaultProject);
  return {
    kind: "dispatch",
    action: "ai",
    project,
    mode: "dev",
    prompt,
    workflow: "",
    extraArgs: "",
  };
}

/**
 * 클라우드 버전 도움말 문자열을 반환한다.
 */
export function buildCloudHelpText() {
  return (
    "*Slack_AIbot 클라우드 무인 모드 도움말*\n" +
    "_(로컬 PC 전원 없이 Cloudflare Workers + GitHub Actions에서 실행됩니다)_\n\n" +
    "*1. 클라우드 AI 코드 개발 & PR 생성*\n" +
    "• `[저장소] 개발 지시` → 해당 GitHub 저장소에서 AI가 코드 수정 후 브랜치·PR 자동 생성\n" +
    "• `!개발 [저장소] 지시` / `!dev [저장소] 지시` → `dev` 모드(코드 수정·검증·PR)\n" +
    "• `!분석 [저장소] 지시` / `!plan [저장소] 지시` → `plan` 모드(읽기 전용 코드 분석·계획)\n" +
    "• 슬래시 명령: `/ai [ondotrip] 글 작성 로직 구조 분석해줘`\n\n" +
    "*2. 원격 저장소·워크플로우·예약·상태 조회*\n" +
    "• `!프로젝트` / `!projects` → 등록된 GitHub 저장소 및 실행 프리셋 목록 조회\n" +
    "• `!실행` / `!run` → 원격 실행 가능한 GitHub Actions 워크플로우 목록 조회\n" +
    "• `!실행 <프리셋>` → 등록된 원격 워크플로우 즉시 실행 (예: `!실행 ondotrip-status`, `!실행 traillog-dryrun`)\n" +
    "• `!예약` / `!schedule` → 클라우드 정기 자동 실행 스케줄 현황 조회\n" +
    "• `!상태` / `!status` → 클라우드 게이트웨이 및 저장소별 최근 실행 상태 조회\n" +
    "• `!작업` / `!jobs` → 최근 GitHub Actions 작업 이력 및 로그 링크 조회\n" +
    "• `!도움` / `!help` → 이 도움말 표시"
  );
}
