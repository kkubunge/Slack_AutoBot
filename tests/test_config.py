"""config 모듈 단위 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.config import (
    find_agy_executable,
    load_config,
    register_dynamic_project,
    resolve_safe_project_path,
)


def test_resolve_safe_project_path_valid(tmp_path: Path) -> None:
    target = resolve_safe_project_path(tmp_path, "MyProject")
    assert target == (tmp_path / "MyProject").resolve()


@pytest.mark.parametrize("bad_path", ["", "../outside", "..\\outside", ".", "C:\\Windows"])
def test_resolve_safe_project_path_blocks_traversal_and_absolute(
    tmp_path: Path, bad_path: str
) -> None:
    with pytest.raises(ValueError):
        resolve_safe_project_path(tmp_path, bad_path)


def test_load_config_from_project_root() -> None:
    base_dir = Path(__file__).resolve().parent.parent
    cfg = load_config(base_dir)
    assert "slack_autobot" in cfg.projects
    assert cfg.default_project == "slack_autobot"
    assert "ondotrip" in cfg.projects
    assert "ondotrip-status" in cfg.tasks
    assert cfg.ai.default_mode in {"dev", "plan"}


def test_find_agy_executable_explicit(tmp_path: Path) -> None:
    fake_agy = tmp_path / "agy.exe"
    fake_agy.write_text("dummy", encoding="utf-8")
    assert find_agy_executable(str(fake_agy)) == str(fake_agy)


def test_register_dynamic_project(tmp_path: Path) -> None:
    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    bot_dir = ws_root / "Slack_AutoBot"
    bot_dir.mkdir()

    cfg_yaml = bot_dir / "config.yaml"
    cfg_yaml.write_text(
        yaml.safe_dump(
            {
                "workspace_root": str(ws_root),
                "default_project": "slack_autobot",
                "projects": {
                    "slack_autobot": {"path": "Slack_AutoBot", "description": "기본 프로젝트"}
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    cfg = load_config(bot_dir)
    created = register_dynamic_project(cfg, "NewBot", "새 자동화 봇", persist_yaml=True)
    assert created.key == "newbot"
    assert created.path.is_dir()
    assert "newbot" in cfg.projects

    reloaded = yaml.safe_load(cfg_yaml.read_text(encoding="utf-8"))
    assert "newbot" in reloaded["projects"]

    with pytest.raises(ValueError):
        register_dynamic_project(cfg, "../bad", "탈출 시도")
