"""표준 라이브러리 threading 기반 내장 일일 스케줄러 서비스."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Callable

from src.state_store import ScheduleRecord, StateStore

logger = logging.getLogger(__name__)


class SchedulerService:
    """등록된 일일 예약 작업을 주기적으로 확인해 실행하는 경량 스케줄러."""

    def __init__(
        self,
        store: StateStore,
        on_trigger: Callable[[ScheduleRecord], None],
        poll_interval_sec: float = 15.0,
    ) -> None:
        self.store = store
        self.on_trigger = on_trigger
        self.poll_interval_sec = poll_interval_sec
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """백그라운드 스케줄러 폴링 스레드를 시작한다."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="SlackSchedulerService",
            daemon=True,
        )
        self._thread.start()
        logger.info("내장 일일 스케줄러가 시작되었습니다 (점검 주기: %.1f초).", self.poll_interval_sec)

    def stop(self) -> None:
        """스케줄러 스레드를 안전하게 정지한다."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None

    def _run_loop(self) -> None:
        """정지 신호가 올 때까지 주기적으로 예약을 검사한다."""
        while not self._stop_event.is_set():
            try:
                self.tick_once(datetime.now())
            except Exception as exc:
                logger.exception("스케줄러 주기 점검 중 오류 발생: %s", exc)
            self._stop_event.wait(self.poll_interval_sec)

    def tick_once(self, now: datetime) -> list[ScheduleRecord]:
        """지정된 시각을 기준으로 실행 대상 예약을 찾아 1회만 트리거한다."""
        current_hhmm = now.strftime("%H:%M")
        today_str = now.strftime("%Y-%m-%d")

        triggered: list[ScheduleRecord] = []
        for sched in self.store.list_schedules(only_enabled=True):
            if sched.time_hhmm != current_hhmm:
                continue
            if sched.last_run_date == today_str:
                continue

            # 중복 실행 방지를 위해 실행 직전 오늘 날짜를 먼저 마킹
            self.store.mark_schedule_ran(sched.schedule_id, today_str)
            triggered.append(sched)

            worker = threading.Thread(
                target=self._safe_invoke,
                args=(sched,),
                name=f"ScheduleWorker-{sched.schedule_id}",
                daemon=True,
            )
            worker.start()

        return triggered

    def _safe_invoke(self, sched: ScheduleRecord) -> None:
        """콜백 예외가 스케줄러 루프를 깨지 않도록 감싸서 호출한다."""
        try:
            self.on_trigger(sched)
        except Exception as exc:
            logger.exception("예약 작업(#%s) 실행 콜백 오류: %s", sched.schedule_id, exc)
