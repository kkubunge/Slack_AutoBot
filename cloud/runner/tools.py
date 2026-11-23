"""클라우드 AI 에이전트가 대상 저장소 내부에서만 안전하게 사용할 수 있는 파일/명령 도구."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "build", ".gradle"}


class RepoToolchain:
    """대상 저장소 루트로 경로를 격리한 읽기/편집/명령 도구 집합."""

    def __init__(
        self,
        repo_root: Path,
        allow_write: bool = False,
        max_output_chars: int = 8000,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.allow_write = allow_write
        self.max_output_chars = max_output_chars
        self.modified_files: set[str] = set()
        self.tool_history: list[str] = []

    def resolve_safe_path(self, rel_path: str, allow_root: bool = False) -> Path:
        """상대 경로가 저장소 루트 내부이며 `.git` 경로가 아님을 검증한다."""
        cleaned = (rel_path or "").strip()
        if not cleaned or cleaned == ".":
            if allow_root:
                return self.repo_root
            raise ValueError("파일 경로를 지정해야 합니다.")

        input_path = Path(cleaned)
        if input_path.is_absolute():
            raise ValueError(f"절대 경로는 허용되지 않습니다: {cleaned}")

        candidate = (self.repo_root / input_path).resolve()
        if not candidate.is_relative_to(self.repo_root):
            raise ValueError(f"저장소 외부 경로 접근이 차단되었습니다: {cleaned}")

        if candidate != self.repo_root:
            rel_parts = candidate.relative_to(self.repo_root).parts
            if ".git" in rel_parts:
                raise ValueError("`.git` 내부 파일 접근 및 수정은 금지되어 있습니다.")
        elif not allow_root:
            raise ValueError("루트 디렉터리 자체를 파일 대상으로 지정할 수 없습니다.")

        return candidate

    def get_gemini_tool_declarations(self) -> list[dict[str, Any]]:
        """모드(allow_write)에 맞는 Gemini FunctionDeclaration 목록을 반환한다."""
        declarations: list[dict[str, Any]] = [
            {
                "name": "list_files",
                "description": "저장소 내부 파일 목록을 조회합니다 (.git, .venv, build 등 제외).",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "directory": {
                            "type": "STRING",
                            "description": "조회할 하위 디렉터리 상대 경로 (기본값 '.')",
                        },
                        "max_entries": {
                            "type": "INTEGER",
                            "description": "최대 반환 파일 수 (기본값 200)",
                        },
                    },
                },
            },
            {
                "name": "read_file",
                "description": "저장소 내 텍스트 파일의 지정 줄 범위를 읽습니다.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "path": {"type": "STRING", "description": "저장소 기준 파일 상대 경로"},
                        "start_line": {"type": "INTEGER", "description": "시작 줄 번호 (1부터 시작, 기본 1)"},
                        "end_line": {"type": "INTEGER", "description": "끝 줄 번호 (기본 250)"},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "search_code",
                "description": "저장소 내 파일들에서 정규식 또는 문자열 패턴을 검색합니다.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "pattern": {"type": "STRING", "description": "검색할 문자열 또는 정규식"},
                        "directory": {"type": "STRING", "description": "검색 시작 상대 경로 (기본 '.')"},
                        "max_matches": {"type": "INTEGER", "description": "최대 일치 수 (기본 50)"},
                    },
                    "required": ["pattern"],
                },
            },
        ]

        if self.allow_write:
            declarations.extend(
                [
                    {
                        "name": "write_file",
                        "description": "새 파일을 생성하거나 기존 파일을 전체 덮어씁니다.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "path": {"type": "STRING", "description": "저장소 기준 파일 상대 경로"},
                                "content": {"type": "STRING", "description": "저장할 전체 UTF-8 파일 내용"},
                            },
                            "required": ["path", "content"],
                        },
                    },
                    {
                        "name": "edit_file",
                        "description": "기존 파일에서 target 문자열과 정확히 일치하는 부분을 replacement로 치환합니다.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "path": {"type": "STRING", "description": "수정할 파일 상대 경로"},
                                "target": {"type": "STRING", "description": "기존 파일의 정확한 문자열"},
                                "replacement": {"type": "STRING", "description": "대체할 새 문자열"},
                            },
                            "required": ["path", "target", "replacement"],
                        },
                    },
                    {
                        "name": "delete_file",
                        "description": "저장소 내 불필요한 파일을 삭제합니다.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "path": {"type": "STRING", "description": "삭제할 파일 상대 경로"},
                            },
                            "required": ["path"],
                        },
                    },
                    {
                        "name": "run_command",
                        "description": "테스트·린트·빌드 검증을 위해 저장소 루트에서 셸 명령을 실행합니다.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "command": {"type": "STRING", "description": "실행할 셸 명령"},
                                "timeout_sec": {"type": "INTEGER", "description": "타임아웃 초 (기본 120)"},
                            },
                            "required": ["command"],
                        },
                    },
                ]
            )

        return [{"functionDeclarations": declarations}]

    def _clip(self, text: str) -> str:
        if len(text) <= self.max_output_chars:
            return text
        return text[: self.max_output_chars] + "\n...(출력이 길어 생략됨)..."

    def execute(self, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
        """도구 이름과 인자를 받아 안전하게 실행하고 결과 딕셔너리를 반환한다."""
        params = args or {}
        if name in {"write_file", "edit_file", "delete_file", "run_command"} and not self.allow_write:
            return {
                "ok": False,
                "error": "읽기 전용(plan) 모드에서는 파일 수정 또는 명령 실행 도구를 사용할 수 없습니다.",
            }

        try:
            if name == "list_files":
                return self._tool_list_files(
                    directory=str(params.get("directory") or "."),
                    max_entries=int(params.get("max_entries") or 200),
                )
            if name == "read_file":
                return self._tool_read_file(
                    path=str(params.get("path") or ""),
                    start_line=int(params.get("start_line") or 1),
                    end_line=int(params.get("end_line") or 250),
                )
            if name == "search_code":
                return self._tool_search_code(
                    pattern=str(params.get("pattern") or ""),
                    directory=str(params.get("directory") or "."),
                    max_matches=int(params.get("max_matches") or 50),
                )
            if name == "write_file":
                return self._tool_write_file(
                    path=str(params.get("path") or ""),
                    content=str(params.get("content") or ""),
                )
            if name == "edit_file":
                return self._tool_edit_file(
                    path=str(params.get("path") or ""),
                    target=str(params.get("target") or ""),
                    replacement=str(params.get("replacement") or ""),
                )
            if name == "delete_file":
                return self._tool_delete_file(path=str(params.get("path") or ""))
            if name == "run_command":
                return self._tool_run_command(
                    command=str(params.get("command") or ""),
                    timeout_sec=int(params.get("timeout_sec") or 120),
                )
            return {"ok": False, "error": f"지원되지 않는 도구입니다: {name}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _tool_list_files(self, directory: str, max_entries: int) -> dict[str, Any]:
        base = self.resolve_safe_path(directory, allow_root=True)
        if not base.is_dir():
            return {"ok": False, "error": f"디렉터리가 존재하지 않습니다: {directory}"}

        files: list[str] = []
        for root_str, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for fname in sorted(filenames):
                full = Path(root_str) / fname
                rel = full.relative_to(self.repo_root).as_posix()
                files.append(rel)
                if len(files) >= max_entries:
                    break
            if len(files) >= max_entries:
                break

        self.tool_history.append(f"`list_files`: `{directory}` ({len(files)}건)")
        return {"ok": True, "files": files, "truncated": len(files) >= max_entries}

    def _tool_read_file(self, path: str, start_line: int, end_line: int) -> dict[str, Any]:
        target = self.resolve_safe_path(path, allow_root=False)
        if not target.is_file():
            return {"ok": False, "error": f"파일이 존재하지 않습니다: {path}"}

        start = max(1, start_line)
        end = max(start, end_line)
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[start - 1 : end]
        numbered = "\n".join(f"{idx}: {line}" for idx, line in enumerate(selected, start=start))
        self.tool_history.append(f"`read_file`: `{path}` ({start}-{min(end, len(lines))}줄)")
        return {
            "ok": True,
            "path": path,
            "total_lines": len(lines),
            "content": self._clip(numbered),
        }

    def _tool_search_code(self, pattern: str, directory: str, max_matches: int) -> dict[str, Any]:
        if not pattern:
            return {"ok": False, "error": "검색 패턴이 비어 있습니다."}
        base = self.resolve_safe_path(directory, allow_root=True)
        if not base.is_dir():
            return {"ok": False, "error": f"디렉터리가 존재하지 않습니다: {directory}"}

        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))

        matches: list[str] = []
        for root_str, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for fname in sorted(filenames):
                full = Path(root_str) / fname
                if full.stat().st_size > 1_000_000:
                    continue
                try:
                    text = full.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                rel = full.relative_to(self.repo_root).as_posix()
                for line_no, line in enumerate(text.splitlines(), start=1):
                    if regex.search(line):
                        matches.append(f"{rel}:{line_no}: {line[:160]}")
                        if len(matches) >= max_matches:
                            break
                if len(matches) >= max_matches:
                    break
            if len(matches) >= max_matches:
                break

        self.tool_history.append(f"`search_code`: `{pattern[:40]}` ({len(matches)}건)")
        return {"ok": True, "matches": matches}

    def _tool_write_file(self, path: str, content: str) -> dict[str, Any]:
        target = self.resolve_safe_path(path, allow_root=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        rel = target.relative_to(self.repo_root).as_posix()
        self.modified_files.add(rel)
        self.tool_history.append(f"`write_file`: `{rel}`")
        return {"ok": True, "path": rel, "bytes": len(content.encode("utf-8"))}

    def _tool_edit_file(self, path: str, target: str, replacement: str) -> dict[str, Any]:
        file_path = self.resolve_safe_path(path, allow_root=False)
        if not file_path.is_file():
            return {"ok": False, "error": f"수정할 파일이 존재하지 않습니다: {path}"}
        if not target:
            return {"ok": False, "error": "target 문자열이 비어 있습니다."}

        original = file_path.read_text(encoding="utf-8", errors="replace")
        count = original.count(target)
        if count == 0:
            return {"ok": False, "error": f"{path}에서 target 문자열을 찾을 수 없습니다."}

        updated = original.replace(target, replacement)
        file_path.write_text(updated, encoding="utf-8")
        rel = file_path.relative_to(self.repo_root).as_posix()
        self.modified_files.add(rel)
        self.tool_history.append(f"`edit_file`: `{rel}` ({count}곳)")
        return {"ok": True, "path": rel, "replacements": count}

    def _tool_delete_file(self, path: str) -> dict[str, Any]:
        file_path = self.resolve_safe_path(path, allow_root=False)
        if not file_path.is_file():
            return {"ok": False, "error": f"삭제할 파일이 존재하지 않습니다: {path}"}
        rel = file_path.relative_to(self.repo_root).as_posix()
        file_path.unlink()
        self.modified_files.add(rel)
        self.tool_history.append(f"`delete_file`: `{rel}`")
        return {"ok": True, "deleted": rel}

    def _tool_run_command(self, command: str, timeout_sec: int) -> dict[str, Any]:
        if not command.strip():
            return {"ok": False, "error": "실행할 명령이 비어 있습니다."}
        effective_timeout = max(5, min(timeout_sec, 300))
        proc = subprocess.run(
            command,
            cwd=str(self.repo_root),
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=effective_timeout,
            check=False,
        )
        self.tool_history.append(f"`run_command`: `{command[:60]}` (rc={proc.returncode})")
        return {
            "ok": proc.returncode == 0,
            "return_code": proc.returncode,
            "stdout": self._clip(proc.stdout or ""),
            "stderr": self._clip(proc.stderr or ""),
        }
