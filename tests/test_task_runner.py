"""task_runner 모듈 단위 테스트."""

from __future__ import annotations

import sys
from pathlib import Path

from src.config import AIConfig, AppConfig, ReportingConfig, SlackConfig, TaskConfig
from src.task_runner import TaskRunner


def test_task_runner_resolves_command_and_executes(tmp_path: Path) -> None:
    cfg = AppConfig(
        base_dir=tmp_path,
        config_path=tmp_path / "config.yaml",
        workspace_root=tmp_path,
        default_project="slack_autobot",
        agy_cli_path=None,
        task_timeout_sec=30,
        ai=AIConfig(),
        reporting=ReportingConfig(),
        slack=SlackConfig(),
        projects={},
        tasks={},
    )
    runner = TaskRunner(cfg)
    task = TaskConfig(
        key="echo-test",
        project="slack_autobot",
        command=[sys.executable, "-c", "print('task-ok')"],
        description="테스트 작업",
    )
    res = runner.run(job_id="t1", task=task, cwd=tmp_path, extra_args="--dummy")
    assert res.success is True
    assert res.return_code == 0
    assert "task-ok" in "\n".join(res.stdout_tail)
    assert Path(res.log_path).is_file()
