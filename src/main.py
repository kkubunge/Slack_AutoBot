"""Slack AI 개발·명령·보고 컨트롤러 CLI 진입점 (--check / --local-cli / 봇 실행)."""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from pathlib import Path

from src.command_router import CommandRouter, IncomingMessage, ReplySink
from src.config import AppConfig, load_config
from src.state_store import StateStore


class LocalConsoleReplySink(ReplySink):
    """Slack 토큰 없이 로컬 터미널에서 동작을 검증할 수 있는 콘솔 출력 싱크."""

    def send_immediate(self, text: str) -> None:
        print("\n" + text + "\n")

    def start_progress(self, initial_text: str) -> str:
        handle = uuid.uuid4().hex[:6]
        print(f"\n[진행 시작:{handle}]\n{initial_text}\n")
        return handle

    def update_progress(self, handle: str, text: str) -> None:
        print(f"[진행 갱신:{handle}]\n{text}\n")

    def finish(
        self,
        handle: str,
        text_chunks: list[str],
        success: bool,
        cancelled: bool = False,
        log_path: str = "",
        upload_log: bool = False,
    ) -> None:
        status_label = "중단됨" if cancelled else ("성공" if success else "실패")
        print(f"\n=== [최종 보고:{handle} | {status_label}] ===")
        for idx, chunk in enumerate(text_chunks, start=1):
            if len(text_chunks) > 1:
                print(f"--- (분할 {idx}/{len(text_chunks)}) ---")
            print(chunk)
        print("==========================================\n")


def _configure_logging(base_dir: Path) -> None:
    """콘솔 및 logs/bot.log 파일 로깅을 초기화한다."""
    logs_dir = base_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "bot.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


def _mask_token(token: str) -> str:
    """토큰을 마스킹해 출력한다."""
    if not token or token.startswith("xoxb-your-") or token.startswith("xapp-your-"):
        return "미설정 (또는 예시 값)"
    if len(token) <= 10:
        return "설정됨"
    return f"설정됨 ({token[:8]}...{token[-4:]})"


def run_check(config: AppConfig) -> int:
    """로컬 환경, 설정 파일, agy CLI, 프로젝트 경로 및 DB 상태를 점검한다."""
    print("============================================================")
    print("  Slack_AIbot 환경 점검")
    print("============================================================")
    print(f"• 기준 폴더       : {config.base_dir}")
    print(f"• 설정 파일       : {config.config_path} ({'존재' if config.config_path.is_file() else '없음'})")
    print(f"• .env 파일       : {config.base_dir / '.env'} ({'존재' if (config.base_dir / '.env').is_file() else '없음'})")
    print(f"• 작업 루트       : {config.workspace_root}")
    print(f"• SLACK_BOT_TOKEN : {_mask_token(config.slack.bot_token)}")
    print(f"• SLACK_APP_TOKEN : {_mask_token(config.slack.app_token)}")
    print(
        f"• 허용 사용자     : "
        + (
            "전체 허용(ALLOW_ALL_USERS=true)"
            if config.slack.allow_all_users
            else (", ".join(config.slack.allowed_user_ids) or "미설정 (명령 차단 상태)")
        )
    )
    print(f"• Antigravity CLI : {config.agy_cli_path or '미감지'}")
    print(f"• 기본 AI 모델/모드: {config.ai.default_model} / {config.ai.default_mode}")

    print("\n[등록된 프로젝트 경로 점검]")
    for key, proj in config.projects.items():
        status = "존재" if proj.path.exists() else "경로 없음"
        print(f"  - {key:<15} : {proj.path} [{status}]")

    print(f"\n[등록된 프리셋 작업: {len(config.tasks)}개]")
    for key, task in config.tasks.items():
        print(f"  - {key:<15} : ({task.project}) {task.description}")

    db_path = config.base_dir / "data" / "state.db"
    store = StateStore(db_path)
    schedules = store.list_schedules()
    store.close()
    print(f"\n[SQLite 상태 저장소]: 정상 초기화 ({db_path}, 등록 예약 {len(schedules)}건)")
    print("============================================================")
    return 0


