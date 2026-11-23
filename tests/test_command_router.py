"""command_router 모듈 단위 및 통합 테스트."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from src.agy_runner import AgyResult, ProgressEvent
from src.command_router import CommandRouter, IncomingMessage, ReplySink
from src.config import AIConfig, AppConfig, ProjectConfig, ReportingConfig, SlackConfig, TaskConfig
from src.state_store import StateStore
from src.task_runner import TaskResult


class RecordingSink(ReplySink):
    """테스트용 메시지 기록 싱크."""

    def __init__(self) -> None:
        self.immediate: list[str] = []
        self.progress: list[str] = []
        self.finished: list[tuple[list[str], bool, bool]] = []
        self._lock = threading.Lock()

    def send_immediate(self, text: str) -> None:
        with self._lock:
            self.immediate.append(text)

    def start_progress(self, initial_text: str) -> str:
        with self._lock:
            self.progress.append(initial_text)
        return "h1"

    def update_progress(self, handle: str, text: str) -> None:
        with self._lock:
            self.progress.append(text)

    def finish(
        self,
        handle: str,
        text_chunks: list[str],
        success: bool,
        cancelled: bool = False,
        log_path: str = "",
        upload_log: bool = False,
    ) -> None:
        with self._lock:
            self.finished.append((text_chunks, success, cancelled))


class FakeAgyRunner:
    """실제 CLI 대신 호출 인자를 기록하고 모의 결과를 반환하는 페이크 러너."""

    def __init__(self, delay_sec: float = 0.0) -> None:
        self.calls: list[dict] = []
        self.delay_sec = delay_sec
        self.cancelled_jobs: set[str] = set()

    def cancel(self, job_id: str) -> bool:
        self.cancelled_jobs.add(job_id)
        return True

    def run(
        self,
        job_id: str,
        prompt: str,
        cwd: Path,
        model: str,
        mode: str = "dev",
        conversation_id: str | None = None,
        timeout_sec: int | None = None,
        on_progress=None,
        cancel_event: threading.Event | None = None,
    ) -> AgyResult:
        self.calls.append(
            {
                "job_id": job_id,
                "prompt": prompt,
                "cwd": cwd,
                "model": model,
                "mode": mode,
                "conversation_id": conversation_id,
            }
        )
        if on_progress:
            on_progress(ProgressEvent(kind="tool_start", message="도구 실행: `view_file`"))
        if self.delay_sec > 0:
            deadline = time.monotonic() + self.delay_sec
            while time.monotonic() < deadline:
                if (cancel_event and cancel_event.is_set()) or job_id in self.cancelled_jobs:
                    return AgyResult(
                        success=False,
                        status="CANCELLED",
                        response="",
                        error="중단됨",
                        cancelled=True,
                    )
                time.sleep(0.02)
        return AgyResult(
            success=True,
            status="SUCCESS",
            response=f"처리 완료 ({cwd.name})",
            conversation_id=conversation_id or "conv-first-001",
            duration_sec=0.1,
            usage={"total_tokens": 100},
        )


class FakeTaskRunner:
    """프리셋 작업 페이크 러너."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Path, str]] = []

    def cancel(self, job_id: str) -> bool:
        return True

    def run(
        self,
        job_id: str,
        task: TaskConfig,
        cwd: Path,
        extra_args: str = "",
        timeout_sec: int | None = None,
        on_progress=None,
        cancel_event=None,
    ) -> TaskResult:
        self.calls.append((task.key, cwd, extra_args))
        return TaskResult(
            success=True,
            return_code=0,
            stdout_tail=["현재 상태: 정상"],
            duration_sec=0.05,
        )


def _build_env(tmp_path: Path, allowed_users: list[str] | None = None, allow_all: bool = False):
    bot_dir = tmp_path / "Slack_AIbot"
    ondo_dir = tmp_path / "Google_OndoTrip"
    bot_dir.mkdir(parents=True)
    ondo_dir.mkdir(parents=True)
    (bot_dir / "config.yaml").write_text(
        "workspace_root: .\nprojects:\n  slack_aibot:\n    path: Slack_AIbot\n",
        encoding="utf-8",
    )

    cfg = AppConfig(
        base_dir=bot_dir,
        config_path=bot_dir / "config.yaml",
        workspace_root=tmp_path,
        default_project="slack_aibot",
        agy_cli_path="agy.exe",
        task_timeout_sec=30,
        ai=AIConfig(),
        reporting=ReportingConfig(update_interval_sec=0.0),
        slack=SlackConfig(
            allowed_user_ids=allowed_users if allowed_users is not None else ["U_ALLOWED"],
            allow_all_users=allow_all,
        ),
        projects={
            "slack_aibot": ProjectConfig("slack_aibot", bot_dir, "Slack_AIbot", "Slack_AIbot 컨트롤러"),
            "ondotrip": ProjectConfig("ondotrip", ondo_dir, "Google_OndoTrip", "블로그 봇"),
        },
        tasks={
            "ondotrip-status": TaskConfig(
                "ondotrip-status", "ondotrip", [".venv\\Scripts\\python.exe", "main.py", "--status"], "현황 조회"
            )
        },
    )
    store = StateStore(bot_dir / "data" / "state.db")
    return cfg, store


