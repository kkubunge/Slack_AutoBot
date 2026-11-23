"""클라우드 GitHub 저장소 및 원격 워크플로우 설정 로드 모듈."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class CloudRepoConfig:
    """클라우드에서 AI 개발 대상으로 삼을 GitHub 저장소 정보."""

    key: str
    repo: str
    default_branch: str = "main"
    description: str = ""


@dataclass(slots=True)
class CloudWorkflowConfig:
    """Slack에서 원격 트리거할 GitHub Actions 워크플로우 프리셋."""

    key: str
    repo: str
    workflow: str
    ref: str = "main"
    inputs: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass(slots=True)
class CloudGeminiConfig:
    """클라우드 Gemini 에이전트 모델 및 턴 제한 설정."""

    default_model: str = "gemini-2.5-pro"
    fallback_models: list[str] = field(
        default_factory=lambda: [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-3.5-flash",
            "gemini-flash-latest",
        ]
    )
    max_turns: int = 15


@dataclass(slots=True)
class CloudAppConfig:
    """클라우드 러너 통합 설정."""

    default_repo: str
    gemini: CloudGeminiConfig
    repos: dict[str, CloudRepoConfig]
    workflows: dict[str, CloudWorkflowConfig]


def load_cloud_config(config_path: Path | None = None) -> CloudAppConfig:
    """`cloud/cloud_repos.yaml`과 환경변수를 읽어 클라우드 설정을 반환한다."""
    resolved_path = config_path or (Path(__file__).resolve().parent.parent / "cloud_repos.yaml")
    raw: dict[str, Any] = {}
    if resolved_path.is_file():
        with resolved_path.open("r", encoding="utf-8") as fp:
            loaded = yaml.safe_load(fp)
            if isinstance(loaded, dict):
                raw = loaded

    gem_raw = raw.get("gemini") or {}
    default_model = (
        os.environ.get("GEMINI_MODEL", "").strip()
        or str(gem_raw.get("default_model", "gemini-2.5-pro")).strip()
    )
    fallbacks = [
        str(m).strip()
        for m in (gem_raw.get("fallback_models") or [])
        if str(m).strip()
    ]
    if default_model and default_model not in fallbacks:
        fallbacks.insert(0, default_model)

    max_turns = int(gem_raw.get("max_turns", 15))
    env_turns = os.environ.get("MAX_AGENT_TURNS", "").strip()
    if env_turns.isdigit() and int(env_turns) > 0:
        max_turns = int(env_turns)

    repos: dict[str, CloudRepoConfig] = {}
    for key, info in (raw.get("repos") or {}).items():
        if not isinstance(info, dict):
            continue
        repo_key = str(key).strip().lower()
        repo_name = str(info.get("repo", "")).strip()
        if not repo_key or not repo_name:
            continue
        repos[repo_key] = CloudRepoConfig(
            key=repo_key,
            repo=repo_name,
            default_branch=str(info.get("default_branch", "main")).strip() or "main",
            description=str(info.get("description", "")).strip(),
        )

    workflows: dict[str, CloudWorkflowConfig] = {}
    for key, info in (raw.get("workflows") or {}).items():
        if not isinstance(info, dict):
            continue
        wf_key = str(key).strip().lower()
        wf_repo = str(info.get("repo", "")).strip()
        wf_file = str(info.get("workflow", "")).strip()
        if not wf_key or not wf_repo or not wf_file:
            continue
        inputs_raw = info.get("inputs") or {}
        workflows[wf_key] = CloudWorkflowConfig(
            key=wf_key,
            repo=wf_repo,
            workflow=wf_file,
            ref=str(info.get("ref", "main")).strip() or "main",
            inputs=dict(inputs_raw) if isinstance(inputs_raw, dict) else {},
            description=str(info.get("description", "")).strip(),
        )

    default_repo = str(raw.get("default_repo", "slack_aibot")).strip().lower()
    if default_repo not in repos and repos:
        default_repo = next(iter(repos.keys()))

    return CloudAppConfig(
        default_repo=default_repo,
        gemini=CloudGeminiConfig(
            default_model=default_model,
            fallback_models=fallbacks,
            max_turns=max_turns,
        ),
        repos=repos,
        workflows=workflows,
    )
