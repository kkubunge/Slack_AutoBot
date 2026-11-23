"""Slack 명령어 라우팅, 권한 제어, AI/프리셋 작업 오케스트레이션 모듈."""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from src.agy_runner import AgyResult, AgyRunner, ProgressEvent
from src.config import (
    VALID_AI_MODES,
    AppConfig,
    ProjectConfig,
    register_dynamic_project,
)
from src.formatter import (
    format_agy_report,
    format_help_message,
    format_progress_message,
    format_task_report,
    split_slack_message,
)
from src.state_store import ScheduleRecord, StateStore
from src.task_runner import TaskResult, TaskRunner, query_windows_schedulers

logger = logging.getLogger(__name__)

_PROJECT_PREFIX_RE = re.compile(r"^\[([a-zA-Z0-9_-]+)\]\s*(.+)$", re.DOTALL)
_TIME_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_MENTION_PREFIX_RE = re.compile(r"^(?:<@[A-Z0-9]+>\s*)+")


class ReplySink(ABC):
    """Slack 또는 로컬 콘솔로 응답과 진행 상황을 전달하는 추상 인터페이스."""

    @abstractmethod
    def send_immediate(self, text: str) -> None:
        """단발성 안내 또는 오류 메시지를 즉시 전송한다."""

    @abstractmethod
    def start_progress(self, initial_text: str) -> str:
        """진행 상황 표시용 초기 메시지를 생성하고 핸들을 반환한다."""

    @abstractmethod
    def update_progress(self, handle: str, text: str) -> None:
        """진행 중인 상태 메시지를 갱신한다."""

    @abstractmethod
    def finish(
        self,
        handle: str,
        text_chunks: list[str],
        success: bool,
        cancelled: bool = False,
        log_path: str = "",
        upload_log: bool = False,
    ) -> None:
        """최종 보고 메시지를 전송하고 완료 상태(리액션/로그 첨부 등)를 반영한다."""


@dataclass(slots=True)
class IncomingMessage:
    """수신된 사용자 명령/대화 메시지 정보."""

    text: str
    user_id: str
    channel_id: str
    thread_ts: str
    message_ts: str
    is_thread_reply: bool = False
    is_dm: bool = False


@dataclass(slots=True)
class ActiveJob:
    """현재 실행 중인 작업의 런타임 추적 정보."""

    job_id: str
    kind: str  # "ai" | "task"
    project_key: str
    target: str
    user_id: str
    channel_id: str
    thread_ts: str
    started_at: float
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


def strip_leading_mentions(text: str) -> str:
    """메시지 앞부분의 Slack 봇 멘션(`<@U...>`)을 제거한다."""
    return _MENTION_PREFIX_RE.sub("", text or "").strip()


