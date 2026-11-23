"""표준 라이브러리 urllib 기반 Slack Web API 보고 모듈 (클라우드 러너용)."""

from __future__ import annotations

import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

# 컨트롤러 루트의 src.formatter 재사용 보장
_CONTROLLER_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_CONTROLLER_ROOT) not in sys.path:
    sys.path.insert(0, str(_CONTROLLER_ROOT))

from src.formatter import split_slack_message  # noqa: E402

logger = logging.getLogger(__name__)


def _default_json_post(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    """urllib을 사용해 JSON POST 요청을 보내고 응답을 파싱한다."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {"ok": True}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "error": f"HTTP {exc.code}: {err_body[:300]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


class SlackWebReporter:
    """GitHub Actions 실행 중/완료 상태를 Slack 채널 스레드로 보고하는 클라이언트."""

    def __init__(
        self,
        bot_token: str,
        transport: Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.bot_token = bot_token.strip()
        self._post = transport or _default_json_post

    def _call_slack(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.bot_token:
            return {"ok": False, "error": "SLACK_BOT_TOKEN 미설정"}
        return self._post(
            f"https://slack.com/api/{method}",
            {
                "Authorization": f"Bearer {self.bot_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            payload,
        )

    def post_message(self, channel: str, text: str, thread_ts: str = "") -> str:
        """채널 또는 스레드에 메시지를 전송하고 생성된 ts를 반환한다."""
        if not channel:
            return ""
        payload: dict[str, Any] = {"channel": channel, "text": text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        res = self._call_slack("chat.postMessage", payload)
        if not res.get("ok"):
            logger.warning("Slack post_message 실패: %s", res.get("error"))
            return ""
        return str(res.get("ts") or "")

    def update_message(self, channel: str, ts: str, text: str) -> bool:
        """기존 Slack 메시지를 갱신한다."""
        if not channel or not ts:
            return False
        res = self._call_slack("chat.update", {"channel": channel, "ts": ts, "text": text})
        if not res.get("ok"):
            logger.debug("Slack update_message 실패: %s", res.get("error"))
            return False
        return True

    def post_response_url(self, response_url: str, text: str) -> bool:
        """채널 직접 전송이 불가할 때 Slash Command response_url로 폴백 전송한다."""
        if not response_url:
            return False
        res = self._post(
            response_url,
            {"Content-Type": "application/json; charset=utf-8"},
            {"response_type": "in_channel", "text": text},
        )
        return bool(res.get("ok", True))

    def report_final(
        self,
        channel: str,
        thread_ts: str,
        status_ts: str,
        report_text: str,
        max_chars: int = 3500,
        response_url: str = "",
    ) -> None:
        """최종 보고서를 Slack 길이 제한에 맞게 분할 전송(첫 청크는 status_ts 갱신)한다."""
        chunks = split_slack_message(report_text, max_chars=max_chars)
        if not channel:
            if response_url:
                self.post_response_url(response_url, chunks[0])
            return

        effective_thread = thread_ts or status_ts
        first_sent = False
        if status_ts:
            first_sent = self.update_message(channel, status_ts, chunks[0])
        if not first_sent:
            created_ts = self.post_message(channel, chunks[0], thread_ts=effective_thread)
            if not effective_thread and created_ts:
                effective_thread = created_ts
            if not created_ts and response_url:
                self.post_response_url(response_url, chunks[0])
                return

        for extra in chunks[1:]:
            self.post_message(channel, extra, thread_ts=effective_thread)
