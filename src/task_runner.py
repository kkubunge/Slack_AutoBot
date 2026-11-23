"""사전 등록된 로컬 프로젝트 작업 실행기 및 Windows 작업 스케줄러 조회 유틸리티."""

from __future__ import annotations

import csv
import io
import os
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from src.agy_runner import terminate_process_tree
from src.config import AppConfig, TaskConfig


@dataclass(slots=True)
class TaskResult:
    """로컬 작업 실행 결과."""

    success: bool
    return_code: int
    stdout_tail: list[str] = field(default_factory=list)
    stderr_tail: list[str] = field(default_factory=list)
    duration_sec: float = 0.0
    log_path: str = ""
    command_display: str = ""
    error: str = ""
    cancelled: bool = False


class TaskRunner:
    """등록된 프로젝트 스크립트/명령어를 실행하고 로그를 수집하는 러너."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._active_procs: dict[str, subprocess.Popen[str]] = {}
        self._cancel_flags: set[str] = set()

    def cancel(self, job_id: str) -> bool:
        """실행 중인 작업 프로세스 트리를 중단한다."""
        with self._lock:
            self._cancel_flags.add(job_id)
            proc = self._active_procs.get(job_id)
        if proc and proc.poll() is None:
            terminate_process_tree(proc.pid)
            return True
        return False

    @staticmethod
    def _resolve_command(command: list[str], cwd: Path, extra_args: str = "") -> list[str]:
        """작업 폴더 기준으로 실행 파일 경로를 정규화하고 추가 인자를 결합한다."""
        cmd = list(command)
        if cmd:
            first = cmd[0]
            if ("\\" in first or "/" in first) and not Path(first).is_absolute():
                candidate = (cwd / first).resolve()
                if candidate.exists():
                    cmd[0] = str(candidate)
        if extra_args.strip():
            # Windows 경로 백슬래시가 사라지지 않도록 posix=False 대신 인자 분해 후 따옴표 정리
            parsed = shlex.split(extra_args.strip(), posix=(os.name != "nt"))
            cleaned = [arg.strip('"').strip("'") if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in ('"', "'") else arg for arg in parsed]
            cmd.extend(cleaned)
        return cmd

    def run(
        self,
        job_id: str,
        task: TaskConfig,
        cwd: Path,
        extra_args: str = "",
        timeout_sec: int | None = None,
        on_progress: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> TaskResult:
        """지정된 프리셋 작업을 실행하고 실시간 출력과 로그를 반환한다."""
        if not cwd.exists():
            return TaskResult(
                success=False,
                return_code=-1,
                error=f"작업 경로가 존재하지 않습니다: {cwd}",
            )

        effective_timeout = timeout_sec or self.config.task_timeout_sec
        cmd = self._resolve_command(task.command, cwd, extra_args=extra_args)
        cmd_display = " ".join(cmd)

        logs_dir = self.config.base_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = logs_dir / f"job_{job_id}.log"

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        start_monotonic = time.monotonic()
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
            return TaskResult(
                success=False,
                return_code=-1,
                command_display=cmd_display,
                log_path=str(log_file),
                error=f"작업 프로세스 실행 실패: {exc}",
            )

        with self._lock:
            self._active_procs[job_id] = proc

        stop_watchdog = threading.Event()

        def _drain_stderr() -> None:
            if not proc.stderr:
                return
            try:
                for line in proc.stderr:
                    stderr_lines.append(line.rstrip("\r\n"))
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
                log_fp.write(f"[TASK] {task.key}\n[CWD] {cwd}\n[CMD] {cmd_display}\n\n")
                if proc.stdout:
                    for raw_line in proc.stdout:
                        log_fp.write(raw_line)
                        log_fp.flush()
                        cleaned = raw_line.rstrip("\r\n")
                        if cleaned:
                            stdout_lines.append(cleaned)
                            if on_progress:
                                on_progress(cleaned)
                proc.wait(timeout=15)
                stderr_thread.join(timeout=2)
                if stderr_lines:
                    log_fp.write("\n[STDERR]\n" + "\n".join(stderr_lines) + "\n")
        except Exception as exc:
            terminate_process_tree(proc.pid)
            return TaskResult(
                success=False,
                return_code=proc.returncode or -1,
                stdout_tail=stdout_lines[-20:],
                stderr_tail=stderr_lines[-20:],
                duration_sec=round(time.monotonic() - start_monotonic, 2),
                log_path=str(log_file),
                command_display=cmd_display,
                error=f"작업 실행 중 예외 발생: {exc}",
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
        rc = proc.returncode if proc.returncode is not None else -1

        if cancelled:
            return TaskResult(
                success=False,
                return_code=rc,
                stdout_tail=stdout_lines[-20:],
                stderr_tail=stderr_lines[-20:],
                duration_sec=elapsed,
                log_path=str(log_file),
                command_display=cmd_display,
                error="사용자 요청으로 작업이 중단되었습니다.",
                cancelled=True,
            )

        if timed_out:
            return TaskResult(
                success=False,
                return_code=rc,
                stdout_tail=stdout_lines[-20:],
                stderr_tail=stderr_lines[-20:],
                duration_sec=elapsed,
                log_path=str(log_file),
                command_display=cmd_display,
                error=f"실행 제한 시간({effective_timeout}초)을 초과했습니다.",
            )

        return TaskResult(
            success=(rc == 0),
            return_code=rc,
            stdout_tail=stdout_lines[-20:],
            stderr_tail=stderr_lines[-20:],
            duration_sec=elapsed,
            log_path=str(log_file),
            command_display=cmd_display,
            error="" if rc == 0 else f"종료 코드 {rc}",
        )


def _decode_windows_output(raw: bytes) -> str:
    """Windows 콘솔 명령 바이트 출력을 UTF-8 우선, 실패 시 CP949로 안전하게 디코딩한다."""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp949", errors="replace")


def query_windows_schedulers(keywords: tuple[str, ...] = ("OndoTrip", "NaverMap", "TrailLog", "GoogleAI", "Slack_AIbot", "Slack")) -> str:
    """기존 Windows 작업 스케줄러 등록 상태를 읽기 전용으로 조회해 한국어로 요약한다."""
    if os.name != "nt":
        return "Windows 환경이 아니어서 작업 스케줄러를 조회할 수 없습니다."

    try:
        proc = subprocess.run(
            ["schtasks", "/Query", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=False,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return f"Windows 작업 스케줄러 조회 실패: {exc}"

    if proc.returncode != 0:
        return f"Windows 작업 스케줄러 조회 오류 (코드 {proc.returncode})"

    stdout_text = _decode_windows_output(proc.stdout)
    matched: list[tuple[str, str, str]] = []
    reader = csv.reader(io.StringIO(stdout_text))
    lowered_kw = tuple(k.lower() for k in keywords)
    for row in reader:
        if len(row) < 3:
            continue
        task_name, next_run, status = row[0].strip(), row[1].strip(), row[2].strip()
        if any(k in task_name.lower() for k in lowered_kw):
            matched.append((task_name, next_run, status))

    if not matched:
        return "GoogleAI_PJT 관련으로 등록된 Windows 작업 스케줄러 항목이 없습니다."

    lines = ["*기존 Windows 작업 스케줄러 현황 (읽기 전용 조회)*"]
    for task_name, next_run, status in matched:
        lines.append(f"• `{task_name}` — 상태: `{status}` | 다음 실행: {next_run}")
    lines.append("_참고: 기존 스케줄러는 안전을 위해 자동 삭제하지 않았습니다. 비활성화하려면 각 프로젝트의 `unregister_scheduler.bat` 또는 `schtasks /Change /TN \"작업명\" /DISABLE`을 사용하세요._")
    return "\n".join(lines)
