"""Slack mrkdwn 변환, 장문 분할 및 보고 메시지 포맷터."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from src.agy_runner import AgyResult
    from src.task_runner import TaskResult

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _convert_prose_segment(segment: str) -> str:
    """코드가 아닌 일반 Markdown 텍스트를 Slack mrkdwn으로 변환한다."""
    # 1. 마크다운 링크 [텍스트](URL) -> <URL|텍스트>
    converted = _LINK_RE.sub(r"<\2|\1>", segment)
    # 2. 제목(# 제목) -> *제목*
    converted = _HEADING_RE.sub(r"*\1*", converted)
    # 3. 굵은 글씨(**텍스트**) -> *텍스트*
    converted = _BOLD_RE.sub(r"*\1*", converted)
    return converted


def markdown_to_slack_mrkdwn(text: str) -> str:
    """코드 블록(```)과 인라인 코드(`)를 보존하면서 Markdown을 Slack mrkdwn으로 변환한다."""
    if not text:
        return ""

    # 펜스 코드 블록 기준으로 먼저 분할
    fence_parts = re.split(r"(```[\s\S]*?```)", text)
    output_parts: list[str] = []
    for part in fence_parts:
        if part.startswith("```") and part.endswith("```"):
            output_parts.append(part)
            continue
        # 인라인 코드(`...`) 보호
        inline_parts = re.split(r"(`[^`\n]+`)", part)
        for sub in inline_parts:
            if sub.startswith("`") and sub.endswith("`"):
                output_parts.append(sub)
            else:
                output_parts.append(_convert_prose_segment(sub))

    return "".join(output_parts)


def split_slack_message(text: str, max_chars: int = 3500) -> list[str]:
    """Slack 메시지 길이 제한에 맞게 줄바꿈·코드블록 경계를 존중하여 분할한다."""
    if not text:
        return [""]
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text
    in_code_block = False

    while remaining:
        if len(remaining) <= max_chars:
            chunk = ("```\n" if in_code_block else "") + remaining
            chunks.append(chunk)
            break

        # 코드 블록 접두 여지를 남겨 안전한 절단 위치 탐색
        limit = max_chars - (8 if in_code_block else 4)
        slice_part = remaining[:limit]
        split_at = slice_part.rfind("\n")
        if split_at < int(limit * 0.4):
            split_at = slice_part.rfind(" ")
        if split_at <= 0:
            split_at = limit

        raw_piece = remaining[:split_at].rstrip("\r\n")
        remaining = remaining[split_at:].lstrip("\r\n")

        prefix = "```\n" if in_code_block else ""
        # 현재 조각에 포함된 ``` 개수로 열린 코드블록 상태 추적
        fence_count = raw_piece.count("```")
        if fence_count % 2 == 1:
            in_code_block = not in_code_block

        suffix = "\n```" if in_code_block else ""
        chunks.append(f"{prefix}{raw_piece}{suffix}")

    return chunks


def format_progress_message(
    job_id: str,
    title: str,
    project_key: str,
    detail_label: str,
    elapsed_sec: float,
    recent_steps: Iterable[str],
) -> str:
    """진행 중인 작업의 실시간 상태 메시지를 생성한다."""
    steps_list = list(recent_steps)[-6:]
    lines = [
        f":hourglass_flowing_sand: *{title}* (`{job_id}`)",
        f"• 프로젝트: `{project_key}` | {detail_label} | 경과: `{elapsed_sec:.1f}초`",
    ]
    if steps_list:
        lines.append("• *최근 진행 내역*:")
        for step in steps_list:
            lines.append(f"  - {step}")
    else:
        lines.append("• 준비 중입니다...")
    return "\n".join(lines)


def format_agy_report(
    job_id: str,
    project_key: str,
    model: str,
    mode: str,
    result: AgyResult,
) -> str:
    """Antigravity CLI 실행 결과를 Slack 보고서 형식으로 포맷한다."""
    if result.cancelled:
        badge = ":octagonal_sign: *AI 작업 중단됨*"
    elif result.success:
        badge = ":white_check_mark: *AI 작업 완료*"
    else:
        badge = ":x: *AI 작업 실패*"

    conv_short = result.conversation_id[:8] if result.conversation_id else "-"
    total_tokens = result.usage.get("total_tokens", 0)
    token_info = f" | 토큰: `{total_tokens:,}`" if total_tokens else ""

    lines = [
        f"{badge} (`{job_id}`)",
        f"• 프로젝트: `{project_key}` | 모델: `{model}` | 모드: `{mode}`",
        f"• 세션: `{conv_short}` | 소요: `{result.duration_sec:.1f}초`{token_info}",
    ]

    if result.tool_history:
        shown_tools = result.tool_history[:10]
        extra = len(result.tool_history) - len(shown_tools)
        lines.append("• *수행 도구 요약*:")
        for item in shown_tools:
            lines.append(f"  - {item}")
        if extra > 0:
            lines.append(f"  - _외 {extra}개 단계 더 있음_")

    if result.response:
        lines.append("")
        lines.append("*결과 보고*")
        lines.append(markdown_to_slack_mrkdwn(result.response))
    elif result.error:
        lines.append("")
        lines.append("*오류 상세*")
        lines.append(f"```\n{result.error}\n```")

    if result.error and result.response:
        lines.append("")
        lines.append(f":warning: *참고 오류*: `{result.error}`")

    if result.log_path:
        lines.append(f"\n_상세 로그: `{result.log_path}`_")

    return "\n".join(lines)


def format_task_report(
    job_id: str,
    task_key: str,
    project_key: str,
    result: TaskResult,
) -> str:
    """로컬 작업 프리셋 실행 결과를 Slack 보고서 형식으로 포맷한다."""
    if result.cancelled:
        badge = ":octagonal_sign: *작업 중단됨*"
    elif result.success:
        badge = ":white_check_mark: *작업 실행 완료*"
    else:
        badge = ":x: *작업 실행 실패*"

    lines = [
        f"{badge} (`{job_id}`)",
        f"• 작업: `{task_key}` | 프로젝트: `{project_key}` | 종료코드: `{result.return_code}` | 소요: `{result.duration_sec:.1f}초`",
    ]

    if result.stdout_tail:
        tail_text = "\n".join(result.stdout_tail[-15:])
        lines.append("*최근 출력*")
        lines.append(f"```\n{tail_text}\n```")

    if not result.success:
        err_parts: list[str] = []
        if result.error:
            err_parts.append(result.error)
        if result.stderr_tail:
            err_parts.append("\n".join(result.stderr_tail[-12:]))
        if err_parts:
            lines.append("*오류 로그*")
            lines.append(f"```\n{chr(10).join(err_parts)}\n```")

    if result.log_path:
        lines.append(f"_전체 로그: `{result.log_path}`_")

    return "\n".join(lines)


def format_help_message() -> str:
    """Slack 봇 전체 명령어 도움말을 반환한다."""
    return (
        "*AutoBot(오토봇) 도움말*\n"
        "\n"
        "*1. AI 개발 및 분석 명령*\n"
        "• 일반 메시지 입력 → 현재 선택된 프로젝트에서 바로 AI 개발(`dev` 모드) 실행\n"
        "• `[프로젝트명] 지시내용` → 이번 요청만 지정 프로젝트에서 실행 (예: `[온도트립] 로그 포맷 개선해줘`)\n"
        "• 프로젝트/실행 별칭: `ondotrip` = `온도트립`·`구글블로그` | `traillog` = `트레일로그`·`인스타` | `slack_aibot` = `AutoBot`·`오토봇`\n"
        "• `!개발 <지시>` / `!dev <지시>` → AI 개발(`dev` 모드) 실행\n"
        "• `!분석 <지시>` / `!plan <지시>` → 코드 수정 없이 읽기 전용(`plan` 모드) 분석·계획 수립\n"
        "• _스레드에 답글을 달면 이전 AI 대화(`conversation_id`)가 자동으로 이어집니다._\n"
        "\n"
        "*2. 프로젝트·모델·모드 관리*\n"
        "• `!프로젝트` / `!projects` → 등록된 프로젝트 목록과 현재 선택 표시\n"
        "• `!선택 <프로젝트>` / `!use <프로젝트>` → 현재 채널의 기본 작업 프로젝트 변경\n"
        "• `!새프로젝트 <이름> [설명]` / `!new <이름> [설명]` → 새 프로젝트 폴더 생성 및 등록\n"
        "• `!모델 [모델명]` / `!model [모델명]` → 사용 가능 모델 목록 조회 또는 변경\n"
        "• `!모드 [dev|plan]` / `!mode [dev|plan]` → 기본 실행 모드 조회 또는 변경\n"
        "\n"
        "*3. 프리셋 작업 및 스케줄 대체 제어*\n"
        "• `!실행` / `!run` → 등록된 작업 프리셋 목록 조회\n"
        "• `!실행 <작업명> [추가인자]` / `!run <작업명> [추가인자]` → 프로젝트 작업 실행 및 결과 보고\n"
        "• `!예약 목록` / `!schedule list` → 내장 일일 예약 목록 조회\n"
        "• `!예약 추가 <HH:MM> <run|ai> <대상> [내용]` → 매일 지정 시각에 자동 실행 예약\n"
        "  예) `!예약 추가 10:00 run ondotrip` / `!예약 추가 09:00 ai traillog 오늘자 게시 이력 점검해줘`\n"
        "• `!예약 삭제 <ID>` / `!예약 켜기 <ID>` / `!예약 끄기 <ID>`\n"
        "\n"
        "*4. 작업 모니터링·로그·상태*\n"
        "• `!상태` / `!status` → 봇 가동 시간, 현재 설정, `agy` 상태, 실행 중 작업 수 조회\n"
        "• `!작업` / `!jobs` → 실행 중 및 최근 완료 작업 이력 조회\n"
        "• `!중단 <작업ID|all>` / `!stop <작업ID|all>` → 실행 중인 작업 즉시 중단\n"
        "• `!로그 <작업ID>` / `!log <작업ID>` → 해당 작업의 마지막 로그 확인\n"
        "• `!도움` / `!help` → 이 도움말 표시"
    )
