"""state_store 모듈 단위 테스트."""

from __future__ import annotations

from pathlib import Path

from src.state_store import StateStore


def test_thread_sessions_and_channel_settings(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    try:
        assert store.get_thread_session("C1", "100.1") is None
        saved = store.upsert_thread_session("C1", "100.1", "conv-abc", "slack_autobot", "gemini-4-neon-high", "dev")
        assert saved.conversation_id == "conv-abc"

        loaded = store.get_thread_session("C1", "100.1")
        assert loaded is not None
        assert loaded.project_key == "slack_autobot"

        store.upsert_thread_session("C1", "100.1", "conv-xyz", "ondotrip", "gemini-3.8-flash-high", "plan")
        updated = store.get_thread_session("C1", "100.1")
        assert updated is not None
        assert updated.conversation_id == "conv-xyz"
        assert updated.project_key == "ondotrip"

        store.set_channel_setting("C1", "traillog", "gemini-4-neon-medium", "plan")
        ch = store.get_channel_setting("C1")
        assert ch is not None
        assert ch.project_key == "traillog"
        assert ch.mode == "plan"
    finally:
        store.close()


def test_jobs_schedules_and_dynamic_projects(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    try:
        store.create_job("j1", "ai", "slack_autobot", "테스트 프롬프트", "U1", "C1", "t1", "logs/j1.log")
        store.finish_job("j1", "success", "완료됨", 3.2, conversation_id="conv-1")
        job = store.get_job("j1")
        assert job is not None
        assert job.status == "success"
        assert job.duration_sec == 3.2
        assert len(store.list_recent_jobs(5)) == 1

        sched = store.add_schedule("10:00", "run", "ondotrip", "ondotrip", "C1", "U1")
        assert sched.enabled is True
        assert len(store.list_schedules(only_enabled=True)) == 1

        assert store.set_schedule_enabled(sched.schedule_id, False) is True
        assert len(store.list_schedules(only_enabled=True)) == 0
        assert store.set_schedule_enabled(sched.schedule_id, True) is True
        store.mark_schedule_ran(sched.schedule_id, "2026-11-23")
        reloaded = store.get_schedule(sched.schedule_id)
        assert reloaded is not None
        assert reloaded.last_run_date == "2026-11-23"

        assert store.delete_schedule(sched.schedule_id) is True
        assert store.list_schedules() == []

        store.save_dynamic_project("mybot", str(tmp_path / "MyBot"), "설명")
        dyn = store.list_dynamic_projects()
        assert len(dyn) == 1
        assert dyn[0].key == "mybot"
    finally:
        store.close()