class CommandRouter:
    """Slack 및 로컬 콘솔 입력을 해석해 명령/AI 개발/작업을 수행하는 라우터."""

    def __init__(
        self,
        config: AppConfig,
        store: StateStore,
        agy_runner: AgyRunner | None = None,
        task_runner: TaskRunner | None = None,
        background: bool = True,
    ) -> None:
        self.config = config
        self.store = store
        self.agy_runner = agy_runner or AgyRunner(config)
        self.task_runner = task_runner or TaskRunner(config)
        self.background = background
        self.started_monotonic = time.monotonic()

        self._jobs_lock = threading.RLock()
        self._active_jobs: dict[str, ActiveJob] = {}
        self._project_locks: dict[str, threading.Lock] = {}

        # DB에 저장된 동적 프로젝트를 설정 객체에 복원
        self._restore_dynamic_projects()

    def _restore_dynamic_projects(self) -> None:
        """이전에 동적으로 추가된 프로젝트가 설정에 빠져 있으면 복원한다."""
        for dyn in self.store.list_dynamic_projects():
            if dyn.key not in self.config.projects:
                p = Path(dyn.path)
                try:
                    rel = str(p.relative_to(self.config.workspace_root))
                except ValueError:
                    rel = p.name
                self.config.projects[dyn.key] = ProjectConfig(
                    key=dyn.key,
                    path=p,
                    relative_path=rel,
                    description=dyn.description,
                )

    def _get_project_lock(self, project_key: str) -> threading.Lock:
        """동일 프로젝트에 대한 동시 파일 편집 충돌을 막기 위한 프로젝트 락을 반환한다."""
        with self._jobs_lock:
            if project_key not in self._project_locks:
                self._project_locks[project_key] = threading.Lock()
            return self._project_locks[project_key]

    def get_channel_context(self, channel_id: str) -> tuple[str, str, str]:
        """채널의 현재 (프로젝트, 모델, 모드)를 반환한다."""
        setting = self.store.get_channel_setting(channel_id)
        if setting:
            proj = setting.project_key if setting.project_key in self.config.projects else self.config.default_project
            model = setting.model or self.config.ai.default_model
            mode = setting.mode if setting.mode in VALID_AI_MODES else self.config.ai.default_mode
            return proj, model, mode
        return (
            self.config.default_project,
            self.config.ai.default_model,
            self.config.ai.default_mode,
        )

    def _save_channel_context(self, channel_id: str, project_key: str, model: str, mode: str) -> None:
        """채널 설정을 저장소에 기록한다."""
        self.store.set_channel_setting(channel_id, project_key, model, mode)

    def _check_authorization(self, msg: IncomingMessage) -> tuple[bool, str]:
        """허용된 사용자 및 채널인지 확인한다."""
        slack_cfg = self.config.slack
        if not slack_cfg.allow_all_users:
            if not slack_cfg.allowed_user_ids:
                return (
                    False,
                    ":lock: *접근 권한 설정이 필요합니다.*\n"
                    f"• 현재 `.env`의 `ALLOWED_USER_IDS`가 비어 있어 보안상 명령 실행이 차단되었습니다.\n"
                    f"• 본인의 Slack User ID(`{msg.user_id}`)를 `.env`에 `ALLOWED_USER_IDS={msg.user_id}`로 추가한 뒤 봇을 재시작해 주세요.",
                )
            if msg.user_id not in slack_cfg.allowed_user_ids:
                return (
                    False,
                    f":no_entry: 허용되지 않은 사용자입니다 (`{msg.user_id}`).",
                )

        if slack_cfg.allowed_channel_ids and msg.channel_id not in slack_cfg.allowed_channel_ids:
            return (
                False,
                f":no_entry: 이 채널(`{msg.channel_id}`)에서는 봇 사용이 허용되지 않았습니다.",
            )

        return True, ""

    def list_active_jobs(self) -> list[ActiveJob]:
        """현재 실행 중인 작업 목록을 반환한다."""
        with self._jobs_lock:
            return list(self._active_jobs.values())

    def wait_all_jobs(self, timeout_sec: float = 10.0) -> None:
        """실행 중인 백그라운드 작업들이 끝날 때까지 대기한다 (주로 테스트용)."""
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            threads = [j.thread for j in self.list_active_jobs() if j.thread and j.thread.is_alive()]
            if not threads:
                return
            for th in threads:
                remaining = max(0.05, deadline - time.monotonic())
                th.join(timeout=remaining)

    def handle_message(self, msg: IncomingMessage, sink: ReplySink) -> None:
        """수신된 메시지를 권한 검증 후 명령어 또는 AI 개발 요청으로 라우팅한다."""
        allowed, reason = self._check_authorization(msg)
        if not allowed:
            sink.send_immediate(reason)
            return

        cleaned = strip_leading_mentions(msg.text)
        if not cleaned:
            sink.send_immediate(format_help_message())
            return

        if cleaned.startswith("!"):
            self._handle_bang_command(cleaned, msg, sink)
            return

        # 일반 자연어 메시지 -> AI 개발 요청
        self._dispatch_ai_prompt(cleaned, msg, sink, mode_override=None)

    def _handle_bang_command(self, text: str, msg: IncomingMessage, sink: ReplySink) -> None:
        """`!`로 시작하는 제어 명령어를 분기 처리한다."""
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if cmd in {"!도움", "!help"}:
            sink.send_immediate(format_help_message())
            return

        if cmd in {"!상태", "!status"}:
            sink.send_immediate(self._build_status_text(msg.channel_id))
            return

        if cmd in {"!프로젝트", "!projects"}:
            sink.send_immediate(self._build_projects_text(msg.channel_id))
            return

        if cmd in {"!선택", "!use"}:
            self._cmd_select_project(rest, msg, sink)
            return

        if cmd in {"!새프로젝트", "!new"}:
            self._cmd_new_project(rest, msg, sink)
            return

        if cmd in {"!모델", "!model"}:
            self._cmd_model(rest, msg, sink)
            return

        if cmd in {"!모드", "!mode"}:
            self._cmd_mode(rest, msg, sink)
            return

        if cmd in {"!개발", "!dev"}:
            if not rest:
                sink.send_immediate("사용법: `!개발 <개발 지시 내용>`")
                return
            self._dispatch_ai_prompt(rest, msg, sink, mode_override="dev")
            return

        if cmd in {"!분석", "!plan"}:
            if not rest:
                sink.send_immediate("사용법: `!분석 <분석 또는 계획 요청 내용>`")
                return
            self._dispatch_ai_prompt(rest, msg, sink, mode_override="plan")
            return

        if cmd in {"!실행", "!run"}:
            self._cmd_run_task(rest, msg, sink)
            return

        if cmd in {"!작업", "!jobs"}:
            sink.send_immediate(self._build_jobs_text())
            return

        if cmd in {"!중단", "!stop"}:
            self._cmd_stop_job(rest, sink)
            return

        if cmd in {"!로그", "!log"}:
            self._cmd_show_log(rest, sink)
            return

        if cmd in {"!예약", "!schedule"}:
            self._cmd_schedule(rest, msg, sink)
            return

        if cmd in {"!스케줄러", "!winscheduler"}:
            sink.send_immediate(query_windows_schedulers())
            return

        sink.send_immediate(
            f":question: 알 수 없는 명령어입니다: `{cmd}`\n전체 명령어 목록은 `!도움`으로 확인하세요."
        )

    # ------------------------------------------------------------------
    # 상태 / 설정 제어 명령어
    # ------------------------------------------------------------------
    def _build_status_text(self, channel_id: str) -> str:
        """봇 상태 요약 텍스트를 생성한다."""
        proj, model, mode = self.get_channel_context(channel_id)
        uptime_sec = int(time.monotonic() - self.started_monotonic)
        hours, rem = divmod(uptime_sec, 3600)
        mins, secs = divmod(rem, 60)
        active = self.list_active_jobs()
        schedules = self.store.list_schedules(only_enabled=True)
        agy_status = f"감지됨 (`{self.config.agy_cli_path}`)" if self.config.agy_cli_path else ":warning: 미감지"

        lines = [
            "*Slack_AIbot 상태*",
            f"• 가동 시간: `{hours}시간 {mins}분 {secs}초`",
            f"• 현재 채널 기본 프로젝트: `{proj}` (`{self.config.projects[proj].path}`)" if proj in self.config.projects else f"• 현재 프로젝트: `{proj}`",
            f"• 현재 AI 모델 / 모드: `{model}` / `{mode}`",
            f"• Antigravity CLI: {agy_status}",
            f"• 실행 중 작업: `{len(active)}건` | 활성 내장 예약: `{len(schedules)}건`",
        ]
        if active:
            for job in active:
                elapsed = time.monotonic() - job.started_at
                lines.append(f"  - `{job.job_id}` [{job.kind}] `{job.project_key}` ({elapsed:.0f}초 경과)")
        return "\n".join(lines)

    def _build_projects_text(self, channel_id: str) -> str:
        """등록된 프로젝트 목록을 생성한다."""
        current_proj, _, _ = self.get_channel_context(channel_id)
        lines = ["*등록된 프로젝트 목록*"]
        for key, proj in self.config.projects.items():
            mark = " :point_left: *현재 선택*" if key == current_proj else ""
            exists_badge = "준비됨" if proj.path.exists() else "폴더 없음"
            lines.append(f"• `{key}` (`{proj.relative_path}`, {exists_badge}) — {proj.description}{mark}")
        lines.append("\n_변경: `!선택 <프로젝트키>` | 임시 지정: `[프로젝트키] 지시내용` | 생성: `!새프로젝트 <이름> [설명]`_")
        return "\n".join(lines)

    def _cmd_select_project(self, arg: str, msg: IncomingMessage, sink: ReplySink) -> None:
        """현재 채널의 기본 프로젝트를 변경한다."""
        key = arg.strip().lower()
        if not key:
            sink.send_immediate("사용법: `!선택 <프로젝트키>`\n목록 조회: `!프로젝트`")
            return
        if key not in self.config.projects:
            sink.send_immediate(
                f":warning: 등록되지 않은 프로젝트입니다: `{key}`\n`!프로젝트`로 목록을 확인하거나 `!새프로젝트 {key}`로 새로 만드세요."
            )
            return

        _, model, mode = self.get_channel_context(msg.channel_id)
        self._save_channel_context(msg.channel_id, key, model, mode)
        proj = self.config.projects[key]
        sink.send_immediate(
            f":white_check_mark: 기본 프로젝트를 `{key}` (`{proj.path}`)로 변경했습니다.\n_{proj.description}_"
        )

    def _cmd_new_project(self, arg: str, msg: IncomingMessage, sink: ReplySink) -> None:
        """새 프로젝트 폴더를 생성하고 기본 프로젝트로 전환한다."""
        if not arg:
            sink.send_immediate("사용법: `!새프로젝트 <폴더이름> [설명]`\n예: `!새프로젝트 WeatherBot 날씨 알림 자동화`")
            return
        parts = arg.split(maxsplit=1)
        name = parts[0].strip()
        desc = parts[1].strip() if len(parts) > 1 else ""
        try:
            proj = register_dynamic_project(self.config, name=name, description=desc, persist_yaml=True)
        except ValueError as exc:
            sink.send_immediate(f":warning: 프로젝트 생성 실패: {exc}")
            return

        self.store.save_dynamic_project(proj.key, str(proj.path), proj.description)
        _, model, mode = self.get_channel_context(msg.channel_id)
        self._save_channel_context(msg.channel_id, proj.key, model, mode)
        sink.send_immediate(
            f":tada: 새 프로젝트 `{proj.key}`를 생성하고 현재 채널 기본 프로젝트로 선택했습니다.\n• 경로: `{proj.path}`\n• 설명: {proj.description}"
        )

    def _cmd_model(self, arg: str, msg: IncomingMessage, sink: ReplySink) -> None:
        """AI 모델 목록 조회 또는 변경을 처리한다."""
        proj, current_model, mode = self.get_channel_context(msg.channel_id)
        target = arg.strip()
        if not target:
            lines = [f"*현재 AI 모델*: `{current_model}`", "*사용 가능한 추천 모델*:"]
            for m in self.config.ai.available_models:
                mark = " (선택됨)" if m == current_model else ""
                lines.append(f"• `{m}`{mark}")
            lines.append("\n변경: `!모델 <모델명>`")
            sink.send_immediate("\n".join(lines))
            return

        self._save_channel_context(msg.channel_id, proj, target, mode)
        warn = ""
        if self.config.ai.available_models and target not in self.config.ai.available_models:
            warn = "\n_참고: 추천 목록 외 모델명을 직접 지정했습니다. 실행 전 유효성을 확인하세요._"
        sink.send_immediate(f":white_check_mark: AI 모델을 `{target}`로 변경했습니다.{warn}")

    def _cmd_mode(self, arg: str, msg: IncomingMessage, sink: ReplySink) -> None:
        """AI 실행 모드(dev/plan)를 조회하거나 변경한다."""
        proj, model, current_mode = self.get_channel_context(msg.channel_id)
        target = arg.strip().lower()
        if not target:
            sink.send_immediate(
                f"*현재 실행 모드*: `{current_mode}`\n"
                "• `dev`: 파일 생성·수정·명령 실행 포함 전체 개발 수행\n"
                "• `plan`: 코드 수정 없이 읽기 전용 분석 및 계획 수립\n"
                "변경: `!모드 dev` 또는 `!모드 plan`"
            )
            return

        if target not in VALID_AI_MODES:
            sink.send_immediate(":warning: 지원되는 모드는 `dev` 또는 `plan`입니다.")
            return

        self._save_channel_context(msg.channel_id, proj, model, target)
        sink.send_immediate(f":white_check_mark: 기본 실행 모드를 `{target}`로 변경했습니다.")

    # ------------------------------------------------------------------
    # AI 개발 작업 실행
    # ------------------------------------------------------------------
    def _dispatch_ai_prompt(
        self,
        raw_prompt: str,
        msg: IncomingMessage,
        sink: ReplySink,
        mode_override: str | None = None,
    ) -> None:
        """AI 개발/분석 요청을 파싱해 비동기(또는 동기)로 실행한다."""
        prompt = raw_prompt.strip()
        explicit_project: str | None = None

        prefix_match = _PROJECT_PREFIX_RE.match(prompt)
        if prefix_match:
            candidate_proj = prefix_match.group(1).strip().lower()
            inner_prompt = prefix_match.group(2).strip()
            if candidate_proj not in self.config.projects:
                sink.send_immediate(
                    f":warning: `[ {candidate_proj} ]`에 해당하는 프로젝트가 없습니다.\n`!프로젝트`로 등록된 키를 확인해 주세요."
                )
                return
            explicit_project = candidate_proj
            prompt = inner_prompt

        chan_proj, chan_model, chan_mode = self.get_channel_context(msg.channel_id)

        conversation_id: str | None = None
        project_key = explicit_project or chan_proj
        model = chan_model
        mode = mode_override or chan_mode

        # 스레드 답글이면 기존 스레드 세션을 이어받음 (단, 명시적으로 다른 프로젝트를 지정한 경우는 새 대화 시작)
        if msg.is_thread_reply:
            session = self.store.get_thread_session(msg.channel_id, msg.thread_ts)
            if session:
                if not explicit_project or explicit_project == session.project_key:
                    project_key = session.project_key
                    conversation_id = session.conversation_id or None
                    model = chan_model or session.model
                    mode = mode_override or chan_mode or session.mode

        if project_key not in self.config.projects:
            sink.send_immediate(f":warning: 프로젝트 `{project_key}`를 찾을 수 없습니다.")
            return

        project_cfg = self.config.projects[project_key]
        if not project_cfg.path.exists():
            sink.send_immediate(
                f":warning: 프로젝트 폴더가 존재하지 않습니다: `{project_cfg.path}`"
            )
            return

        full_prompt = prompt
        if mode == "plan":
            full_prompt = (
                "[읽기 전용 분석 모드: 파일을 생성·수정·삭제하지 말고 조사와 계획만 한국어로 보고하세요.]\n\n"
                + full_prompt
            )
        if self.config.ai.system_prefix and not conversation_id:
            full_prompt = f"{self.config.ai.system_prefix}\n\n{full_prompt}"

        self._launch_ai_job(
            prompt=full_prompt,
            raw_summary=prompt,
            project_cfg=project_cfg,
            model=model,
            mode=mode,
            conversation_id=conversation_id,
            user_id=msg.user_id,
            channel_id=msg.channel_id,
            thread_ts=msg.thread_ts,
            sink=sink,
        )

    def _launch_ai_job(
        self,
        prompt: str,
        raw_summary: str,
        project_cfg: ProjectConfig,
        model: str,
        mode: str,
        conversation_id: str | None,
        user_id: str,
        channel_id: str,
        thread_ts: str,
        sink: ReplySink,
    ) -> str:
        """AI 개발 작업을 생성해 실행한다."""
        job_id = uuid.uuid4().hex[:8]
        log_path = str(self.config.base_dir / "logs" / f"job_{job_id}.log")
        self.store.create_job(
            job_id=job_id,
            kind="ai",
            project_key=project_cfg.key,
            target=raw_summary[:200],
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            log_path=log_path,
        )

        active_job = ActiveJob(
            job_id=job_id,
            kind="ai",
            project_key=project_cfg.key,
            target=raw_summary[:80],
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            started_at=time.monotonic(),
        )
        with self._jobs_lock:
            self._active_jobs[job_id] = active_job

        conv_label = f"이어쓰기 `{conversation_id[:8]}`" if conversation_id else "새 세션"
        initial_text = format_progress_message(
            job_id=job_id,
            title="AI 개발 작업 시작" if mode == "dev" else "AI 분석/계획 시작",
            project_key=project_cfg.key,
            detail_label=f"모델: `{model}` | 모드: `{mode}` | {conv_label}",
            elapsed_sec=0.0,
            recent_steps=["대기열 진입 및 환경 준비 중..."],
        )
        handle = sink.start_progress(initial_text)

        def _worker() -> None:
            proj_lock = self._get_project_lock(project_cfg.key)
            recent_steps: deque[str] = deque(maxlen=8)
            last_update_ts = 0.0
            update_interval = self.config.reporting.update_interval_sec
            ui_lock = threading.Lock()

            def _on_progress(evt: ProgressEvent) -> None:
                nonlocal last_update_ts
                with ui_lock:
                    if evt.kind == "tool_done":
                        # 이미 tool_start가 있다면 중복 줄 추가 최소화
                        pass
                    else:
                        recent_steps.append(evt.message)
                    now_m = time.monotonic()
                    if now_m - last_update_ts >= update_interval:
                        last_update_ts = now_m
                        msg_text = format_progress_message(
                            job_id=job_id,
                            title="AI 개발 진행 중" if mode == "dev" else "AI 분석 진행 중",
                            project_key=project_cfg.key,
                            detail_label=f"모델: `{model}` | 모드: `{mode}`",
                            elapsed_sec=now_m - active_job.started_at,
                            recent_steps=recent_steps,
                        )
                        sink.update_progress(handle, msg_text)

            result: AgyResult | None = None
            try:
                if not proj_lock.acquire(blocking=False):
                    recent_steps.append("같은 프로젝트의 선행 작업 완료 대기 중...")
                    sink.update_progress(
                        handle,
                        format_progress_message(
                            job_id=job_id,
                            title="선행 작업 대기 중",
                            project_key=project_cfg.key,
                            detail_label=f"모델: `{model}` | 모드: `{mode}`",
                            elapsed_sec=time.monotonic() - active_job.started_at,
                            recent_steps=recent_steps,
                        ),
                    )
                    proj_lock.acquire()
                try:
                    if active_job.cancel_event.is_set():
                        result = AgyResult(
                            success=False,
                            status="CANCELLED",
                            response="",
                            error="실행 대기 중 사용자 요청으로 중단되었습니다.",
                            cancelled=True,
                        )
                    else:
                        result = self.agy_runner.run(
                            job_id=job_id,
                            prompt=prompt,
                            cwd=project_cfg.path,
                            model=model,
                            mode=mode,
                            conversation_id=conversation_id,
                            on_progress=_on_progress,
                            cancel_event=active_job.cancel_event,
                        )
                finally:
                    proj_lock.release()
            except Exception as exc:
                logger.exception("AI 작업(%s) 워커 예외: %s", job_id, exc)
                result = AgyResult(
                    success=False,
                    status="ERROR",
                    response="",
                    error=f"내부 실행 오류: {exc}",
                    log_path=log_path,
                )
            finally:
                with self._jobs_lock:
                    self._active_jobs.pop(job_id, None)

            assert result is not None
            final_conv_id = result.conversation_id or conversation_id or ""
            if final_conv_id:
                self.store.upsert_thread_session(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    conversation_id=final_conv_id,
                    project_key=project_cfg.key,
                    model=model,
                    mode=mode,
                )

            db_status = "cancelled" if result.cancelled else ("success" if result.success else "failed")
            summary_line = (result.response or result.error or db_status)[:240]
            self.store.finish_job(
                job_id=job_id,
                status=db_status,
                summary=summary_line,
                duration_sec=result.duration_sec,
                log_path=result.log_path or log_path,
                conversation_id=final_conv_id,
            )

            report_text = format_agy_report(
                job_id=job_id,
                project_key=project_cfg.key,
                model=model,
                mode=mode,
                result=result,
            )
            chunks = split_slack_message(report_text, max_chars=self.config.reporting.max_message_chars)
            sink.finish(
                handle=handle,
                text_chunks=chunks,
                success=result.success,
                cancelled=result.cancelled,
                log_path=result.log_path or log_path,
                upload_log=self.config.reporting.upload_long_logs,
            )

        if self.background:
            thread = threading.Thread(target=_worker, name=f"AgyJob-{job_id}", daemon=True)
            active_job.thread = thread
            thread.start()
        else:
            _worker()

        return job_id

    # ------------------------------------------------------------------
    # 프리셋 작업 실행 및 작업 제어
    # ------------------------------------------------------------------
    def _cmd_run_task(self, arg: str, msg: IncomingMessage, sink: ReplySink) -> None:
        """등록된 프로젝트 작업 목록을 보여주거나 지정한 작업을 실행한다."""
        if not arg:
            lines = ["*실행 가능한 등록 작업 프리셋*"]
            for key, task in self.config.tasks.items():
                lines.append(f"• `{key}` (`{task.project}`) — {task.description}")
            lines.append("\n실행 예시: `!실행 ondotrip-status` 또는 `!실행 mealandbrew`")
            sink.send_immediate("\n".join(lines))
            return

        parts = arg.split(maxsplit=1)
        task_key = parts[0].strip().lower()
        extra_args = parts[1].strip() if len(parts) > 1 else ""

        if task_key not in self.config.tasks:
            sink.send_immediate(
                f":warning: 등록되지 않은 작업입니다: `{task_key}`\n`!실행`을 입력해 사용 가능한 목록을 확인하세요."
            )
            return

        task_cfg = self.config.tasks[task_key]
        if task_cfg.project not in self.config.projects:
            sink.send_immediate(f":warning: 작업에 연결된 프로젝트 `{task_cfg.project}`를 찾을 수 없습니다.")
            return

        project_cfg = self.config.projects[task_cfg.project]
        self._launch_task_job(
            task_key=task_key,
            extra_args=extra_args,
            project_cfg=project_cfg,
            user_id=msg.user_id,
            channel_id=msg.channel_id,
            thread_ts=msg.thread_ts,
            sink=sink,
        )

    def _launch_task_job(
        self,
        task_key: str,
        extra_args: str,
        project_cfg: ProjectConfig,
        user_id: str,
        channel_id: str,
        thread_ts: str,
        sink: ReplySink,
    ) -> str:
        """등록된 프리셋 작업을 실행한다."""
        task_cfg = self.config.tasks[task_key]
        job_id = uuid.uuid4().hex[:8]
        log_path = str(self.config.base_dir / "logs" / f"job_{job_id}.log")
        self.store.create_job(
            job_id=job_id,
            kind="task",
            project_key=project_cfg.key,
            target=f"{task_key} {extra_args}".strip(),
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            log_path=log_path,
        )

        active_job = ActiveJob(
            job_id=job_id,
            kind="task",
            project_key=project_cfg.key,
            target=task_key,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            started_at=time.monotonic(),
        )
        with self._jobs_lock:
            self._active_jobs[job_id] = active_job

        initial_text = format_progress_message(
            job_id=job_id,
            title=f"작업 실행 시작 ({task_key})",
            project_key=project_cfg.key,
            detail_label=f"설명: {task_cfg.description}",
            elapsed_sec=0.0,
            recent_steps=["프로세스 준비 중..."],
        )
        handle = sink.start_progress(initial_text)

        def _worker() -> None:
            recent_lines: deque[str] = deque(maxlen=6)
            last_update_ts = 0.0
            update_interval = self.config.reporting.update_interval_sec
            ui_lock = threading.Lock()

            def _on_line(line: str) -> None:
                nonlocal last_update_ts
                with ui_lock:
                    recent_lines.append(f"`{line[:100]}`")
                    now_m = time.monotonic()
                    if now_m - last_update_ts >= update_interval:
                        last_update_ts = now_m
                        sink.update_progress(
                            handle,
                            format_progress_message(
                                job_id=job_id,
                                title=f"작업 실행 중 ({task_key})",
                                project_key=project_cfg.key,
                                detail_label=f"설명: {task_cfg.description}",
                                elapsed_sec=now_m - active_job.started_at,
                                recent_steps=recent_lines,
                            ),
                        )

            result: TaskResult | None = None
            try:
                result = self.task_runner.run(
                    job_id=job_id,
                    task=task_cfg,
                    cwd=project_cfg.path,
                    extra_args=extra_args,
                    on_progress=_on_line,
                    cancel_event=active_job.cancel_event,
                )
            except Exception as exc:
                logger.exception("프리셋 작업(%s) 실행 예외: %s", job_id, exc)
                result = TaskResult(
                    success=False,
                    return_code=-1,
                    log_path=log_path,
                    error=f"내부 실행 오류: {exc}",
                )
            finally:
                with self._jobs_lock:
                    self._active_jobs.pop(job_id, None)

            assert result is not None
            db_status = "cancelled" if result.cancelled else ("success" if result.success else "failed")
            summary_line = (
                "\n".join(result.stdout_tail[-3:]) if result.stdout_tail else (result.error or db_status)
            )[:240]
            self.store.finish_job(
                job_id=job_id,
                status=db_status,
                summary=summary_line,
                duration_sec=result.duration_sec,
                log_path=result.log_path or log_path,
            )

            report_text = format_task_report(
                job_id=job_id,
                task_key=task_key,
                project_key=project_cfg.key,
                result=result,
            )
            chunks = split_slack_message(report_text, max_chars=self.config.reporting.max_message_chars)
            sink.finish(
                handle=handle,
                text_chunks=chunks,
                success=result.success,
                cancelled=result.cancelled,
                log_path=result.log_path or log_path,
                upload_log=self.config.reporting.upload_long_logs,
            )

        if self.background:
            thread = threading.Thread(target=_worker, name=f"TaskJob-{job_id}", daemon=True)
            active_job.thread = thread
            thread.start()
        else:
            _worker()

        return job_id

    def _build_jobs_text(self) -> str:
        """현재 실행 중 작업과 최근 완료 작업 이력을 정리한다."""
        active = self.list_active_jobs()
        recent = self.store.list_recent_jobs(limit=10)

        lines = ["*작업 현황 및 최근 이력*"]
        if active:
            lines.append("*[실행 중]*")
            for job in active:
                elapsed = time.monotonic() - job.started_at
                lines.append(f"• `{job.job_id}` [{job.kind}] `{job.project_key}` — {job.target} ({elapsed:.0f}초 경과)")
        else:
            lines.append("• 현재 실행 중인 작업이 없습니다.")

        if recent:
            lines.append("\n*[최근 이력 10건]*")
            for rec in recent:
                lines.append(
                    f"• `{rec.job_id}` [{rec.kind}] `{rec.project_key}` | 상태: `{rec.status}` | 소요: `{rec.duration_sec:.1f}초` — {rec.target[:50]}"
                )
        return "\n".join(lines)

    def _cmd_stop_job(self, arg: str, sink: ReplySink) -> None:
        """실행 중인 작업 하나 또는 전체를 중단한다."""
        target = arg.strip().lower()
        if not target:
            sink.send_immediate("사용법: `!중단 <작업ID>` 또는 `!중단 all`")
            return

        active = self.list_active_jobs()
        if not active:
            sink.send_immediate("현재 실행 중인 작업이 없습니다.")
            return

        targets = active if target == "all" else [j for j in active if j.job_id.lower() == target]
        if not targets:
            sink.send_immediate(f":warning: 실행 중인 작업 ID `{target}`를 찾지 못했습니다. (`!작업`으로 확인)")
            return

        stopped_ids: list[str] = []
        for job in targets:
            job.cancel_event.set()
            if job.kind == "ai":
                self.agy_runner.cancel(job.job_id)
            else:
                self.task_runner.cancel(job.job_id)
            stopped_ids.append(job.job_id)

        sink.send_immediate(
            f":octagonal_sign: 중단 요청을 전달했습니다: {', '.join(f'`{jid}`' for jid in stopped_ids)}"
        )

    def _cmd_show_log(self, arg: str, sink: ReplySink) -> None:
        """지정한 작업 ID의 로그 파일 끝부분을 조회해 전송한다."""
        job_id = arg.strip()
        if not job_id:
            sink.send_immediate("사용법: `!로그 <작업ID>`")
            return
        rec = self.store.get_job(job_id)
        if not rec or not rec.log_path:
            sink.send_immediate(f":warning: 작업 ID `{job_id}`의 로그 정보를 찾지 못했습니다.")
            return
        log_file = Path(rec.log_path)
        if not log_file.is_file():
            sink.send_immediate(f":warning: 로그 파일이 존재하지 않습니다: `{log_file}`")
            return

        content = log_file.read_text(encoding="utf-8", errors="replace")
        tail = content[-3000:] if len(content) > 3000 else content
        sink.send_immediate(f"*작업 `{job_id}` 로그 (마지막 부분)*\n```\n{tail}\n```")

    # ------------------------------------------------------------------
    # 내장 예약 관리
    # ------------------------------------------------------------------
    def _cmd_schedule(self, arg: str, msg: IncomingMessage, sink: ReplySink) -> None:
        """`!예약` 서브명령어(목록/추가/삭제/켜기/끄기)를 처리한다."""
        if not arg or arg.strip().lower() in {"목록", "list"}:
            schedules = self.store.list_schedules()
            if not schedules:
                sink.send_immediate(
                    "등록된 내장 예약이 없습니다.\n"
                    "추가 예시:\n"
                    "• `!예약 추가 09:30 run mealandbrew`\n"
                    "• `!예약 추가 10:00 ai ondotrip 오늘자 로그 점검해줘`"
                )
                return
            lines = ["*Slack 내장 일일 예약 목록*"]
            for s in schedules:
                state = "활성" if s.enabled else "중지됨"
                last_run = s.last_run_date or "없음"
                proj_part = f"`{s.project_key}` " if s.project_key else ""
                lines.append(
                    f"• `#{s.schedule_id}` | 매일 `{s.time_hhmm}` | `{s.kind}` {proj_part}| 상태: `{state}` | 최근 실행: {last_run} | 내용: `{s.payload}`"
                )
            sink.send_immediate("\n".join(lines))
            return

        tokens = arg.split(maxsplit=1)
        sub = tokens[0].strip().lower()
        rest = tokens[1].strip() if len(tokens) > 1 else ""

        if sub in {"추가", "add"}:
            # 형식: HH:MM run <task> [args] | HH:MM ai <project> <prompt>
            parts = rest.split(maxsplit=2)
            if len(parts) < 3:
                sink.send_immediate(
                    "사용법:\n"
                    "• `!예약 추가 <HH:MM> run <작업명>`\n"
                    "• `!예약 추가 <HH:MM> ai <프로젝트키> <AI 지시내용>`"
                )
                return
            time_hhmm, kind, tail = parts[0].strip(), parts[1].strip().lower(), parts[2].strip()
            if not _TIME_HHMM_RE.match(time_hhmm):
                sink.send_immediate(":warning: 시각 형식은 `HH:MM` (00:00 ~ 23:59)이어야 합니다. 예: `09:30`")
                return
            if kind not in {"run", "ai"}:
                sink.send_immediate(":warning: 예약 종류는 `run` 또는 `ai`만 지원합니다.")
                return

            if kind == "run":
                task_key = tail.split(maxsplit=1)[0].lower()
                if task_key not in self.config.tasks:
                    sink.send_immediate(f":warning: 등록되지 않은 작업입니다: `{task_key}` (`!실행`으로 목록 확인)")
                    return
                proj_key = self.config.tasks[task_key].project
                payload = tail
            else:
                ai_parts = tail.split(maxsplit=1)
                if len(ai_parts) < 2:
                    sink.send_immediate("사용법: `!예약 추가 <HH:MM> ai <프로젝트키> <AI 지시내용>`")
                    return
                proj_key = ai_parts[0].strip().lower()
                payload = ai_parts[1].strip()
                if proj_key not in self.config.projects:
                    sink.send_immediate(f":warning: 등록되지 않은 프로젝트입니다: `{proj_key}`")
                    return

            target_channel = msg.channel_id or self.config.slack.report_channel_id
            created = self.store.add_schedule(
                time_hhmm=time_hhmm,
                kind=kind,
                project_key=proj_key,
                payload=payload,
                channel_id=target_channel,
                created_by=msg.user_id,
            )
            sink.send_immediate(
                f":alarm_clock: 예약 `#{created.schedule_id}`가 등록되었습니다.\n"
                f"• 시각: 매일 `{created.time_hhmm}` | 유형: `{created.kind}` | 프로젝트: `{created.project_key}`\n"
                f"• 내용: `{created.payload}` | 보고 채널: `{created.channel_id}`"
            )
            return

        if sub in {"삭제", "del", "delete", "remove"}:
            sid = self._parse_schedule_id(rest)
            if sid is None:
                sink.send_immediate("사용법: `!예약 삭제 <예약ID>`")
                return
            if self.store.delete_schedule(sid):
                sink.send_immediate(f":wastebasket: 예약 `#{sid}`를 삭제했습니다.")
            else:
                sink.send_immediate(f":warning: 예약 `#{sid}`를 찾을 수 없습니다.")
            return

        if sub in {"켜기", "enable"}:
            sid = self._parse_schedule_id(rest)
            if sid is None:
                sink.send_immediate("사용법: `!예약 켜기 <예약ID>`")
                return
            if self.store.set_schedule_enabled(sid, True):
                sink.send_immediate(f":white_check_mark: 예약 `#{sid}`를 활성화했습니다.")
            else:
                sink.send_immediate(f":warning: 예약 `#{sid}`를 찾을 수 없습니다.")
            return

        if sub in {"끄기", "disable"}:
            sid = self._parse_schedule_id(rest)
            if sid is None:
                sink.send_immediate("사용법: `!예약 끄기 <예약ID>`")
                return
            if self.store.set_schedule_enabled(sid, False):
                sink.send_immediate(f":pause_button: 예약 `#{sid}`를 비활성화했습니다.")
            else:
                sink.send_immediate(f":warning: 예약 `#{sid}`를 찾을 수 없습니다.")
            return

        sink.send_immediate(
            ":warning: 지원되지 않는 예약 명령입니다. `!예약 목록`, `!예약 추가`, `!예약 삭제`, `!예약 켜기`, `!예약 끄기`를 사용하세요."
        )

    @staticmethod
    def _parse_schedule_id(raw: str) -> int | None:
        """`#1` 또는 `1` 형태의 예약 ID를 정수로 파싱한다."""
        cleaned = raw.strip().lstrip("#")
        if not cleaned.isdigit():
            return None
        return int(cleaned)

    def execute_schedule(
        self,
        sched: ScheduleRecord,
        sink_factory: Callable[[str, str], ReplySink],
    ) -> None:
        """내장 스케줄러에 의해 트리거된 예약 작업을 실행한다."""
        channel_id = sched.channel_id or self.config.slack.report_channel_id
        if not channel_id:
            logger.warning("예약 #%s 실행: 보고할 채널 ID가 없어 건너뜁니다.", sched.schedule_id)
            return

        sink = sink_factory(channel_id, "")
        sink.send_immediate(
            f":alarm_clock: *예약 작업 `#{sched.schedule_id}` 자동 실행 시작* (`{sched.time_hhmm}` / `{sched.kind}`)"
        )

        if sched.kind == "run":
            parts = sched.payload.split(maxsplit=1)
            task_key = parts[0].strip().lower()
            extra_args = parts[1].strip() if len(parts) > 1 else ""
            if task_key not in self.config.tasks:
                sink.send_immediate(f":x: 예약 작업 `#{sched.schedule_id}` 실패: 작업 `{task_key}` 없음")
                return
            proj_cfg = self.config.projects.get(self.config.tasks[task_key].project)
            if not proj_cfg:
                sink.send_immediate(f":x: 예약 작업 `#{sched.schedule_id}` 실패: 프로젝트 없음")
                return
            self._launch_task_job(
                task_key=task_key,
                extra_args=extra_args,
                project_cfg=proj_cfg,
                user_id="scheduler",
                channel_id=channel_id,
                thread_ts="",
                sink=sink,
            )
            return

        if sched.kind == "ai":
            proj_cfg = self.config.projects.get(sched.project_key)
            if not proj_cfg:
                sink.send_immediate(f":x: 예약 작업 `#{sched.schedule_id}` 실패: 프로젝트 `{sched.project_key}` 없음")
                return
            _, model, mode = self.get_channel_context(channel_id)
            full_prompt = sched.payload
            if self.config.ai.system_prefix:
                full_prompt = f"{self.config.ai.system_prefix}\n\n{sched.payload}"
            self._launch_ai_job(
                prompt=full_prompt,
                raw_summary=sched.payload,
                project_cfg=proj_cfg,
                model=model,
                mode=mode,
                conversation_id=None,
                user_id="scheduler",
                channel_id=channel_id,
                thread_ts="",
                sink=sink,
            )
