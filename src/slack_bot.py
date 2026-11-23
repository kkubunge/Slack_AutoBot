"""Slack Bolt + Socket Mode 어댑터 및 실시간 메시지/리액션/파일 보고 모듈."""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from src.command_router import CommandRouter, IncomingMessage, ReplySink
from src.config import AppConfig
from src.scheduler_service import SchedulerService
from src.state_store import ScheduleRecord, StateStore

logger = logging.getLogger(__name__)


class _ProcessedEventCache:
    """Slack 재전송 및 app_mention/message 동시 수신 중복을 방지하는 TTL 캐시."""

    def __init__(self, ttl_sec: float = 120.0) -> None:
        self._ttl = ttl_sec
        self._lock = threading.Lock()
        self._seen: dict[tuple[str, str], float] = {}

    def mark_if_new(self, channel_id: str, ts: str) -> bool:
        """처음 본 이벤트면 기록하고 True, 이미 처리했으면 False를 반환한다."""
        if not channel_id or not ts:
            return True
        now = time.monotonic()
        key = (channel_id, ts)
        with self._lock:
            expired = [k for k, exp in self._seen.items() if exp <= now]
            for k in expired:
                self._seen.pop(k, None)
            if key in self._seen:
                return False
            self._seen[key] = now + self._ttl
            return True


class SlackReplySink(ReplySink):
    """Slack Web API(`chat_postMessage`, `chat_update`, `reactions`, `files_upload_v2`) 구현체."""

    def __init__(
        self,
        client: Any,
        channel_id: str,
        thread_ts: str = "",
        reaction_ts: str = "",
    ) -> None:
        self.client = client
        self.channel_id = channel_id
        self.thread_ts = thread_ts or None
        self.reaction_ts = reaction_ts or None

    def _add_reaction(self, emoji: str) -> None:
        if not self.reaction_ts:
            return
        try:
            self.client.reactions_add(
                channel=self.channel_id,
                timestamp=self.reaction_ts,
                name=emoji,
            )
        except Exception:
            pass

    def _remove_reaction(self, emoji: str) -> None:
        if not self.reaction_ts:
            return
        try:
            self.client.reactions_remove(
                channel=self.channel_id,
                timestamp=self.reaction_ts,
                name=emoji,
            )
        except Exception:
            pass

    def send_immediate(self, text: str) -> None:
        """단발성 응답을 스레드(또는 채널)로 전송한다."""
        kwargs: dict[str, Any] = {"channel": self.channel_id, "text": text}
        if self.thread_ts:
            kwargs["thread_ts"] = self.thread_ts
        self.client.chat_postMessage(**kwargs)

    def start_progress(self, initial_text: str) -> str:
        """진행 중 리액션(`eyes`)을 달고 상태 메시지를 게시한 뒤 메시지 `ts`를 반환한다."""
        self._add_reaction("eyes")
        kwargs: dict[str, Any] = {"channel": self.channel_id, "text": initial_text}
        if self.thread_ts:
            kwargs["thread_ts"] = self.thread_ts
        resp = self.client.chat_postMessage(**kwargs)
        return str(resp.get("ts") or "")

    def update_progress(self, handle: str, text: str) -> None:
        """기존 상태 메시지를 실시간으로 수정(`chat_update`)한다."""
        if not handle:
            return
        try:
            self.client.chat_update(channel=self.channel_id, ts=handle, text=text)
        except Exception as exc:
            logger.debug("Slack 진행 메시지 업데이트 실패 (무시됨): %s", exc)

    def finish(
        self,
        handle: str,
        text_chunks: list[str],
        success: bool,
        cancelled: bool = False,
        log_path: str = "",
        upload_log: bool = False,
    ) -> None:
        """완료 리액션을 갱신하고 최종 보고 청크 및 필요 시 로그 파일을 업로드한다."""
        self._remove_reaction("eyes")
        if cancelled:
            self._add_reaction("octagonal_sign")
        elif success:
            self._add_reaction("white_check_mark")
        else:
            self._add_reaction("x")

        chunks = text_chunks or ["(출력 없음)"]
        if handle:
            try:
                self.client.chat_update(channel=self.channel_id, ts=handle, text=chunks[0])
            except Exception:
                self.send_immediate(chunks[0])
        else:
            self.send_immediate(chunks[0])

        for extra in chunks[1:]:
            self.send_immediate(extra)

        if upload_log and log_path and (len(chunks) > 1 or not success):
            log_file = Path(log_path)
            if log_file.is_file() and log_file.stat().st_size > 0:
                try:
                    upload_kwargs: dict[str, Any] = {
                        "channel": self.channel_id,
                        "file": str(log_file),
                        "filename": log_file.name,
                        "title": f"실행 로그 ({log_file.name})",
                    }
                    if self.thread_ts:
                        upload_kwargs["thread_ts"] = self.thread_ts
                    self.client.files_upload_v2(**upload_kwargs)
                except Exception as exc:
                    logger.warning("로그 파일 Slack 업로드 실패: %s", exc)


