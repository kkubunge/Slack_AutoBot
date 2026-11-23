"""task_runner 모듈 단위 테스트."""

from __future__ import annotations

from src.task_runner import _decode_windows_output


def test_decode_windows_output_supports_utf8_and_cp949() -> None:
    sample = "2026-11-24 오전 9:30:00"
    assert _decode_windows_output(sample.encode("utf-8")) == sample
    assert _decode_windows_output(sample.encode("cp949")) == sample
    assert _decode_windows_output(b"") == ""