def test_authorization_blocks_unconfigured_and_unauthorized_user(tmp_path: Path) -> None:
    cfg, store = _build_env(tmp_path, allowed_users=[], allow_all=False)
    try:
        router = CommandRouter(cfg, store, FakeAgyRunner(), FakeTaskRunner(), background=False)
        sink = RecordingSink()
        router.handle_message(IncomingMessage("!상태", "U_ME_123", "C1", "100", "100"), sink)
        assert "ALLOWED_USER_IDS" in sink.immediate[0]
        assert "U_ME_123" in sink.immediate[0]

        cfg.slack.allowed_user_ids = ["U_ADMIN"]
        sink2 = RecordingSink()
        router.handle_message(IncomingMessage("!상태", "U_INTRUDER", "C1", "100", "100"), sink2)
        assert "허용되지 않은 사용자" in sink2.immediate[0]
    finally:
        store.close()


def test_commands_project_switch_schedule_and_thread_continuity(tmp_path: Path) -> None:
    cfg, store = _build_env(tmp_path)
    fake_agy = FakeAgyRunner()
    fake_task = FakeTaskRunner()
    try:
        router = CommandRouter(cfg, store, fake_agy, fake_task, background=False)
        sink = RecordingSink()

        # 1. 기본 프로젝트 전환 & 모델/모드 전환
        router.handle_message(IncomingMessage("!선택 ondotrip", "U_ALLOWED", "C1", "1", "1"), sink)
        assert "ondotrip" in sink.immediate[-1]

        router.handle_message(IncomingMessage("!모델 gemini-3.8-flash-high", "U_ALLOWED", "C1", "2", "2"), sink)
        router.handle_message(IncomingMessage("!모드 plan", "U_ALLOWED", "C1", "3", "3"), sink)

        # 2. 새 프로젝트 생성
        router.handle_message(IncomingMessage("!새프로젝트 DemoApp 데모 앱", "U_ALLOWED", "C1", "4", "4"), sink)
        assert "demoapp" in cfg.projects
        assert (tmp_path / "DemoApp").is_dir()

        # 3. 프리셋 작업 실행
        router.handle_message(IncomingMessage("!실행 ondotrip-status --verbose", "U_ALLOWED", "C1", "5", "5"), sink)
        assert fake_task.calls[0][0] == "ondotrip-status"
        assert fake_task.calls[0][2] == "--verbose"
        assert sink.finished[-1][1] is True

        # 4. [프로젝트] 접두어 오버라이드 + 스레드 멀티턴 이어쓰기
        router.handle_message(
            IncomingMessage("<@UBOT> [ondotrip] 구조 요약해줘", "U_ALLOWED", "C1", "t-100", "t-100", is_thread_reply=False),
            sink,
        )
        assert fake_agy.calls[-1]["cwd"] == cfg.projects["ondotrip"].path
        assert fake_agy.calls[-1]["conversation_id"] is None

        # 같은 스레드에 답글 -> 기존 conversation_id("conv-first-001") 자동 전달
        router.handle_message(
            IncomingMessage("테스트 코드도 확인해줘", "U_ALLOWED", "C1", "t-100", "t-101", is_thread_reply=True),
            sink,
        )
        assert fake_agy.calls[-1]["conversation_id"] == "conv-first-001"
        assert fake_agy.calls[-1]["cwd"] == cfg.projects["ondotrip"].path

        # 5. 예약 등록/제어
        router.handle_message(IncomingMessage("!예약 추가 09:30 run ondotrip-status", "U_ALLOWED", "C1", "6", "6"), sink)
        router.handle_message(IncomingMessage("!예약 목록", "U_ALLOWED", "C1", "7", "7"), sink)
        assert "09:30" in sink.immediate[-1]
        router.handle_message(IncomingMessage("!예약 끄기 1", "U_ALLOWED", "C1", "8", "8"), sink)
        router.handle_message(IncomingMessage("!예약 켜기 1", "U_ALLOWED", "C1", "9", "9"), sink)
        router.handle_message(IncomingMessage("!예약 삭제 1", "U_ALLOWED", "C1", "10", "10"), sink)
        assert store.list_schedules() == []
    finally:
        store.close()


def test_stop_running_background_job(tmp_path: Path) -> None:
    cfg, store = _build_env(tmp_path)
    fake_agy = FakeAgyRunner(delay_sec=3.0)
    try:
        router = CommandRouter(cfg, store, fake_agy, FakeTaskRunner(), background=True)
        sink = RecordingSink()
        router.handle_message(IncomingMessage("긴 작업 수행", "U_ALLOWED", "C1", "200", "200"), sink)

        # 워커 시작 대기
        deadline = time.monotonic() + 2.0
        while not router.list_active_jobs() and time.monotonic() < deadline:
            time.sleep(0.02)
        active = router.list_active_jobs()
        assert len(active) == 1

        router.handle_message(IncomingMessage(f"!중단 {active[0].job_id}", "U_ALLOWED", "C1", "201", "201"), sink)
        router.wait_all_jobs(timeout_sec=3.0)
        assert sink.finished[-1][1] is False
        assert sink.finished[-1][2] is True
    finally:
        store.close()
