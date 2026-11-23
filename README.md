# Slack_AIbot — Slack 기반 AI 개발·명령·보고 컨트롤러

GitHub 저장소: `https://github.com/kkubunge/Slack_AIbot.git`

Windows 작업 스케줄러 대신 **Slack** 으로 원격 명령을 내리고, 실시간 진행 상황과 완료 결과를 보고받으며 AI 기반 코드 개발·분석·자동화 작업을 제어하는 통합 컨트롤러입니다.

- **로컬 PC 모드 (`src/`)**: Socket Mode로 로컬 PC의 `agy.EXE` 및 로컬 프로젝트(`.venv`) 제어
- **클라우드 무인 모드 (`cloud/`, `.github/workflows/`)**: **로컬 PC 전원 없이** `Slack ↔ Cloudflare Workers ↔ GitHub Actions + Gemini API`로 24시간 AI 코드 개발·PR 자동 생성·원격 워크플로우 실행 및 보고 ([클라우드 배포 가이드](cloud/README_CLOUD.md))

---

## 프로젝트 구조 (`Slack_AIbot/`)

```text
Slack_AIbot/
├── .github/workflows/
│   └── slack_ai_worker.yml   # GitHub Actions 클라우드 AI 워커
├── cloud/                    # 로컬 PC 없는 클라우드 무인 모드 (방법 A)
│   ├── README_CLOUD.md       # Cloudflare Workers + GitHub Actions 5분 배포 가이드
│   ├── cloud_repos.yaml      # 원격 제어 대상 GitHub 저장소 및 워크플로우 목록
│   ├── slack_cloud_manifest.yaml
│   ├── worker/               # Cloudflare Workers Webhook 수신 게이트웨이
│   └── runner/               # Gemini Function Calling 에이전트 및 PR·Slack 보고 러너
├── .env.example              # Slack 토큰 및 권한 설정 템플릿
├── .gitignore                # .env, .venv, data, logs 등 제외 규칙
├── config.yaml               # 프로젝트 목록, AI 모델/모드, 프리셋 작업 설정
├── slack_app_manifest.yaml   # Slack 앱 원클릭 생성용 매니페스트
├── requirements.txt          # Python 패키지 의존성 목록
├── README.md                 # 사용 및 설정 안내서
├── run_bot.bat               # Slack_AIbot 실행 스크립트
├── run_local_test.bat        # Slack 연결 없이 터미널에서 테스트 (--local-cli)
├── run_tests.bat             # pytest 전체 테스트 실행
├── register_autostart.bat    # Windows 로그인 시 자동 실행 등록 (선택)
├── unregister_autostart.bat  # Windows 로그인 자동 실행 해제
├── src/
│   ├── __init__.py
│   ├── main.py               # CLI 진입점 (봇 실행 / --check / --local-cli)
│   ├── config.py             # 설정 로드, 경로 탈출 검증, agy.exe 탐지
│   ├── state_store.py        # SQLite(data/state.db) 세션·설정·이력·예약 저장소
│   ├── agy_runner.py         # Antigravity CLI(stream-json) 스트리밍 실행기
│   ├── task_runner.py        # 프리셋 프로젝트 작업 실행 및 Windows 스케줄러 조회
│   ├── scheduler_service.py  # 내장 일일 예약 스케줄러 서비스
│   ├── formatter.py          # Markdown→Slack mrkdwn 변환, 장문 분할, 보고서 포맷
│   ├── command_router.py     # 권한 제어, 명령 라우팅, 프로젝트별 락 관리
│   └── slack_bot.py          # Slack Bolt Socket Mode 어댑터
└── tests/
    ├── __init__.py
    ├── test_agy_runner.py
    ├── test_command_router.py
    ├── test_config.py
    ├── test_formatter.py
│   ├── test_scheduler_service.py
│   ├── test_state_store.py
│   ├── test_task_runner.py
│   └── test_cloud_runner.py
```

---

## 두 가지 운영 모드 선택