class SlackControllerBot:
    """Slack Socket Mode 연결과 이벤트/스케줄러 수명주기를 관리하는 메인 봇 클래스."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.store = StateStore(config.base_dir / "data" / "state.db")
        self.router = CommandRouter(config=config, store=self.store, background=True)
        self.event_cache = _ProcessedEventCache()
        self.bot_user_id: str = ""

        self.app = App(token=config.slack.bot_token)
        self.scheduler = SchedulerService(
            store=self.store,
            on_trigger=self._on_schedule_trigger,
            poll_interval_sec=15.0,
        )
        self._register_handlers()

    def _resolve_bot_user_id(self) -> str:
        """auth.test API를 통해 자신의 봇 User ID를 확인한다."""
        if self.bot_user_id:
            return self.bot_user_id
        try:
            resp = self.app.client.auth_test()
            self.bot_user_id = str(resp.get("user_id") or "")
        except Exception as exc:
            logger.warning("봇 User ID 조회 실패: %s", exc)
        return self.bot_user_id

    def _on_schedule_trigger(self, sched: ScheduleRecord) -> None:
        """내장 스케줄러가 예약 시각에 도달하면 Slack 채널로 작업을 실행한다."""
        def _sink_factory(channel_id: str, thread_ts: str) -> ReplySink:
            return SlackReplySink(
                client=self.app.client,
                channel_id=channel_id,
                thread_ts=thread_ts,
                reaction_ts="",
            )

        self.router.execute_schedule(sched, _sink_factory)

    def _register_handlers(self) -> None:
        """app_mention 및 message 이벤트 핸들러를 등록한다."""

        @self.app.event("app_mention")
        def handle_app_mention(event: dict[str, Any], client: Any) -> None:
            channel_id = str(event.get("channel") or "")
            msg_ts = str(event.get("ts") or "")
            user_id = str(event.get("user") or "")
            if not user_id or event.get("bot_id"):
                return
            if not self.event_cache.mark_if_new(channel_id, msg_ts):
                return

            raw_thread_ts = str(event.get("thread_ts") or "")
            is_thread_reply = bool(raw_thread_ts and raw_thread_ts != msg_ts)
            effective_thread_ts = raw_thread_ts or msg_ts
            # 채널 본문 호출은 본문에 바로 응답하고, 기존 스레드 내 호출만 스레드를 유지한다.
            reply_thread_ts = raw_thread_ts

            sink = SlackReplySink(
                client=client,
                channel_id=channel_id,
                thread_ts=reply_thread_ts,
                reaction_ts=msg_ts,
            )
            incoming = IncomingMessage(
                text=str(event.get("text") or ""),
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=effective_thread_ts,
                message_ts=msg_ts,
                is_thread_reply=is_thread_reply,
                is_dm=False,
            )
            self.router.handle_message(incoming, sink)

        @self.app.event("message")
        def handle_message_events(event: dict[str, Any], client: Any) -> None:
            if event.get("subtype") or event.get("bot_id"):
                return

            user_id = str(event.get("user") or "")
            channel_id = str(event.get("channel") or "")
            msg_ts = str(event.get("ts") or "")
            text = str(event.get("text") or "")
            channel_type = str(event.get("channel_type") or "")
            is_dm = channel_type == "im"

            if not user_id or not channel_id or not msg_ts:
                return

            bot_uid = self._resolve_bot_user_id()
            has_bot_mention = bool(bot_uid and re.search(rf"<@{re.escape(bot_uid)}>", text))

            # 채널에서 @봇 멘션이 포함된 경우 app_mention 핸들러에 처리를 위임
            if not is_dm and has_bot_mention:
                return

            raw_thread_ts = str(event.get("thread_ts") or "")
            is_thread_reply = bool(raw_thread_ts and raw_thread_ts != msg_ts)
            effective_thread_ts = raw_thread_ts or msg_ts

            if not is_dm:
                stripped = text.strip()
                is_bang_cmd = stripped.startswith("!")
                is_tracked_thread = False
                if (
                    not is_bang_cmd
                    and is_thread_reply
                    and self.config.slack.auto_reply_threads
                ):
                    is_tracked_thread = (
                        self.store.get_thread_session(channel_id, effective_thread_ts) is not None
                    )
                if not is_bang_cmd and not is_tracked_thread:
                    return

            if not self.event_cache.mark_if_new(channel_id, msg_ts):
                return

            # DM에서는 항상 본문에 바로 응답하고, 채널에서는 기존 스레드 내 호출만 스레드를 유지한다.
            reply_thread_ts = "" if is_dm else raw_thread_ts
            sink = SlackReplySink(
                client=client,
                channel_id=channel_id,
                thread_ts=reply_thread_ts,
                reaction_ts=msg_ts,
            )
            incoming = IncomingMessage(
                text=text,
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=effective_thread_ts,
                message_ts=msg_ts,
                is_thread_reply=is_thread_reply,
                is_dm=is_dm,
            )
            self.router.handle_message(incoming, sink)

    def start(self) -> None:
        """스케줄러와 Slack Socket Mode 핸들러를 시작한다."""
        bot_uid = self._resolve_bot_user_id()
        logger.info("Slack 봇 연결 준비 완료 (bot_user_id=%s)", bot_uid or "unknown")

        if self.config.slack.report_channel_id:
            try:
                self.app.client.chat_postMessage(
                    channel=self.config.slack.report_channel_id,
                    text=(
                        ":rocket: *AutoBot(오토봇)이 온라인 상태가 되었습니다.*\n"
                        f"• 기본 프로젝트: `{self.config.default_project}` | 기본 모델: `{self.config.ai.default_model}`\n"
                        "• 명령어 안내는 `!도움`을 입력해 주세요."
                    ),
                )
            except Exception as exc:
                logger.warning("시작 알림 전송 실패: %s", exc)

        self.scheduler.start()
        handler = SocketModeHandler(self.app, self.config.slack.app_token)
        try:
            handler.start()
        finally:
            self.scheduler.stop()
            self.store.close()
