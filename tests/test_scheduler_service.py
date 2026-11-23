"""scheduler_service 모듈 단위 테스트."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.scheduler_service import SchedulerService
from src.state_store import ScheduleRecord, StateStore


def test_scheduler_triggers_once_per_day_and_respects_enabled(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    triggered: list[int] = []
    try:
        s1 = store.add_schedule("09:30", "run", "ondotrip", "ondotrip", "C1", "U1")
        s2 = store.add_schedule("09:30", "ai", "slack_aibot", "점검", "C1", "U1")
        store.set_schedule_enabled(s2.schedule_id, False)

        def _on_trigger(rec: ScheduleRecord) -> None:
            triggered.append(rec.schedule_id)

        svc = SchedulerService(store=store, on_trigger=_on_trigger, poll_interval_sec=60.0)

        # 다른 시각 -> 실행 안 됨
        assert svc.tick_once(datetime(2026, 11, 23, 9, 29)) == []
        # 일치 시각 -> s1만 실행
        ran_first = svc.tick_once(datetime(2026, 11, 23, 9, 30))
        assert [r.schedule_id for r in ran_first] == [s1.schedule_id]
        # 같은 날 같은 시각 재점검 -> 중복 실행 방지
        assert svc.tick_once(datetime(2026, 11, 23, 9, 30)) == []
        # 다음 날 같은 시각 -> 다시 실행
        ran_next_day = svc.tick_once(datetime(2026, 11, 24, 9, 30))
        assert [r.schedule_id for r in ran_next_day] == [s1.schedule_id]
    finally:
        store.close()
