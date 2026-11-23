"""설정 로드, 경로 보안 검증 및 Antigravity CLI 탐지 모듈."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

VALID_PROJECT_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]{1,50}$")
VALID_AI_MODES = ("dev", "plan")


@dataclass(slots=True)
class ProjectConfig:
    """등록된 개발 프로젝트 정보."""

    key: str
    path: Path
    relative_path: str
    description: str = ""


@dataclass(slots=True)
class TaskConfig:
    """실행 가능한 사전 정의 작업(기존 스케줄러 대체 프리셋)."""

    key: str
    project: str
    command: list[str]
    description: str = ""


@dataclass(slots=True)
class AIConfig:
    """Antigravity CLI AI 실행 설정."""

    default_model: str = "gemini-4-neon-high"
    default_mode: str = "dev"
    available_models: list[str] = field(
        default_factory=lambda: [
            "gemini-4-neon-high",
            "gemini-4-neon-xhigh",
            "gemini-4-neon-medium",
            "gemini-3.8-flash-high",
            "gemini-3.8-flash-medium",
            "claude-sonnet-5-5-high",
            "claude-opus-5-5-high",
        ]
    )
    timeout_sec: int = 1800
    system_prefix: str = ""


@dataclass(slots=True)
class ReportingConfig:
    """Slack 실시간 진행 보고 및 메시지 분할 설정."""

    update_interval_sec: float = 2.5
    max_message_chars: int = 3500
    upload_long_logs: bool = True


@dataclass(slots=True)
class SlackConfig:
    """Slack 연동 및 접근 제어 설정."""

    bot_token: str = ""
    app_token: str = ""
    allowed_user_ids: list[str] = field(default_factory=list)
    allow_all_users: bool = False
    allowed_channel_ids: list[str] = field(default_factory=list)
    report_channel_id: str = ""
    auto_reply_threads: bool = True


@dataclass(slots=True)
class AppConfig:
    """애플리케이션 전체 통합 설정."""

    base_dir: Path
    config_path: Path
    workspace_root: Path
    default_project: str
    agy_cli_path: str | None
    task_timeout_sec: int
    ai: AIConfig
    reporting: ReportingConfig
    slack: SlackConfig
    projects: dict[str, ProjectConfig]
    tasks: dict[str, TaskConfig]


def _parse_csv_env(value: str | None) -> list[str]:
    """쉼표로 구분된 환경변수 문자열을 정리된 리스트로 변환한다."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_bool_env(value: str | None, default: bool = False) -> bool:
    """불리언 성격의 환경변수 문자열을 안전하게 파싱한다."""
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_int_env(value: str | None, default: int) -> int:
    """정수 환경변수를 파싱하고 실패 시 기본값을 반환한다."""
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
        return parsed if parsed > 0 else default
    except ValueError:
        return default


def resolve_safe_project_path(workspace_root: Path, relative_or_name: str) -> Path:
    """작업 루트 하위의 안전한 프로젝트 경로만 허용하고 경로 탈출을 차단한다."""
    cleaned = relative_or_name.strip()
    if not cleaned:
        raise ValueError("프로젝트 경로가 비어 있습니다.")

    candidate_input = Path(cleaned)
    if candidate_input.is_absolute():
        raise ValueError(f"절대 경로는 프로젝트 경로로 허용되지 않습니다: {cleaned}")

    root_resolved = workspace_root.resolve()
    candidate = (root_resolved / candidate_input).resolve()

    if candidate == root_resolved:
        raise ValueError("작업 루트 자체를 프로젝트 경로로 지정할 수 없습니다.")
    if not candidate.is_relative_to(root_resolved):
        raise ValueError(f"작업 루트 외부로의 경로 탈출이 차단되었습니다: {cleaned}")

    return candidate