| 구분 | 로컬 PC 모드 (`src/`) | 클라우드 무인 모드 (`cloud/`) |
| :--- | :--- | :--- |
| **PC 전원** | 로컬 PC 상시 켜짐 필요 | **PC 꺼져 있어도 24시간 동작** |
| **통신 방식** | Slack Socket Mode | Cloudflare Workers Webhook + GitHub Actions |
| **AI 엔진** | 로컬 `agy.EXE` (Antigravity CLI) | Gemini REST API Function Calling (`GEMINI_API_KEY`) |
| **적합한 작업** | 로컬 전용 파일, 브라우저 로그인 필요한 티스토리 자동화 등 | GitHub 저장소 코드 개발·자동 PR 생성, `TrailLog` 등 Actions 실행 |
| **상세 안내** | 아래 가이드 참고 | [`cloud/README_CLOUD.md`](cloud/README_CLOUD.md) 참고 |

---

## 주요 기능

1. **Slack 기반 AI 원격 개발 (`agy.exe` 연동)**
   - Slack 채널 멘션(`@Slack_AIbot`) 또는 DM에 자연어로 지시하면 선택한 프로젝트 폴더에서 AI가 코드를 분석·작성·수정·검증하고 결과를 보고합니다.
   - **실시간 진행 보고**: 도구 실행(`view_file`, `write_file`, `run_command` 등) 단계와 경과 시간을 Slack 메시지로 실시간 업데이트합니다.
   - **스레드 멀티턴 대화**: 작업 스레드에 답글을 달면 이전 `conversation_id`를 자동 복원해 이어서 지시할 수 있습니다.
   - **모드 분리**: `dev` 모드(코드 편집 및 명령 실행 허용)와 `plan` 모드(읽기 전용 분석/설계)를 지원합니다.
2. **기존 스케줄러 작업 온디맨드 실행 및 내장 예약 대체**
   - `!실행 ondotrip`, `!실행 ondotrip-status`, `!실행 traillog`, `!실행 traillog-check` 등으로 `Google_OndoTrip`과 `Instagram_TrailLog`를 언제든 Slack에서 실행하고 로그를 보고받습니다.
   - `!예약 추가 10:00 run ondotrip` 형태로 Windows 작업 스케줄러 대신 `Slack_AIbot` 내부에서 일일 예약을 관리할 수 있습니다.
3. **안전장치 및 권한 제어**
   - `.env`의 `ALLOWED_USER_IDS` 화이트리스트에 등록된 사용자만 명령을 실행할 수 있습니다.
   - 같은 프로젝트에 대한 동시 AI 작업은 프로젝트별 락으로 순차 보호됩니다.
   - `!중단 <작업ID|all>` 명령으로 실행 중인 AI/작업 프로세스 트리를 즉시 종료할 수 있습니다.

---

## Slack 앱 3분 설정 가이드

