"""클라우드 러너(cloud/runner) 및 GitHub Actions 설정 단위/통합 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cloud.runner.cloud_config import load_cloud_config
from cloud.runner.gemini_agent import GeminiCodingAgent
from cloud.runner.github_ops import trigger_remote_workflow
from cloud.runner.main import execute_cloud_payload
from cloud.runner.slack_reporter import SlackWebReporter
from cloud.runner.tools import RepoToolchain


def test_load_cloud_config_and_yaml_syntax() -> None:
    root = Path(__file__).resolve().parent.parent
    cfg = load_cloud_config(root / "cloud" / "cloud_repos.yaml")
    assert cfg.default_repo == "slack_aibot"
    assert set(cfg.repos.keys()) == {"slack_aibot", "traillog", "ondotrip"}
    assert set(cfg.workflows.keys()) == {"traillog", "traillog-dryrun", "ondotrip", "ondotrip-preview", "ondotrip-status"}

    # 클라우드 매니페스트 및 Actions 워크플로우 YAML 구문 검증
    manifest = yaml.safe_load((root / "cloud" / "slack_cloud_manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["settings"]["socket_mode_enabled"] is False
    workflow = yaml.safe_load((root / ".github" / "workflows" / "slack_ai_worker.yml").read_text(encoding="utf-8"))
    assert "repository_dispatch" in workflow[True if True in workflow else "on"]

    ondotrip_wf_path = root.parent / "Google_OndoTrip" / ".github" / "workflows" / "daily_post.yml"
    if ondotrip_wf_path.is_file():
        ondotrip_wf = yaml.safe_load(ondotrip_wf_path.read_text(encoding="utf-8"))
        assert "workflow_dispatch" in ondotrip_wf[True if True in ondotrip_wf else "on"]


def test_repo_toolchain_sandbox_and_modes(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")

    # plan 모드(읽기 전용)
    ro_tools = RepoToolchain(tmp_path, allow_write=False)
    ro_decls = ro_tools.get_gemini_tool_declarations()[0]["functionDeclarations"]
    ro_names = {d["name"] for d in ro_decls}
    assert ro_names == {"list_files", "read_file", "search_code"}

    blocked = ro_tools.execute("write_file", {"path": "x.py", "content": "1"})
    assert blocked["ok"] is False

    # 경로 탈출 및 .git 차단
    with pytest.raises(ValueError):
        ro_tools.resolve_safe_path("../outside.txt")
    with pytest.raises(ValueError):
        ro_tools.resolve_safe_path(".git/config")

    # dev 모드(쓰기/명령 허용)
    rw_tools = RepoToolchain(tmp_path, allow_write=True)
    rw_decls = rw_tools.get_gemini_tool_declarations()[0]["functionDeclarations"]
    assert len(rw_decls) == 7

    listed = rw_tools.execute("list_files", {})
    assert listed["ok"] is True
    assert "app.py" in listed["files"]

    written = rw_tools.execute("write_file", {"path": "sub/demo.txt", "content": "line1\nline2\n"})
    assert written["ok"] is True
    assert "sub/demo.txt" in rw_tools.modified_files

    edited = rw_tools.execute("edit_file", {"path": "sub/demo.txt", "target": "line2", "replacement": "line_two"})
    assert edited["ok"] is True

    searched = rw_tools.execute("search_code", {"pattern": "line_two"})
    assert searched["ok"] is True
    assert any("sub/demo.txt:2:" in m for m in searched["matches"])

    read_res = rw_tools.execute("read_file", {"path": "sub/demo.txt"})
    assert "line_two" in read_res["content"]

    cmd_res = rw_tools.execute("run_command", {"command": "python -c \"print('ok-cmd')\""})
    assert cmd_res["ok"] is True
    assert "ok-cmd" in cmd_res["stdout"]

    deleted = rw_tools.execute("delete_file", {"path": "sub/demo.txt"})
    assert deleted["ok"] is True
    assert not (tmp_path / "sub" / "demo.txt").exists()


def test_gemini_coding_agent_multiturn_and_model_fallback(tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_transport(url: str, headers: dict[str, str], payload: dict):
        calls.append(url)
        assert headers.get("x-goog-api-key") == "test-key"
        # 1순위 모델 404 -> 2순위 모델 폴백 확인
        if "gemini-primary" in url:
            return 404, {"error": {"message": "model not found"}}

        turn_num = len([c for c in calls if "gemini-fallback" in c])
        if turn_num == 1:
            return 200, {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "write_file",
                                        "args": {"path": "hello.txt", "content": "안녕 클라우드\n"},
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        if turn_num == 2:
            return 200, {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"functionCall": {"name": "read_file", "args": {"path": "hello.txt"}}}
                            ]
                        }
                    }
                ]
            }
        return 200, {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": "**hello.txt**를 생성하고 내용을 검증했습니다."}]
                    }
                }
            ]
        }

    agent = GeminiCodingAgent(
        api_key="test-key",
        default_model="gemini-primary",
        fallback_models=["gemini-fallback"],
        max_turns=5,
        transport=fake_transport,
    )
    toolchain = RepoToolchain(tmp_path, allow_write=True)
    res = agent.run("hello.txt 생성", toolchain, mode="dev")

    assert res.success is True
    assert res.model_used == "gemini-fallback"
    assert res.turns == 3
    assert "hello.txt" in res.modified_files
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "안녕 클라우드\n"


def test_trigger_remote_workflow_and_execute_cloud_payload(tmp_path: Path) -> None:
    gh_calls: list[tuple[str, str, dict | None]] = []

    def fake_gh(method: str, url: str, token: str, payload: dict | None):
        gh_calls.append((method, url, payload))
        return 204, {}

    wf_res = trigger_remote_workflow(
        repo_full_name="kkubunge/Instagram_TrailLog",
        workflow_file="auto_publish.yml",
        ref="main",
        inputs={"brd": "걷기", "dry_run": True},
        token="ghp-test",
        transport=fake_gh,
    )
    assert wf_res["ok"] is True
    assert gh_calls[0][1].endswith("/actions/workflows/auto_publish.yml/dispatches")
    assert gh_calls[0][2]["inputs"]["dry_run"] == "true"

    slack_calls: list[tuple[str, dict]] = []

    def fake_slack(url: str, headers: dict[str, str], body: dict):
        slack_calls.append((url, body))
        return {"ok": True, "ts": "999.1"}

    root = Path(__file__).resolve().parent.parent
    cfg = load_cloud_config(root / "cloud" / "cloud_repos.yaml")
    reporter = SlackWebReporter(bot_token="xoxb-test", transport=fake_slack)

    # list 명령 실행
    rc_list = execute_cloud_payload(
        payload={"job_id": "j1", "action": "list", "prompt": "projects", "channel_id": "C1", "status_ts": "100.1"},
        cloud_cfg=cfg,
        reporter=reporter,
    )
    assert rc_list == 0
    assert slack_calls[-1][0].endswith("chat.update")
    assert "kkubunge/Google_OndoTrip" in slack_calls[-1][1]["text"]

    # plan 모드 모의 AI 실행 (중첩 slack 메타데이터 형식 검증 포함)
    class FakeAgent:
        def run(self, prompt, toolchain, mode="plan", on_progress=None):
            from cloud.runner.gemini_agent import AgentRunResult

            return AgentRunResult(
                success=True,
                response="### 분석 결과\n정상입니다.",
                model_used="gemini-2.5-pro",
                turns=2,
                tool_history=["`list_files`: `.` (1건)"],
            )

    (tmp_path / "README.md").write_text("# Demo", encoding="utf-8")
    rc_ai = execute_cloud_payload(
        payload={
            "job_id": "j2",
            "action": "ai",
            "project": "ondotrip",
            "mode": "plan",
            "prompt": "구조 확인",
            "slack": {
                "channel_id": "C1",
                "thread_ts": "100.0",
                "status_ts": "100.2",
            },
        },
        cloud_cfg=cfg,
        reporter=reporter,
        target_repo_dir=tmp_path,
        agent=FakeAgent(),
        run_url="https://github.com/kkubunge/Slack_AIbot/actions/runs/1",
    )
    assert rc_ai == 0
    assert slack_calls[-1][1]["ts"] == "100.2"
    assert "클라우드 AI 작업 완료" in slack_calls[-1][1]["text"]
    assert "*분석 결과*" in slack_calls[-1][1]["text"]


def test_cloud_runner_imports_without_dotenv() -> None:
    """클라우드 환경(PyYAML만 설치, dotenv 미설치)에서도 cloud.runner.main import가 성공하는지 검증한다."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    code = (
        "import sys; sys.modules['dotenv'] = None; "
        "import src.formatter, cloud.runner.main; "
        "print('OK')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
