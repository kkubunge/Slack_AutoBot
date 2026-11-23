"""agy_runner 모듈 단위 테스트 (명령 조립, NDJSON 파싱, 스트리밍 실행, 강제 중단)."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

from src.agy_runner import (
    AgyRunner,
    ProgressEvent,
    build_agy_command,
    parse_stream_line,
    summarize_tool_call,
)
from src.config import AIConfig, AppConfig, ReportingConfig, SlackConfig


def _make_test_config(base_dir: Path) -> AppConfig:
    return AppConfig(
        base_dir=base_dir,
        config_path=base_dir / "config.yaml",
        workspace_root=base_dir,
        default_project="demo",
        agy_cli_path=sys.executable,
        task_timeout_sec=30,
        ai=AIConfig(timeout_sec=30),
        reporting=ReportingConfig(),
        slack=SlackConfig(),
        projects={},
        tasks={},
    )


def test_build_agy_command_modes_and_conversation() -> None:
    cmd_dev = build_agy_command("agy.exe", "코드 수정", "gemini-4-neon-high", mode="dev")
    assert "--dangerously-skip-permissions" in cmd_dev
    assert "--mode" not in cmd_dev
    assert "auto" not in cmd_dev

    cmd_plan = build_agy_command(
        "agy.exe", "분석해줘", "gemini-3.8-flash-high", mode="plan", conversation_id="conv-123"
    )
    assert "--mode" in cmd_plan
    assert cmd_plan[cmd_plan.index("--mode") + 1] == "plan"
    assert "--dangerously-skip-permissions" in cmd_plan
    assert "auto" not in cmd_plan
    assert "--conversation" in cmd_plan
    assert cmd_plan[cmd_plan.index("--conversation") + 1] == "conv-123"


def test_parse_stream_line_and_summarize_tool() -> None:
    assert parse_stream_line("warning: something") is None
    parsed = parse_stream_line('{"event":"init","conversation_id":"eb3c0deb"}')
    assert parsed is not None
    assert parsed["conversation_id"] == "eb3c0deb"

    assert summarize_tool_call("run_command", {"CommandLine": "pytest -v"}) == "`run_command`: `pytest -v`"
    assert summarize_tool_call("write_file", {"Path": "src/main.py"}) == "`write_file`: `src/main.py`"
    assert summarize_tool_call("grep_search", {"Query": "AgyRunner"}) == "`grep_search`: `AgyRunner`"


def test_agy_runner_streaming_subprocess_and_cancel(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    runner = AgyRunner(cfg)

    # agy CLI의 stream-json 출력을 흉내 내는 모의 wrapper 스크립트 생성
    fake_cli = tmp_path / "fake_agy.py"
    fake_cli.write_text(
        """
import json, sys, time
args = sys.argv[1:]
mode = "slow" if "--conversation" in args and "slow" in args else "fast"
print(json.dumps({"event": "init", "conversation_id": "conv-mock-001"}), flush=True)
print(json.dumps({
    "event": "step_update",
    "step_update": {
        "step_index": 2,
        "state": "ACTIVE",
        "step_type": "tool",
        "tool_name": "view_file",
        "tool_info": {"parameters": {"Path": "README.md"}}
    }
}), flush=True)
if mode == "slow":
    time.sleep(10)
print(json.dumps({
    "event": "step_update",
    "step_update": {"step_index": 3, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "완료"}
}), flush=True)
print(json.dumps({
    "event": "result",
    "result": {
        "conversation_id": "conv-mock-001",
        "status": "SUCCESS",
        "response": "모의 실행이 완료되었습니다.",
        "duration_seconds": 0.25,
        "num_turns": 1,
        "usage": {"total_tokens": 1234}
    }
}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    events: list[ProgressEvent] = []
    # Python 스크립트를 실행하도록 runner.run의 cmd 형태와 맞추기 위해 래퍼 .cmd/.py 또는 monkeypatch 사용
    import src.agy_runner as agy_mod

    orig_build = agy_mod.build_agy_command
    try:
        agy_mod.build_agy_command = (
            lambda agy_path, prompt, model, mode="dev", conversation_id=None: [
                sys.executable,
                str(fake_cli),
                *(["--conversation", conversation_id] if conversation_id else []),
            ]
        )
        res = runner.run(
            job_id="mock1",
            prompt="테스트",
            cwd=tmp_path,
            model="gemini-4-neon-high",
            mode="dev",
            on_progress=events.append,
        )
        assert res.success is True
        assert res.conversation_id == "conv-mock-001"
        assert res.response == "모의 실행이 완료되었습니다."
        assert res.usage["total_tokens"] == 1234
        assert any(e.kind == "tool_start" for e in events)
        assert Path(res.log_path).is_file()

        # 취소 테스트
        cancel_evt = threading.Event()
        timer = threading.Timer(0.4, cancel_evt.set)
        timer.start()
        res_cancel = runner.run(
            job_id="mock2",
            prompt="느린 작업",
            cwd=tmp_path,
            model="gemini-4-neon-high",
            mode="dev",
            conversation_id="slow",
            cancel_event=cancel_evt,
        )
        timer.cancel()
        assert res_cancel.success is False
        assert res_cancel.cancelled is True
        assert res_cancel.status == "CANCELLED"
    finally:
        agy_mod.build_agy_command = orig_build


def test_agy_runner_treats_empty_response_with_denied_actions_as_failure(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    runner = AgyRunner(cfg)

    denied_cli = tmp_path / "denied_agy.py"
    denied_cli.write_text(
        """
import json
print(json.dumps({"event": "init", "conversation_id": "conv-denied-1"}), flush=True)
print(json.dumps({
    "event": "step_update",
    "step_update": {
        "step_index": 2,
        "state": "ERROR",
        "step_type": "tool",
        "tool_name": "run_command",
        "tool_info": {"parameters": {"CommandLine": "dir"}}
    }
}), flush=True)
print(json.dumps({
    "event": "result",
    "result": {
        "conversation_id": "conv-denied-1",
        "status": "SUCCESS",
        "response": "",
        "denied_actions": [{"action": "command", "display_name": "Bash"}]
    }
}), flush=True)
""".strip(),
        encoding="utf-8",
    )

    events: list[ProgressEvent] = []
    import src.agy_runner as agy_mod

    orig_build = agy_mod.build_agy_command
    try:
        agy_mod.build_agy_command = lambda *args, **kwargs: [sys.executable, str(denied_cli)]
        res = runner.run(
            job_id="denied1",
            prompt="테스트",
            cwd=tmp_path,
            model="gemini-4-neon-high",
            mode="plan",
            on_progress=events.append,
        )
        assert res.success is False
        assert "헤드리스 권한 거부" in res.error
        assert "Bash" in res.error
        assert any(e.kind == "tool_error" for e in events)
    finally:
        agy_mod.build_agy_command = orig_build