1. **Slack 앱 생성**
   - [Slack API Apps 페이지](https://api.slack.com/apps)에서 **Create New App** → **From a manifest** 선택.
   - 워크스페이스를 선택하고, 이 폴더의 [`slack_app_manifest.yaml`](slack_app_manifest.yaml) 내용을 그대로 붙여넣어 `Slack_AIbot` 앱을 생성합니다.
2. **App-Level Token 발급 (`SLACK_APP_TOKEN`)**
   - 앱 설정의 **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes**.
   - 이름(예: `socket-token`) 입력 후 `connections:write` 스코프 추가 → 발급된 `xapp-...` 토큰을 복사합니다.
3. **워크스페이스 설치 및 Bot Token 발급 (`SLACK_BOT_TOKEN`)**
   - 좌측 **Install App** → **Install to Workspace** 클릭 후 허용.
   - 발급된 **Bot User OAuth Token** (`xoxb-...`)을 복사합니다.
4. **`.env` 설정**
   - `.env.example`을 복사하여 `.env` 파일을 만들고 토큰을 입력합니다.
   - 본인의 Slack User ID를 확인해 `ALLOWED_USER_IDS`에 입력합니다. (모르면 비워둔 채 봇에 메시지를 보내면 봇이 본인의 User ID를 안내해 줍니다.)
   - 봇을 사용할 채널에 `/invite @Slack_AIbot`으로 초대합니다.

---

## 실행 방법

### 1. 환경 점검 및 로컬 테스트 (Slack 토큰 없이 가능)
```powershell
# 설정, agy.exe 탐지, 프로젝트 경로, DB 및 기존 Windows 스케줄러 점검
.\.venv\Scripts\python.exe -m src.main --check

# 터미널에서 Slack_AIbot 명령과 AI 개발을 직접 테스트
run_local_test.bat
```

### 2. Slack_AIbot 실행
```powershell
run_bot.bat
```
- PC 로그인 시 자동으로 봇을 백그라운드 실행 등록하려면 `register_autostart.bat`을 실행하세요. (해제는 `unregister_autostart.bat`)

---

## Slack 명령어 가이드

| 명령어 | 별칭 | 설명 | 예시 |
| :--- | :--- | :--- | :--- |
| `(일반 메시지)` | - | 현재 선택된 프로젝트에서 AI 개발(`dev`) 실행 | `README에 실행 예시 추가해줘` |
| `[프로젝트] 내용` | - | 이번 요청만 다른 프로젝트에서 AI 개발 실행 | `[ondotrip] 로그 포맷 개선해줘` |
| `!개발 <지시>` | `!dev` | AI 개발(`dev` 모드) 명시적 실행 | `!dev 단위 테스트 보강해줘` |
| `!분석 <지시>` | `!plan` | 코드 수정 없이 읽기 전용(`plan` 모드) 분석 | `!plan main.py 병목 지점 찾아줘` |
| `!프로젝트` | `!projects` | 등록된 프로젝트 목록과 현재 기본 프로젝트 확인 | `!프로젝트` |
| `!선택 <키>` | `!use` | 현재 채널의 기본 프로젝트 변경 | `!선택 ondotrip` |
| `!새프로젝트 <이름> [설명]` | `!new` | 작업 루트에 새 폴더 생성 및 프로젝트 등록 | `!새프로젝트 WeatherBot 날씨 봇` |
| `!모델 [모델명]` | `!model` | 사용 가능 모델 목록 조회 또는 변경 | `!모델 gemini-3.8-flash-high` |
| `!모드 [dev\|plan]` | `!mode` | 채널 기본 AI 모드 조회 또는 변경 | `!모드 plan` |
| `!실행 [작업명] [인자]` | `!run` | 등록된 프리셋 목록 조회 또는 작업 실행 | `!실행 ondotrip-status` |
| `!예약 목록` | `!schedule list` | 내장 일일 예약 목록 조회 | `!예약 목록` |
| `!예약 추가 <HH:MM> <run\|ai> <대상>` | `!schedule add` | 매일 지정 시각 자동 실행 예약 | `!예약 추가 10:00 run ondotrip` |
| `!예약 삭제\|켜기\|끄기 <ID>` | `!schedule del\|enable\|disable` | 예약 삭제 및 활성화 제어 | `!예약 끄기 1` |
| `!스케줄러` | `!winscheduler` | 기존 Windows 작업 스케줄러 현황 조회 | `!스케줄러` |
| `!상태` | `!status` | 봇 가동 시간, 설정, `agy` 및 실행 작업 조회 | `!상태` |
| `!작업` | `!jobs` | 실행 중 및 최근 완료 작업 10건 조회 | `!작업` |
| `!중단 <작업ID\|all>` | `!stop` | 실행 중인 AI/프리셋 작업 즉시 중단 | `!중단 all` |
| `!로그 <작업ID>` | `!log` | 특정 작업의 마지막 로그 조회 | `!로그 a1b2c3d4` |
| `!도움` | `!help` | 전체 명령어 도움말 표시 | `!도움` |

---

## 기존 Windows 작업 스케줄러에서 Slack / 클라우드 방식으로 전환하기

`Slack_AIbot`은 안전을 위해 기존 Windows 작업 스케줄러(`OndoTrip_Daily_Auto` 등)를 자동으로 삭제하거나 비활성화하지 않습니다.

1. 먼저 Slack에서 `!스케줄러` 명령으로 현재 등록된 Windows 작업을 확인합니다.
2. 클라우드 스케줄러(Cloudflare Workers Cron, 매일 오전 10:00 `Google_OndoTrip` 실행) 또는 `Slack_AIbot` 내장 예약(`!예약 추가 10:00 run ondotrip`)으로 전환합니다.
3. 이중 실행을 막으려면 아래 명령으로 기존 Windows 작업을 비활성화하세요(미사용 작업 포함).
   ```powershell
   schtasks /Change /TN "OndoTrip_Daily_Auto" /DISABLE
   schtasks /Change /TN "NaverMap_MealAndBrew_Daily_0930" /DISABLE
   ```

---

## 테스트 실행

```powershell
run_tests.bat
# 또는 전체 Python 테스트 + Cloudflare Workers 테스트
.\.venv\Scripts\python.exe -m pytest -v
node --test cloud\worker\test\worker.test.mjs
```
