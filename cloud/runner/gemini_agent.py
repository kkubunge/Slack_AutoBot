"""Gemini REST API Function Calling 기반 클라우드 자율 코딩 에이전트."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from cloud.runner.tools import RepoToolchain

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentRunResult:
    """Gemini 클라우드 에이전트 실행 최종 결과."""

    success: bool
    response: str
    model_used: str = ""
    turns: int = 0
    tool_history: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    error: str = ""


def _default_gemini_transport(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any] | str]:
    """urllib을 이용해 Gemini REST API에 POST 요청을 보낸다."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        err_raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(err_raw)
        except json.JSONDecodeError:
            return exc.code, err_raw
    except Exception as exc:
        return 0, str(exc)


class GeminiCodingAgent:
    """도구 호출 루프를 수행하는 무인 Gemini 개발·분석 에이전트."""

    def __init__(
        self,
        api_key: str,
        default_model: str = "gemini-2.5-pro",
        fallback_models: list[str] | None = None,
        max_turns: int = 15,
        transport: Callable[[str, dict[str, str], dict[str, Any]], tuple[int, dict[str, Any] | str]]
        | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.models = list(
            dict.fromkeys(
                [m for m in [default_model, *(fallback_models or [])] if m]
            )
        ) or ["gemini-2.5-pro", "gemini-2.5-flash"]
        self.max_turns = max(1, max_turns)
        self._transport = transport or _default_gemini_transport

    def _call_with_fallback(
        self,
        contents: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system_instruction: str,
        start_model_idx: int = 0,
    ) -> tuple[int, dict[str, Any] | None, str]:
        """모델 폴백 체인을 따라 generateContent를 호출한다."""
        payload = {
            "systemInstruction": {"parts": [{"text": system_instruction}]},
            "contents": contents,
            "tools": tools,
            "generationConfig": {"temperature": 0.2},
        }
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }

        last_err = ""
        for idx in range(start_model_idx, len(self.models)):
            model = self.models[idx]
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            status, body = self._transport(url, headers, payload)
            if status == 200 and isinstance(body, dict):
                return idx, body, ""

            err_detail = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)[:300]
            last_err = f"모델 `{model}` HTTP {status}: {err_detail}"
            logger.warning("Gemini API 호출 실패 (%s) -> 폴백 시도", last_err)

            # 인증/잘못된 요청(400/401/403) 중 모델 미존재가 아닌 치명 오류는 폴백해도 동일할 수 있으나,
            # 일부 키 권한/모델 미지원(404/429/500/503/0)은 다음 모델로 재시도
            if status in {401, 403}:
                return idx, None, last_err

        return start_model_idx, None, last_err or "사용 가능한 Gemini 모델 호출에 실패했습니다."

    def run(
        self,
        prompt: str,
        toolchain: RepoToolchain,
        mode: str = "dev",
        on_progress: Callable[[str], None] | None = None,
    ) -> AgentRunResult:
        """지정된 저장소 툴체인을 사용해 멀티턴 AI 개발 또는 분석을 수행한다."""
        if not self.api_key:
            return AgentRunResult(
                success=False,
                response="",
                error="GEMINI_API_KEY가 GitHub Secrets에 설정되지 않았습니다.",
            )

        mode_instruction = (
            "당신은 클라우드 개발 에이전트입니다. 대상 저장소의 코드를 분석·수정하고 가능한 경우 테스트 명령(`run_command`)으로 검증한 뒤, "
            "변경한 파일과 검증 결과를 한국어로 간결하게 요약 보고하세요."
            if mode == "dev"
            else "당신은 읽기 전용 코드 분석·설계 에이전트입니다. 파일을 수정하지 말고 `list_files`, `read_file`, `search_code` 도구만 사용해 조사한 결과와 구현 계획을 한국어로 보고하세요."
        )

        tools = toolchain.get_gemini_tool_declarations()
        contents: list[dict[str, Any]] = [
            {"role": "user", "parts": [{"text": prompt}]}
        ]
        model_idx = 0

        for turn in range(1, self.max_turns + 1):
            model_idx, resp_data, err = self._call_with_fallback(
                contents=contents,
                tools=tools,
                system_instruction=mode_instruction,
                start_model_idx=model_idx,
            )
            if resp_data is None:
                return AgentRunResult(
                    success=False,
                    response="",
                    model_used=self.models[model_idx],
                    turns=turn,
                    tool_history=list(toolchain.tool_history),
                    modified_files=sorted(toolchain.modified_files),
                    error=err,
                )

            candidates = resp_data.get("candidates") or []
            if not candidates:
                return AgentRunResult(
                    success=False,
                    response="",
                    model_used=self.models[model_idx],
                    turns=turn,
                    tool_history=list(toolchain.tool_history),
                    modified_files=sorted(toolchain.modified_files),
                    error="Gemini API 응답에 candidate가 비어 있습니다.",
                )

            model_content = candidates[0].get("content") or {}
            parts = model_content.get("parts") or []
            contents.append({"role": "model", "parts": parts})

            func_calls = [p["functionCall"] for p in parts if isinstance(p, dict) and "functionCall" in p]
            if not func_calls:
                text_parts = [str(p.get("text", "")) for p in parts if isinstance(p, dict) and p.get("text")]
                final_text = "\n".join(text_parts).strip()
                return AgentRunResult(
                    success=True,
                    response=final_text or "(응답 텍스트 없음)",
                    model_used=self.models[model_idx],
                    turns=turn,
                    tool_history=list(toolchain.tool_history),
                    modified_files=sorted(toolchain.modified_files),
                )

            response_parts: list[dict[str, Any]] = []
            for fc in func_calls:
                fn_name = str(fc.get("name") or "")
                fn_args = fc.get("args") if isinstance(fc.get("args"), dict) else {}
                if on_progress:
                    on_progress(f"[턴 {turn}] 도구 실행: `{fn_name}`")
                tool_res = toolchain.execute(fn_name, fn_args)
                response_parts.append(
                    {
                        "functionResponse": {
                            "name": fn_name,
                            "response": {"result": tool_res},
                        }
                    }
                )

            contents.append({"role": "user", "parts": response_parts})

        return AgentRunResult(
            success=False,
            response="",
            model_used=self.models[model_idx],
            turns=self.max_turns,
            tool_history=list(toolchain.tool_history),
            modified_files=sorted(toolchain.modified_files),
            error=f"최대 에이전트 턴 수({self.max_turns}턴)에 도달해 작업을 종료했습니다.",
        )