def run_local_cli(config: AppConfig) -> int:
    """Slack 토큰 없이 터미널에서 명령과 AI 개발을 테스트하는 대화형 콘솔."""
    # 로컬 테스트 시에는 .env의 허용 목록이 비어 있어도 바로 테스트할 수 있도록 임시 허용
    if not config.slack.allowed_user_ids:
        config.slack.allow_all_users = True

    store = StateStore(config.base_dir / "data" / "state.db")
    router = CommandRouter(config=config, store=store, background=False)
    sink = LocalConsoleReplySink()

    print("============================================================")
    print("  Slack_AIbot 로컬 테스트 콘솔 (--local-cli)")
    print("  • Slack에 입력할 명령(!도움, !프로젝트, !실행 등) 또는 AI 지시를 입력하세요.")
    print("  • 스레드 이어쓰기 테스트: '/newthread'로 새 스레드 시작")
    print("  • 종료: 'exit' 또는 'quit'")
    print("============================================================")

    channel_id = "LOCAL_CLI"
    user_id = "LOCAL_USER"
    current_thread_ts = f"local-{uuid.uuid4().hex[:8]}"
    has_started_thread = False

    try:
        while True:
            try:
                raw = input(f"\n[{current_thread_ts}] > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n종료합니다.")
                break

            if not raw:
                continue
            if raw.lower() in {"exit", "quit"}:
                print("종료합니다.")
                break
            if raw.lower() == "/newthread":
                current_thread_ts = f"local-{uuid.uuid4().hex[:8]}"
                has_started_thread = False
                print(f"새 스레드로 전환했습니다: {current_thread_ts}")
                continue

            msg = IncomingMessage(
                text=raw,
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=current_thread_ts,
                message_ts=f"msg-{uuid.uuid4().hex[:6]}",
                is_thread_reply=has_started_thread,
                is_dm=True,
            )
            router.handle_message(msg, sink)
            if not raw.startswith("!"):
                has_started_thread = True
    finally:
        store.close()
    return 0


def _has_valid_slack_tokens(config: AppConfig) -> bool:
    """실제 실행 가능한 Slack Bot/App 토큰이 설정되었는지 검사한다."""
    bot = config.slack.bot_token
    app = config.slack.app_token
    if not bot or not app:
        return False
    if bot.startswith("xoxb-your-") or app.startswith("xapp-your-"):
        return False
    return bot.startswith("xoxb-") and app.startswith("xapp-")


def main(argv: list[str] | None = None) -> int:
    """프로그램 메인 진입점."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Slack 기반 AI 개발·명령·보고 컨트롤러",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="설정, agy CLI, 프로젝트 경로 및 DB 상태를 점검합니다.",
    )
    parser.add_argument(
        "--local-cli",
        action="store_true",
        help="Slack 연결 없이 터미널에서 봇 명령과 AI 개발을 테스트합니다.",
    )
    args = parser.parse_args(argv)

    config = load_config()
    _configure_logging(config.base_dir)

    if args.check:
        return run_check(config)
    if args.local_cli:
        return run_local_cli(config)

    if not _has_valid_slack_tokens(config):
        print("============================================================")
        print("  Slack 토큰(.env) 설정이 필요합니다.")
        print("============================================================")
        print(f"1. '{config.base_dir / '.env.example'}' 파일을 복사해 '.env' 파일을 만드세요.")
        print("2. Slack 앱(slack_app_manifest.yaml)에서 발급한 아래 토큰을 입력하세요:")
        print("   - SLACK_BOT_TOKEN=xoxb-...")
        print("   - SLACK_APP_TOKEN=xapp-...")
        print("   - ALLOWED_USER_IDS=<본인 Slack User ID>")
        print("3. 토큰 발급 전 로컬 동작만 먼저 확인하려면 아래 명령을 실행하세요:")
        print("   .venv\\Scripts\\python.exe -m src.main --check")
        print("   .venv\\Scripts\\python.exe -m src.main --local-cli")
        print("============================================================")
        return 1

    from src.slack_bot import SlackControllerBot

    bot = SlackControllerBot(config)
    bot.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
