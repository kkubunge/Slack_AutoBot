"""formatter 모듈 단위 테스트."""

from __future__ import annotations

from src.agy_runner import AgyResult
from src.formatter import (
    format_agy_report,
    format_help_message,
    format_task_report,
    markdown_to_slack_mrkdwn,
    split_slack_message,
)
from src.task_runner import TaskResult


def test_markdown_to_slack_mrkdwn_preserves_code() -> None:
    src = (
        "### 변경 사항\n"
        "**중요** 수정과 [문서](https://example.com) 및 `**인라인코드**` 유지\n"
        "```python\n"
        "# **코드블록 내부**\n"
        "print('ok')\n"
        "```"
    )
    out = markdown_to_slack_mrkdwn(src)
    assert "*변경 사항*" in out
    assert "*중요*" in out
    assert "<https://example.com|문서>" in out
    assert "`**인라인코드**`" in out
    assert "# **코드블록 내부**" in out


def test_split_slack_message_balances_code_blocks() -> None:
    code_lines = "\n".join(f"line_{i} = {i}" for i in range(120))
    text = f"앞부분 설명\n```python\n{code_lines}\n```\n뒷부분 설명"
    chunks = split_slack_message(text, max_chars=400)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 410
        assert chunk.count("```") % 2 == 0


def test_format_reports_and_help() -> None:
    agy_res = AgyResult(
        success=True,
        status="SUCCESS",
        response="**완료** 테스트",
        conversation_id="12345678-aaaa",
        duration_sec=4.5,
        usage={"total_tokens": 500},
        tool_history=["`view_file`: `README.md`"],
        log_path="logs/job_1.log",
    )
    rep = format_agy_report("job1", "slack_aibot", "gemini-4-neon-high", "dev", agy_res)
    assert "AI 작업 완료" in rep
    assert "*완료*" in rep
    assert "12345678" in rep

    task_res = TaskResult(
        success=False,
        return_code=1,
        stdout_tail=["running..."],
        stderr_tail=["boom!"],
        duration_sec=1.2,
        log_path="logs/job_2.log",
        error="종료 코드 1",
    )
    task_rep = format_task_report("job2", "ondotrip", "ondotrip", task_res)
    assert "작업 실행 실패" in task_rep
    assert "boom!" in task_rep

    help_text = format_help_message()
    assert "!실행" in help_text
    assert "!예약" in help_text
