"""GitHub Actions 원격 워크플로우 실행 및 PR 자동 생성 유틸리티."""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable


def _default_github_request(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | str]:
    """GitHub REST API v3 HTTP 요청을 수행한다."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Slack-AutoBot-Runner/1.0",
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if not raw:
                return resp.status, {}
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        err_raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(err_raw)
        except json.JSONDecodeError:
            return exc.code, err_raw
    except Exception as exc:
        return 0, str(exc)


def trigger_remote_workflow(
    repo_full_name: str,
    workflow_file: str,
    ref: str,
    inputs: dict[str, Any],
    token: str,
    transport: Callable[[str, str, str, dict[str, Any] | None], tuple[int, dict[str, Any] | str]]
    | None = None,
) -> dict[str, Any]:
    """대상 저장소의 workflow_dispatch를 원격 실행한다."""
    if not token:
        return {"ok": False, "error": "GH_TOKEN(또는 GH_PAT)이 설정되지 않았습니다."}

    req_fn = transport or _default_github_request
    url = f"https://api.github.com/repos/{repo_full_name}/actions/workflows/{workflow_file}/dispatches"
    # GitHub workflow_dispatch inputs는 문자열/불리언 JSON 전달
    clean_inputs = {str(k): str(v).lower() if isinstance(v, bool) else str(v) for k, v in (inputs or {}).items()}
    status, body = req_fn("POST", url, token, {"ref": ref, "inputs": clean_inputs})
    actions_url = f"https://github.com/{repo_full_name}/actions/workflows/{workflow_file}"

    if status in (200, 201, 204):
        return {"ok": True, "actions_url": actions_url}

    detail = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)[:300]
    return {
        "ok": False,
        "actions_url": actions_url,
        "error": f"워크플로우 트리거 실패 (HTTP {status}): {detail}",
    }


def _run_git(repo_dir: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )


def commit_push_and_create_pr(
    repo_dir: Path,
    repo_full_name: str,
    base_branch: str,
    job_id: str,
    prompt: str,
    summary: str,
    token: str,
    transport: Callable[[str, str, str, dict[str, Any] | None], tuple[int, dict[str, Any] | str]]
    | None = None,
) -> dict[str, Any]:
    """변경된 코드가 있으면 새 브랜치에 커밋·푸시하고 Pull Request를 생성한다."""
    status_proc = _run_git(repo_dir, ["status", "--porcelain"])
    if status_proc.returncode != 0:
        return {"ok": False, "changed": False, "error": f"git status 실패: {status_proc.stderr}"}
    if not status_proc.stdout.strip():
        return {"ok": True, "changed": False}

    branch_name = f"ai/slack-{job_id}"
    _run_git(repo_dir, ["config", "user.name", "slack-autobot[bot]"])
    _run_git(repo_dir, ["config", "user.email", "slack-autobot[bot]@users.noreply.github.com"])

    chk = _run_git(repo_dir, ["checkout", "-b", branch_name])
    if chk.returncode != 0:
        return {"ok": False, "changed": True, "error": f"브랜치 생성 실패: {chk.stderr}"}

    add_proc = _run_git(repo_dir, ["add", "-A"])
    if add_proc.returncode != 0:
        return {"ok": False, "changed": True, "error": f"git add 실패: {add_proc.stderr}"}

    short_prompt = " ".join(prompt.split())[:60]
    commit_msg = f"feat(ai): {short_prompt or job_id}"
    commit_proc = _run_git(repo_dir, ["commit", "-m", commit_msg])
    if commit_proc.returncode != 0:
        return {"ok": False, "changed": True, "error": f"git commit 실패: {commit_proc.stderr}"}

    diff_stat = _run_git(repo_dir, ["show", "--stat", "--oneline", "-n", "1"]).stdout.strip()

    push_proc = _run_git(repo_dir, ["push", "-u", "origin", branch_name])
    if push_proc.returncode != 0:
        return {
            "ok": False,
            "changed": True,
            "branch": branch_name,
            "diff_stat": diff_stat,
            "error": f"git push 실패 (대상 저장소 쓰기 권한을 확인하세요: GH_PAT 필요): {push_proc.stderr[:300]}",
        }

    if not token:
        return {
            "ok": True,
            "changed": True,
            "branch": branch_name,
            "diff_stat": diff_stat,
            "pr_url": "",
        }

    req_fn = transport or _default_github_request
    pr_url_api = f"https://api.github.com/repos/{repo_full_name}/pulls"
    pr_body = (
        f"## AutoBot 클라우드 자동 PR (`{job_id}`)\n\n"
        f"**요청 지시**\n> {prompt}\n\n"
        f"**AI 요약**\n{summary[:2500]}\n"
    )
    status_code, pr_resp = req_fn(
        "POST",
        pr_url_api,
        token,
        {
            "title": f"[AutoBot] {short_prompt or job_id}",
            "head": branch_name,
            "base": base_branch,
            "body": pr_body,
        },
    )
    if status_code in (200, 201) and isinstance(pr_resp, dict):
        return {
            "ok": True,
            "changed": True,
            "branch": branch_name,
            "diff_stat": diff_stat,
            "pr_url": str(pr_resp.get("html_url") or ""),
            "pr_number": pr_resp.get("number"),
        }

    detail = pr_resp if isinstance(pr_resp, str) else json.dumps(pr_resp, ensure_ascii=False)[:300]
    return {
        "ok": True,
        "changed": True,
        "branch": branch_name,
        "diff_stat": diff_stat,
        "pr_url": "",
        "error": f"브랜치는 푸시되었으나 PR 생성 실패 (HTTP {status_code}): {detail}",
    }
