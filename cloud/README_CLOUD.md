# Slack_AIbot 클라우드 무인 모드 (Cloudflare Workers + GitHub Actions)

로컬 PC를 켜두지 않고도 **Slack ↔ Cloudflare Workers ↔ GitHub Actions + Gemini API** 연동으로 24시간 언제든 AI 코드 개발·분석·PR 자동 생성 및 원격 워크플로우 실행 결과를 Slack으로 보고받는 무인 모드입니다.

---

## 동작 구조

```text
[Slack]  "@Slack_AIbot [ondotrip] 글 요약 로직 점검해줘"  (또는 /ai ...)
   │  (Events API / Slash Command HTTPS Webhook)
   ▼
[Cloudflare Workers]  (cloud/worker/src/index.js — 무료 플랜 일 10만 요청)
   ├─ 1) Slack HMAC-SHA256 서명 & 타임스탬프 검증 + 권한 체크
   ├─ 2) Slack 채널 스레드에 "접수됨" 상태 메시지 즉시 게시 (ts 확보)
   └─ 3) GitHub API repository_dispatch (event_type: slack_ai_command) 호출
           │
           ▼
[GitHub Actions Runner]  (.github/workflows/slack_ai_worker.yml)
   ├─ 대상 저장소 체크아웃 (Slack_AIbot, Instagram_TrailLog, Google_OndoTrip)
   ├─ cloud/runner/main.py (Gemini Function Calling 에이전트 실행)
   ├─ dev 모드 변경 발생 시 ai/slack-<job_id> 브랜치 푸시 & PR 자동 생성
   └─ Slack Web API로 기존 상태 메시지를 최종 보고서로 업데이트
```

---

## 5분 클라우드 배포 체크리스트

### 1단계: GitHub 저장소 시크릿 등록 (`kkubunge/Slack_AIbot`)
GitHub 저장소 **Settings → Secrets and variables → Actions → New repository secret**에 등록합니다.

| 시크릿 이름 | 설명 | 필수 여부 |
| :--- | :--- | :--- |
| `GEMINI_API_KEY` | Google AI Studio Gemini API 키 | **필수** (AI 개발/분석 시) |
| `SLACK_BOT_TOKEN` | Slack Bot User OAuth Token (`xoxb-...`) | **필수** |
| `GH_PAT` | GitHub Personal Access Token (`repo`, `workflow` 권한) — 다른 저장소(`Instagram_TrailLog`, `Google_OndoTrip` 등) 체크아웃·PR·워크플로우 실행 시 필요 | **권장** |
| `SLACK_REPORT_CHANNEL_ID` | Actions 수동 실행(`workflow_dispatch`) 시 보고받을 기본 채널 ID | 선택 |

> [!NOTE]
> 본 저장소(`Slack_AIbot`)만 제어할 때는 기본 `GITHUB_TOKEN`으로도 동작하지만, **다른 저장소(`Instagram_TrailLog`, `Google_OndoTrip`)에 브랜치를 푸시하거나 원격 워크플로우(`!실행 traillog`, `!실행 ondotrip`)를 트리거하려면 `GH_PAT` 등록이 필요합니다.**

### 2단계: Cloudflare Workers 배포
Cloudflare 무료 계정으로 1분 만에 게이트웨이를 배포합니다.

```powershell
cd D:\99_MyPJT\GoogleAI_PJT\Slack_AIbot\cloud\worker
npx wrangler login
npx wrangler deploy
```

배포 후 출력되는 Worker URL(예: `https://slack-aibot-gateway.<계정>.workers.dev`)을 확인하고, 시크릿을 등록합니다:

```powershell
npx wrangler secret put SLACK_SIGNING_SECRET   # Slack App Basic Information -> Signing Secret
npx wrangler secret put SLACK_BOT_TOKEN        # xoxb-...
npx wrangler secret put GITHUB_TOKEN           # repo + workflow 권한이 있는 GitHub PAT
npx wrangler secret put ALLOWED_USER_IDS       # 본인 Slack User ID (쉼표로 복수 지정)
```

