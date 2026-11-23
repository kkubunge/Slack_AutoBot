"""GitHub Actions 클라우드 워커의 Slack 명령 실행 및 보고 진입점."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

_CONTROLLER_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_CONTROLLER_ROOT) not in sys.path:
    sys.path.insert(0, str(_CONTROLLER_ROOT))

from cloud.runner.cloud_config import CloudAppConfig, load_cloud_config  # noqa: E402
from cloud.runner.gemini_agent import AgentRunResult, GeminiCodingAgent  # noqa: E402
from cloud.runner.github_ops import commit_push_and_create_pr, trigger_remote_workflow  # noqa: E402
from cloud.runner.slack_reporter import SlackWebReporter  # noqa: E402
from cloud.runner.tools import RepoToolchain  # noqa: E402
from src.formatter import markdown_to_slack_mrkdwn  # noqa: E402

logger = logging.getLogger(__name__)


def load_payload(payload_file: str = "") -> dict[str, Any]:
    """환경변수 `PAYLOAD_JSON` 또는 파일/INPUT_* 변수에서 실행 페이로드를 읽는다."""
    raw_json = os.environ.get("PAYLOAD_JSON", "").strip()
    if raw_json and raw_json != "null":
        try:
            loaded = json.loads(raw_json)
            if isinstance(loaded, dict) and loaded:
                return loaded
        except json.JSONDecodeError:
            pass

    if payload_file:
        p = Path(payload_file)
        if p.is_file():
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded

    return {
        "job_id": uuid.uuid4().hex[:8],
        "action": os.environ.get("INPUT_ACTION", "list").strip().lower(),
        "project": os.environ.get("INPUT_PROJECT", "slack_aibot").strip().lower(),
        "mode": os.environ.get("INPUT_MODE", "plan").strip().lower(),
        "prompt": os.environ.get("INPUT_PROMPT", "").strip(),
        "workflow": os.environ.get("INPUT_WORKFLOW", "").strip().lower(),
        "extra_args": os.environ.get("INPUT_EXTRA_ARGS", "").strip(),
        "channel_id": os.environ.get("SLACK_REPORT_CHANNEL_ID", "").strip(),
        "thread_ts": "",
        "status_ts": "",
        "user_id": "github-actions",
        "response_url": "",
    }


def build_list_report(cloud_cfg: CloudAppConfig, list_target: str) -> str:
    """등록된 클라우드 저장소 및 워크플로우 목록 보고서를 생성한다."""
    lines: list[str] = []
    if list_target != "workflows":
        lines.append("*클라우드 등록 GitHub 저장소 목록*")
        for key, repo in cloud_cfg.repos.items():
            mark = " _(기본)_" if key == cloud_cfg.default_repo else ""
            lines.append(f"• `{key}` → `{repo.repo}` (`{repo.default_branch}`) — {repo.description}{mark}")
        lines.append("")

    lines.append("*원격 실행 가능한 워크플로우 프리셋*")
    for key, wf in cloud_cfg.workflows.items():
        lines.append(f"• `{key}` → `{wf.repo}` / `{wf.workflow}` — {wf.description}")
    lines.append("\n_사용 예시: `!실행 traillog-dryrun` 또는 `[daylog] 앱 구조 분석해줘`_")
    return "\n".join(lines)


def format_cloud_ai_report(
    job_id: str,
    project_key: str,
    repo_name: str,
    mode: str,
    elapsed_sec: float,
    agent_res: AgentRunResult,
    pr_info: dict[str, Any] | None = None,
    run_url: str = "",
) -> str:
    """클라우드 Gemini 에이전트 실행 결과를 Slack 보고서로 포맷한다."""
    badge = ":white_check_mark: *클라우드 AI 작업 완료*" if agent_res.success else ":x: *클라우드 AI 작업 실패*"
    lines = [
        f"{badge} (`{job_id}`)",
        f"• 저장소: `{project_key}` (`{repo_name}`) | 모드: `{mode}` | 모델: `{agent_res.model_used or '-'}`",
        f"• 턴 수: `{agent_res.turns}` | 소요: `{elapsed_sec:.1f}초`",
    ]

    if agent_res.tool_history:
        shown = agent_res.tool_history[:10]
        extra = len(agent_res.tool_history) - len(shown)
        lines.append("• *수행 도구 요약*:")
        for item in shown:
            lines.append(f"  - {item}")
        if extra > 0:
            lines.append(f"  - _외 {extra}개 단계 더 있음_")

    if pr_info and pr_info.get("changed"):
        if pr_info.get("pr_url"):
            lines.append(f"• *자동 생성 PR*: <{pr_info['pr_url']}|PR #{pr_info.get('pr_number', '')}> (브랜치: `{pr_info.get('branch')}`)")
        elif pr_info.get("branch"):
            lines.append(f"• *푸시된 브랜치*: `{pr_info['branch']}`")
        if pr_info.get("error"):
            lines.append(f"• :warning: *PR/푸시 참고*: `{pr_info['error']}`")

    if agent_res.response:
        lines.append("")
        lines.append("*결과 보고*")
        lines.append(markdown_to_slack_mrkdwn(agent_res.response))
    elif agent_res.error:
        lines.append("")
        lines.append("*오류 상세*")
        lines.append(f"```\n{agent_res.error}\n```")

    if run_url:
        lines.append(f"\n<{run_url}|GitHub Actions 실행 로그 보기>")

    return "\n".join(lines)


def execute_cloud_payload(
    payload: dict[str, Any],
    cloud_cfg: CloudAppConfig,
    reporter: SlackWebReporter,
    target_repo_dir: Path | None = None,
    gemini_api_key: str = "",
    gh_token: str = "",
    run_url: str = "",
    agent: GeminiCodingAgent | None = None,
) -> int:
    """파싱된 클라우드 명령을 실행하고 Slack으로 최종 결과를 보고한다."""
    job_id = str(payload.get("job_id") or uuid.uuid4().hex[:8])
    action = str(payload.get("action") or "list").strip().lower()
    project_key = str(payload.get("project") or cloud_cfg.default_repo).strip().lower()
    mode = str(payload.get("mode") or "dev").strip().lower()
    prompt = str(payload.get("prompt") or "").strip()
    workflow_key = str(payload.get("workflow") or "").strip().lower()
    channel_id = str(payload.get("channel_id") or "").strip()
    thread_ts = str(payload.get("thread_ts") or "").strip()
    status_ts = str(payload.get("status_ts") or "").strip()
    response_url = str(payload.get("response_url") or "").strip()

    def _report(text: str) -> None:
        reporter.report_final(
            channel=channel_id,
            thread_ts=thread_ts,
            status_ts=status_ts,
            report_text=text,
            response_url=response_url,
        )

    if action == "list":
        _report(build_list_report(cloud_cfg, prompt))
        return 0

    if action == "run":
        if workflow_key not in cloud_cfg.workflows:
            available = ", ".join(f"`{k}`" for k in cloud_cfg.workflows) or "없음"
            _report(
                f":warning: 등록되지 않은 원격 워크플로우입니다: `{workflow_key}`\n사용 가능 목록: {available}"
            )
            return 1

        wf = cloud_cfg.workflows[workflow_key]
        res = trigger_remote_workflow(
            repo_full_name=wf.repo,
            workflow_file=wf.workflow,
            ref=wf.ref,
            inputs=wf.inputs,
            token=gh_token,
        )
        if res.get("ok"):
            _report(
                f":rocket: *원격 워크플로우 실행을 시작했습니다* (`{job_id}`)\n"
                f"• 프리셋: `{wf.key}` (`{wf.repo}` / `{wf.workflow}`)\n"
                f"• <{res['actions_url']}|대상 저장소 Actions 진행 상황 보기>"
            )
            return 0

        _report(
            f":x: *원격 워크플로우 실행 실패* (`{job_id}`)\n"
            f"• 프리셋: `{wf.key}` (`{wf.repo}`)\n"
            f"```\n{res.get('error', '알 수 없는 오류')}\n```"
        )
        return 1

    if action == "ai":
        if project_key not in cloud_cfg.repos:
            available = ", ".join(f"`{k}`" for k in cloud_cfg.repos)
            _report(
                f":warning: 등록되지 않은 클라우드 저장소입니다: `{project_key}`\n등록된 저장소: {available}"
            )
            return 1

        repo_cfg = cloud_cfg.repos[project_key]
        repo_dir = (target_repo_dir or Path.cwd()).resolve()
        if not repo_dir.is_dir():
            _report(f":x: 대상 저장소 체크아웃 경로가 존재하지 않습니다: `{repo_dir}`")
            return 1

        if not prompt:
            _report(":warning: AI에게 전달할 지시 내용이 비어 있습니다.")
            return 1

        allow_write = mode == "dev"
        toolchain = RepoToolchain(repo_root=repo_dir, allow_write=allow_write)
        coding_agent = agent or GeminiCodingAgent(
            api_key=gemini_api_key,
            default_model=cloud_cfg.gemini.default_model,
            fallback_models=cloud_cfg.gemini.fallback_models,
            max_turns=cloud_cfg.gemini.max_turns,
        )

        started = time.monotonic()
        last_update = 0.0
        steps_preview: list[str] = []

        def _on_progress(step_msg: str) -> None:
            nonlocal last_update
            steps_preview.append(step_msg)
            now_m = time.monotonic()
            if status_ts and channel_id and (now_m - last_update >= 4.0):
                last_update = now_m
                recent = "\n".join(f"  - {s}" for s in steps_preview[-5:])
                reporter.update_message(
                    channel_id,
                    status_ts,
                    (
                        f":hourglass_flowing_sand: *클라우드 AI 작업 진행 중* (`{job_id}`)\n"
                        f"• 저장소: `{project_key}` (`{repo_cfg.repo}`) | 모드: `{mode}` | 경과: `{now_m - started:.0f}초`\n"
                        f"• *최근 진행*:\n{recent}"
                    ),
                )

        agent_res = coding_agent.run(
            prompt=prompt,
            toolchain=toolchain,
            mode=mode,
            on_progress=_on_progress,
        )
        elapsed = time.monotonic() - started

        pr_info: dict[str, Any] | None = None
        if agent_res.success and allow_write:
            pr_info = commit_push_and_create_pr(
                repo_dir=repo_dir,
                repo_full_name=repo_cfg.repo,
                base_branch=repo_cfg.default_branch,
                job_id=job_id,
                prompt=prompt,
                summary=agent_res.response,
                token=gh_token,
            )

        report_text = format_cloud_ai_report(
            job_id=job_id,
            project_key=project_key,
            repo_name=repo_cfg.repo,
            mode=mode,
            elapsed_sec=elapsed,
            agent_res=agent_res,
            pr_info=pr_info,
            run_url=run_url,
        )
        _report(report_text)
        return 0 if agent_res.success else 1

    _report(f":warning: 알 수 없는 클라우드 작업 유형입니다: `{action}`")
    return 1


def main(argv: list[str] | None = None) -> int:
    """GitHub Actions 클라우드 워커 진입점."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Slack_AIbot 클라우드 워커")
    parser.add_argument("--payload-file", default="", help="페이로드 JSON 파일 경로(선택)")
    args = parser.parse_args(argv)

    cloud_cfg = load_cloud_config()
    payload = load_payload(args.payload_file)
    reporter = SlackWebReporter(bot_token=os.environ.get("SLACK_BOT_TOKEN", ""))

    target_dir_raw = os.environ.get("TARGET_REPO_DIR", "").strip()
    target_dir = Path(target_dir_raw) if target_dir_raw else Path.cwd()

    try:
        return execute_cloud_payload(
            payload=payload,
            cloud_cfg=cloud_cfg,
            reporter=reporter,
            target_repo_dir=target_dir,
            gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
            gh_token=os.environ.get("GH_TOKEN", ""),
            run_url=os.environ.get("GITHUB_RUN_URL", ""),
        )
    except Exception as exc:
        logger.exception("클라우드 워커 치명 오류: %s", exc)
        channel_id = str(payload.get("channel_id") or "")
        thread_ts = str(payload.get("thread_ts") or "")
        status_ts = str(payload.get("status_ts") or "")
        run_url = os.environ.get("GITHUB_RUN_URL", "")
        err_msg = f":x: *클라우드 워커 실행 중 예외 발생*\n```\n{exc}\n```"
        if run_url:
            err_msg += f"\n<{run_url}|GitHub Actions 실행 로그 보기>"
        reporter.report_final(channel_id, thread_ts, status_ts, err_msg)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