def find_agy_executable(configured_path: str = "") -> str | None:
    """설정 경로, PATH, 기본 설치 경로 순으로 agy 실행 파일을 탐지한다."""
    if configured_path and configured_path.strip():
        explicit = Path(configured_path.strip())
        if explicit.is_file():
            return str(explicit)

    for cmd_name in ("agy", "agy.exe"):
        found = shutil.which(cmd_name)
        if found:
            return found

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates: list[Path] = []
    if local_app_data:
        candidates.append(Path(local_app_data) / "agy" / "bin" / "agy.exe")
    candidates.append(Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.exe")

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    return None


def load_config(base_dir: Path | None = None) -> AppConfig:
    """`.env`와 `config.yaml`을 읽어 통합 설정 객체(`AppConfig`)를 반환한다."""
    resolved_base = (base_dir or Path(__file__).resolve().parent.parent).resolve()
    env_path = resolved_base / ".env"
    if env_path.is_file():
        load_dotenv(dotenv_path=env_path, override=False)

    config_path = resolved_base / "config.yaml"
    raw_yaml: dict[str, Any] = {}
    if config_path.is_file():
        with config_path.open("r", encoding="utf-8") as fp:
            loaded = yaml.safe_load(fp)
            if isinstance(loaded, dict):
                raw_yaml = loaded

    workspace_raw = raw_yaml.get("workspace_root") or str(resolved_base.parent)
    workspace_root = Path(workspace_raw).resolve()

    ai_raw = raw_yaml.get("ai") or {}
    default_model = (
        os.environ.get("DEFAULT_MODEL", "").strip()
        or str(ai_raw.get("default_model", "gemini-4-neon-high")).strip()
    )
    default_mode = (
        os.environ.get("DEFAULT_MODE", "").strip().lower()
        or str(ai_raw.get("default_mode", "dev")).strip().lower()
    )
    if default_mode not in VALID_AI_MODES:
        default_mode = "dev"

    available_models = [str(m).strip() for m in (ai_raw.get("available_models") or []) if str(m).strip()]
    if default_model and default_model not in available_models:
        available_models.insert(0, default_model)

    agy_timeout = _parse_int_env(
        os.environ.get("AGY_TIMEOUT_SEC"),
        int(ai_raw.get("timeout_sec", 1800)),
    )
    ai_cfg = AIConfig(
        default_model=default_model,
        default_mode=default_mode,
        available_models=available_models,
        timeout_sec=agy_timeout,
        system_prefix=str(ai_raw.get("system_prefix", "")).strip(),
    )

    rep_raw = raw_yaml.get("reporting") or {}
    reporting_cfg = ReportingConfig(
        update_interval_sec=float(rep_raw.get("update_interval_sec", 2.5)),
        max_message_chars=int(rep_raw.get("max_message_chars", 3500)),
        upload_long_logs=bool(rep_raw.get("upload_long_logs", True)),
    )

    slack_cfg = SlackConfig(
        bot_token=os.environ.get("SLACK_BOT_TOKEN", "").strip(),
        app_token=os.environ.get("SLACK_APP_TOKEN", "").strip(),
        allowed_user_ids=_parse_csv_env(os.environ.get("ALLOWED_USER_IDS")),
        allow_all_users=_parse_bool_env(os.environ.get("ALLOW_ALL_USERS"), default=False),
        allowed_channel_ids=_parse_csv_env(os.environ.get("ALLOWED_CHANNEL_IDS")),
        report_channel_id=os.environ.get("REPORT_CHANNEL_ID", "").strip(),
        auto_reply_threads=_parse_bool_env(os.environ.get("AUTO_REPLY_THREADS"), default=True),
    )

    projects: dict[str, ProjectConfig] = {}
    for key, info in (raw_yaml.get("projects") or {}).items():
        if not isinstance(info, dict):
            continue
        proj_key = str(key).strip().lower()
        rel_path = str(info.get("path", "")).strip()
        if not proj_key or not rel_path:
            continue
        proj_path = resolve_safe_project_path(workspace_root, rel_path)
        projects[proj_key] = ProjectConfig(
            key=proj_key,
            path=proj_path,
            relative_path=rel_path,
            description=str(info.get("description", "")).strip(),
        )

    tasks: dict[str, TaskConfig] = {}
    for key, info in (raw_yaml.get("tasks") or {}).items():
        if not isinstance(info, dict):
            continue
        task_key = str(key).strip().lower()
        proj_key = str(info.get("project", "")).strip().lower()
        cmd = [str(part) for part in (info.get("command") or []) if str(part)]
        if not task_key or not proj_key or not cmd:
            continue
        tasks[task_key] = TaskConfig(
            key=task_key,
            project=proj_key,
            command=cmd,
            description=str(info.get("description", "")).strip(),
        )

    default_project = (
        os.environ.get("DEFAULT_PROJECT", "").strip().lower()
        or str(raw_yaml.get("default_project", "slack_aibot")).strip().lower()
    )
    if default_project not in projects and projects:
        default_project = next(iter(projects.keys()))

    agy_cli_path = find_agy_executable(os.environ.get("AGY_CLI_PATH", ""))
    task_timeout_sec = _parse_int_env(os.environ.get("TASK_TIMEOUT_SEC"), 1800)

    return AppConfig(
        base_dir=resolved_base,
        config_path=config_path,
        workspace_root=workspace_root,
        default_project=default_project,
        agy_cli_path=agy_cli_path,
        task_timeout_sec=task_timeout_sec,
        ai=ai_cfg,
        reporting=reporting_cfg,
        slack=slack_cfg,
        projects=projects,
        tasks=tasks,
    )


def register_dynamic_project(
    config: AppConfig,
    name: str,
    description: str = "",
    persist_yaml: bool = True,
) -> ProjectConfig:
    """새 프로젝트를 작업 루트 하위에 생성하고 설정(및 config.yaml)에 등록한다."""
    raw_name = name.strip()
    if not VALID_PROJECT_KEY_RE.match(raw_name):
        raise ValueError(
            "프로젝트 이름은 영문/숫자/하이픈(-)/언더스코어(_) 조합(최대 50자)이어야 합니다."
        )

    proj_key = raw_name.lower()
    if proj_key in config.projects:
        raise ValueError(f"이미 등록된 프로젝트입니다: {proj_key}")

    proj_path = resolve_safe_project_path(config.workspace_root, raw_name)
    proj_path.mkdir(parents=True, exist_ok=True)

    proj_cfg = ProjectConfig(
        key=proj_key,
        path=proj_path,
        relative_path=raw_name,
        description=description.strip() or f"Slack에서 생성한 프로젝트 ({raw_name})",
    )
    config.projects[proj_key] = proj_cfg

    if persist_yaml and config.config_path.is_file():
        with config.config_path.open("r", encoding="utf-8") as fp:
            yaml_data = yaml.safe_load(fp) or {}
        if isinstance(yaml_data, dict):
            projects_map = yaml_data.setdefault("projects", {})
            if isinstance(projects_map, dict) and proj_key not in projects_map:
                projects_map[proj_key] = {
                    "path": proj_cfg.relative_path,
                    "description": proj_cfg.description,
                }
                with config.config_path.open("w", encoding="utf-8") as fp:
                    yaml.safe_dump(
                        yaml_data,
                        fp,
                        allow_unicode=True,
                        sort_keys=False,
                        default_flow_style=False,
                    )

    return proj_cfg