### 3단계: Slack 클라우드 앱 생성 (또는 기존 앱에 Webhook 연결)
1. [Slack API Apps](https://api.slack.com/apps)에서 **Create New App → From a manifest**를 선택합니다.
2. [`cloud/slack_cloud_manifest.yaml`](slack_cloud_manifest.yaml) 내용을 붙여넣고, `<your-worker>.workers.dev` 부분을 2단계에서 발급받은 Worker 도메인으로 교체합니다.
   - Event Subscriptions URL: `https://<your-worker>.workers.dev/slack/events`
   - `/ai` Slash Command URL: `https://<your-worker>.workers.dev/slack/commands`
3. 워크스페이스에 앱을 설치(`Install to Workspace`)한 뒤 봇을 채널에 초대(`/invite @Slack_AIbot`)합니다.

---

## 클라우드 모드 지원 명령어

| 입력 방식 | 동작 | 예시 |
| :--- | :--- | :--- |
| `@Slack_AIbot !도움` | 클라우드 명령어 도움말 즉시 표시 | `!도움` |
| `@Slack_AIbot !프로젝트` | 등록된 클라우드 GitHub 저장소·워크플로우 목록 보고 | `!프로젝트` |
| `@Slack_AIbot !실행` | 원격 실행 가능한 GitHub Actions 목록 보고 | `!실행` |
| `@Slack_AIbot !실행 <이름>` | 대상 저장소 워크플로우 원격 실행 | `!실행 ondotrip-status`, `!실행 traillog-dryrun` |
| `@Slack_AIbot !예약` | 클라우드 정기 자동 실행 예약 현황 조회 | `!예약` |
| `@Slack_AIbot !상태` | 클라우드 게이트웨이 및 저장소별 최근 실행 상태 조회 | `!상태` |
| `@Slack_AIbot !작업` | 최근 GitHub Actions 실행 이력 및 로그 링크 조회 | `!작업` |
| `@Slack_AIbot !분석 [저장소] <지시>` | 읽기 전용(`plan`) 코드 분석 보고 (PR 생성 없음) | `!분석 [ondotrip] 블로그 글 생성 구조 설명해줘` |
| `@Slack_AIbot [저장소] <지시>` | 코드 수정·테스트 후 브랜치 & PR 자동 생성(`dev`) | `[traillog] 캡션 문구 오타 점검하고 고쳐줘` |
| `/ai <명령 또는 지시>` | 어느 채널에서든 슬래시 명령으로 즉시 실행 | `/ai !예약`, `/ai [slack_aibot] README 오타 확인해줘` |

---

## 등록 저장소 추가 방법

[`cloud/cloud_repos.yaml`](cloud_repos.yaml)의 `repos:` 또는 `workflows:` 섹션에 새 저장소를 추가해 `main` 브랜치에 푸시하면 즉시 Slack에서 `[새저장소]` 접두어 및 `!실행` 명령으로 사용할 수 있습니다.

---

## 무료 플랜 한도 참고

- **Cloudflare Workers 무료 플랜**: 일 100,000 요청 (Webhook 중계 용도로 사실상 무제한).
- **GitHub Actions**: 공개 저장소(`public`)는 무료 무제한; 비공개 저장소(`private`)는 월 2,000분 무료.
- **Gemini API**: Google AI Studio 무료 티어 또는 종량제 한도 적용.

---

## 클라우드 단위 테스트 실행

```powershell
cd D:\99_MyPJT\GoogleAI_PJT\Slack_AIbot

# Cloudflare Workers 테스트 (Node v24 내장 테스트 러너)
node --test cloud\worker\test\worker.test.mjs

# 클라우드 Python 에이전트·도구·GitHub 연동 테스트
.\.venv\Scripts\python.exe -m pytest tests\test_cloud_runner.py -v
```
