"""Antigravity CLI (`agy.exe`) 비대화형 스트리밍 실행 및 실시간 이벤트 파서."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.config import AppConfig


@dataclass(slots=True)
class ProgressEvent:
    """실행 중 실시간 진행 상황을 전달하는 이벤트."""

    kind: str  # "init" | "tool_start" | "tool_done" | "response_start" | "info"
    message: str
    tool_name: str = ""
    step_index: int = 0


@dataclass(slots=True)
class AgyResult:
    """Antigravity CLI 실행 최종 결과."""

    success: bool
    status: str
    response: str
    conversation_id: str = ""
    duration_sec: float = 0.0
    num_turns: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    tool_history: list[str] = field(default_factory=list)
    error: str = ""
    return_code: int = 0
    log_path: str = ""
    cancelled: bool = False


def build_agy_command(
    agy_path: str,
    prompt: str,
    model: str,
    mode: str = "dev",
    conversation_id: str | None = None,
) -> list[str]:
    """Windows 환경에 안전한 Antigravity CLI 인자 리스트를 생성한다.

    주의:
    - Windows 호스트에서는 터미널 샌드박스 미지원으로 `--mode auto` 사용 시 오류가 발생하므로 절대 사용하지 않는다.
    - `plan` 모드에서도 헤드리스(`--print`) 실행 중 읽기/조회 도구 호출이 `request-review`에 의해
      `denied_actions`로 차단되지 않도록 `--mode plan --dangerously-skip-permissions`를 함께 사용한다.
    """
    cmd = [
        agy_path,
        "--print",
        prompt,
        "--output-format",
        "stream-json",
        "--model",
        model,
    ]

    normalized_mode = (mode or "dev").strip().lower()
    if normalized_mode == "plan":
        cmd.extend(["--mode", "plan", "--dangerously-skip-permissions"])
    else:
        cmd.append("--dangerously-skip-permissions")

    if conversation_id and conversation_id.strip():
        cmd.extend(["--conversation", conversation_id.strip()])

    return cmd


def parse_stream_line(line: str) -> dict[str, Any] | None:
    """stream-json의 NDJSON 한 줄을 안전하게 파싱한다. JSON이 아니면 None을 반환한다."""
    stripped = line.strip()
    if not stripped or not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _truncate(text: str, max_len: int = 80) -> str:
    """긴 문자열을 한 줄 요약 길이로 자른다."""
    one_line = " ".join(text.split())
    if len(one_line) <= max_len:
        return one_line
    return one_line[: max_len - 3] + "..."


def summarize_tool_call(tool_name: str, parameters: dict[str, Any] | None) -> str:
    """도구 이름과 파라미터를 사람이 읽기 쉬운 한국어 1줄 요약으로 변환한다."""
    params = parameters or {}
    name = tool_name or "unknown_tool"

    if name == "run_command":
        cmd = str(params.get("CommandLine") or params.get("command") or "")
        return f"`run_command`: `{_truncate(cmd, 75)}`" if cmd else "`run_command`"

    if name in {
        "view_file",
        "read_file",
        "write_to_file",
        "write_file",
        "edit_file",
        "replace_file_content",
        "multi_replace_file_content",
        "sed_file",
    }:
        target = str(params.get("Path") or params.get("TargetFile") or params.get("AbsolutePath") or "")
        if target:
            return f"`{name}`: `{_truncate(target, 75)}`"
        return f"`{name}`"

    if name == "grep_search":
        query = str(params.get("Query") or "")
        return f"`grep_search`: `{_truncate(query, 65)}`" if query else "`grep_search`"

    if name in {"find_by_name", "list_dir"}:
        target = str(
            params.get("Pattern")
            or params.get("SearchDirectory")
            or params.get("DirectoryPath")
            or ""
        )
        return f"`{name}`: `{_truncate(target, 70)}`" if target else f"`{name}`"

    if name == "search_web":
        query = str(params.get("query") or "")
        return f"`search_web`: `{_truncate(query, 65)}`" if query else "`search_web`"

    return f"`{name}`"


def terminate_process_tree(pid: int) -> None:
    """Windows에서 하위 프로세스까지 포함해 프로세스 트리를 강제 종료한다."""
    if pid <= 0:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception:
            pass
    else:
        try:
            os.kill(pid, 9)
        except Exception:
            pass


class AgyRunner:
    """Antigravity CLI 서브프로세스를 실행하고 스트리밍 이벤트를 수집하는 러너."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._active_procs: dict[str, subprocess.Popen[str]] = {}
        self._cancel_flags: set[str] = set()

    def cancel(self, job_id: str) -> bool:
        """실행 중인 작업 ID에 해당하는 agy 프로세스 트리를 종료한다."""
        with self._lock:
            self._cancel_flags.add(job_id)
            proc = self._active_procs.get(job_id)
        if proc and proc.poll() is None:
            terminate_process_tree(proc.pid)
            return True
        return False

    def run(
        self,
        job_id: str,
        prompt: str,
        cwd: Path,
        model: str,
        mode: str = "dev",
        conversation_id: str | None = None,
        timeout_sec: int | None = None,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        cancel_event: threading.Event | None = None,
        executable_override: str | None = None,
    ) -> AgyResult:
        """agy CLI를 비대화형 stream-json 모드로 실행하고 결과를 반환한다."""
        agy_path = executable_override or self.config.agy_cli_path
        if not agy_path:
            return AgyResult(
                success=False,
                status="ERROR",
                response="",
                error="Antigravity CLI(agy.exe)를 찾을 수 없습니다. `.env`의 `AGY_CLI_PATH`를 확인해 주세요.",
            )

        effective_timeout = timeout_sec or self.config.ai.timeout_sec
        logs_dir = self.config.base_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = logs_dir / f"job_{job_id}.log"

        cmd = build_agy_command(
            agy_path=agy_path,
            prompt=prompt,
            model=model,
            mode=mode,
            conversation_id=conversation_id,
        )

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        start_monotonic = time.monotonic()
        stderr_lines: list[str] = []
        raw_non_json_lines: list[str] = []
        text_deltas: list[str] = []
        tool_history: list[str] = []
        seen_tool_steps: set[int] = set()
        response_started = False

        detected_conv_id = conversation_id or ""
        result_payload: dict[str, Any] | None = None
        timed_out = False
        cancelled = False

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                shell=False,
            )
        except Exception as exc:
            return AgyResult(
                success=False,
                status="ERROR",
                response="",
                error=f"agy CLI 실행 시작 실패: {exc}",
                log_path=str(log_file),
            )

        with self._lock:
            self._active_procs[job_id] = proc

        stop_watchdog = threading.Event()

        def _drain_stderr() -> None:
            if not proc.stderr:
                return
            try:
                for err_line in proc.stderr:
                    stderr_lines.append(err_line.rstrip("\r\n"))
            except Exception:
                pass

        def _watchdog() -> None:
            nonlocal timed_out, cancelled
            deadline = start_monotonic + effective_timeout
            while not stop_watchdog.is_set() and proc.poll() is None:
                if cancel_event and cancel_event.is_set():
                    cancelled = True
                    terminate_process_tree(proc.pid)
                    break
                with self._lock:
                    if job_id in self._cancel_flags:
                        cancelled = True
                        terminate_process_tree(proc.pid)
                        break
                if time.monotonic() >= deadline:
                    timed_out = True
                    terminate_process_tree(proc.pid)
                    break
                stop_watchdog.wait(0.25)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
        stderr_thread.start()
        watchdog_thread.start()

        try:
            with log_file.open("w", encoding="utf-8") as log_fp:
                log_fp.write(f"[JOB] {job_id}\n[CWD] {cwd}\n[CMD] {' '.join(cmd)}\n\n")
                if proc.stdout:
                    for raw_line in proc.stdout:
                        log_fp.write(raw_line)
                        log_fp.flush()

                        event = parse_stream_line(raw_line)
                        if not event:
                            stripped = raw_line.strip()
                            if stripped:
                                raw_non_json_lines.append(stripped)
                            continue

                        evt_type = str(event.get("event", ""))
                        if evt_type == "init":
                            conv = str(event.get("conversation_id") or "").strip()
                            if conv:
                                detected_conv_id = conv
                            if on_progress:
                                on_progress(
                                    ProgressEvent(
                                        kind="init",
                                        message=f"세션 초기화 완료 (`{detected_conv_id[:8]}`)" if detected_conv_id else "세션 초기화 완료",
                                    )
                                )
                        elif evt_type == "step_update":
                            su = event.get("step_update") or {}
                            step_index = int(su.get("step_index") or 0)
                            state = str(su.get("state") or "")
                            step_type = str(su.get("step_type") or "")

                            if step_type == "tool":
                                tool_name = str(su.get("tool_name") or "")
                                tool_info = su.get("tool_info") or {}
                                params = tool_info.get("parameters") if isinstance(tool_info, dict) else {}
                                summary = summarize_tool_call(tool_name, params)
                                if state == "ACTIVE" and step_index not in seen_tool_steps:
                                    seen_tool_steps.add(step_index)
                                    tool_history.append(summary)
                                    if on_progress:
                                        on_progress(
                                            ProgressEvent(
                                                kind="tool_start",
                                                message=f"도구 실행: {summary}",
                                                tool_name=tool_name,
                                                step_index=step_index,
                                            )
                                        )
                                elif state == "DONE":
                                    if step_index not in seen_tool_steps:
                                        seen_tool_steps.add(step_index)
                                        tool_history.append(summary)
                                    if on_progress:
                                        on_progress(
                                            ProgressEvent(
                                                kind="tool_done",
                                                message=f"도구 완료: {summary}",
                                                tool_name=tool_name,
                                                step_index=step_index,
                                            )
                                        )
                                elif state == "ERROR":
                                    if step_index not in seen_tool_steps:
                                        seen_tool_steps.add(step_index)
                                        tool_history.append(f"{summary} (오류)")
                                    if on_progress:
                                        on_progress(
                                            ProgressEvent(
                                                kind="tool_error",
                                                message=f"도구 오류: {summary}",
                                                tool_name=tool_name,
                                                step_index=step_index,
                                            )
                                        )
                            elif step_type == "agent_response":
                                delta = su.get("text_delta")
                                if isinstance(delta, str) and delta:
                                    text_deltas.append(delta)
                                    if not response_started:
                                        response_started = True
                                        if on_progress:
                                            on_progress(
                                                ProgressEvent(
                                                    kind="response_start",
                                                    message="최종 응답 작성 중...",
                                                    step_index=step_index,
                                                )
                                            )
                        elif evt_type == "result":
                            res = event.get("result")
                            if isinstance(res, dict):
                                result_payload = res
                                conv = str(res.get("conversation_id") or "").strip()
                                if conv:
                                    detected_conv_id = conv

                proc.wait(timeout=15)
                stderr_thread.join(timeout=2)
                if stderr_lines:
                    log_fp.write("\n[STDERR]\n" + "\n".join(stderr_lines) + "\n")
        except Exception as exc:
            terminate_process_tree(proc.pid)
            return AgyResult(
                success=False,
                status="ERROR",
                response="".join(text_deltas).strip(),
                conversation_id=detected_conv_id,
                duration_sec=round(time.monotonic() - start_monotonic, 2),
                tool_history=tool_history,
                error=f"agy CLI 실행 중 예외 발생: {exc}",
                return_code=proc.returncode or -1,
                log_path=str(log_file),
            )
        finally:
            stop_watchdog.set()
            watchdog_thread.join(timeout=2)
            with self._lock:
                self._active_procs.pop(job_id, None)
                if job_id in self._cancel_flags:
                    cancelled = True
                    self._cancel_flags.discard(job_id)

        elapsed = round(time.monotonic() - start_monotonic, 2)
        return_code = proc.returncode if proc.returncode is not None else -1

        if cancelled:
            return AgyResult(
                success=False,
                status="CANCELLED",
                response="".join(text_deltas).strip(),
                conversation_id=detected_conv_id,
                duration_sec=elapsed,
                tool_history=tool_history,
                error="사용자 요청으로 작업이 중단되었습니다.",
                return_code=return_code,
                log_path=str(log_file),
                cancelled=True,
            )

        if timed_out:
            return AgyResult(
                success=False,
                status="TIMEOUT",
                response="".join(text_deltas).strip(),
                conversation_id=detected_conv_id,
                duration_sec=elapsed,
                tool_history=tool_history,
                error=f"실행 제한 시간({effective_timeout}초)을 초과하여 중단되었습니다.",
                return_code=return_code,
                log_path=str(log_file),
            )

        if result_payload is not None:
            status_str = str(result_payload.get("status") or "UNKNOWN").upper()
            final_text = str(result_payload.get("response") or "".join(text_deltas)).strip()
            duration_val = float(result_payload.get("duration_seconds") or elapsed)
            num_turns = int(result_payload.get("num_turns") or 0)
            usage_raw = result_payload.get("usage") or {}
            usage = {
                str(k): int(v)
                for k, v in usage_raw.items()
                if isinstance(v, (int, float))
            }
            is_ok = status_str == "SUCCESS" and return_code == 0
            denied_actions = result_payload.get("denied_actions") or []
            err_msg = ""
            if denied_actions and not final_text:
                denied_names = ", ".join(
                    str(item.get("display_name") or item.get("action") or "tool")
                    for item in denied_actions
                    if isinstance(item, dict)
                ) or "tool"
                is_ok = False
                err_detail = "\n".join(stderr_lines[-10:]).strip()
                err_msg = (
                    f"헤드리스 권한 거부로 응답이 생성되지 않았습니다 ({denied_names})."
                    + (f"\n{err_detail}" if err_detail else "")
                )
            elif not is_ok:
                err_detail = "\n".join(stderr_lines[-10:]).strip()
                err_msg = err_detail or f"agy CLI 종료 상태={status_str}, 코드={return_code}"
            return AgyResult(
                success=is_ok,
                status=status_str,
                response=final_text,
                conversation_id=detected_conv_id,
                duration_sec=round(duration_val, 2),
                num_turns=num_turns,
                usage=usage,
                tool_history=tool_history,
                error=err_msg,
                return_code=return_code,
                log_path=str(log_file),
            )

        # result 이벤트가 없었을 때의 폴백 처리
        fallback_text = "".join(text_deltas).strip() or "\n".join(raw_non_json_lines[-20:]).strip()
        if return_code == 0 and fallback_text:
            return AgyResult(
                success=True,
                status="SUCCESS",
                response=fallback_text,
                conversation_id=detected_conv_id,
                duration_sec=elapsed,
                tool_history=tool_history,
                return_code=return_code,
                log_path=str(log_file),
            )

        err_tail = "\n".join(stderr_lines[-15:] or raw_non_json_lines[-15:]).strip()
        return AgyResult(
            success=False,
            status="FAILED",
            response=fallback_text,
            conversation_id=detected_conv_id,
            duration_sec=elapsed,
            tool_history=tool_history,
            error=err_tail or f"agy CLI가 비정상 종료되었습니다 (코드={return_code}).",
            return_code=return_code,
            log_path=str(log_file),
        )
