"""SQLite 기반 세션, 채널 설정, 작업 이력, 예약 및 동적 프로젝트 영속 저장소."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _utc_now_iso() -> str:
    """현재 UTC 시각을 ISO-8601 문자열로 반환한다."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class ThreadSession:
    """Slack 스레드와 Antigravity 대화 세션(`conversation_id`) 매핑 정보."""

    channel_id: str
    thread_ts: str
    conversation_id: str
    project_key: str
    model: str
    mode: str
    created_at: str
    updated_at: str


@dataclass(slots=True)
class ChannelSetting:
    """채널별 현재 선택된 프로젝트, AI 모델 및 실행 모드."""

    channel_id: str
    project_key: str
    model: str
    mode: str
    updated_at: str


@dataclass(slots=True)
class JobRecord:
    """AI 개발 및 프리셋 작업 실행 이력 레코드."""

    job_id: str
    kind: str
    project_key: str
    target: str
    user_id: str
    channel_id: str
    thread_ts: str
    status: str
    summary: str
    log_path: str
    started_at: str
    finished_at: str
    duration_sec: float
    conversation_id: str


@dataclass(slots=True)
class ScheduleRecord:
    """내장 Slack 예약 작업 레코드."""

    schedule_id: int
    time_hhmm: str
    kind: str
    project_key: str
    payload: str
    channel_id: str
    enabled: bool
    last_run_date: str
    created_by: str
    created_at: str


@dataclass(slots=True)
class DynamicProjectRecord:
    """런타임 중 추가된 동적 프로젝트 레코드."""

    key: str
    path: str
    description: str
    created_at: str


class StateStore:
    """스레드 안전한 SQLite 상태 저장소."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        """필요한 테이블과 WAL 모드를 초기화한다."""
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS thread_sessions (
                    channel_id TEXT NOT NULL,
                    thread_ts TEXT NOT NULL,
                    conversation_id TEXT NOT NULL DEFAULT '',
                    project_key TEXT NOT NULL,
                    model TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (channel_id, thread_ts)
                );

                CREATE TABLE IF NOT EXISTS channel_settings (
                    channel_id TEXT PRIMARY KEY,
                    project_key TEXT NOT NULL,
                    model TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS job_history (
                    job_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    project_key TEXT NOT NULL,
                    target TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    thread_ts TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    log_path TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT '',
                    duration_sec REAL NOT NULL DEFAULT 0.0,
                    conversation_id TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS schedules (
                    schedule_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time_hhmm TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    project_key TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_run_date TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS dynamic_projects (
                    key TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    def close(self) -> None:
        """DB 연결을 종료한다."""
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # 스레드 세션 관리
    # ------------------------------------------------------------------
    def get_thread_session(self, channel_id: str, thread_ts: str) -> ThreadSession | None:
        """채널과 스레드 타임스탬프로 기존 AI 대화 세션을 조회한다."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT channel_id, thread_ts, conversation_id, project_key, model, mode, created_at, updated_at
                FROM thread_sessions
                WHERE channel_id = ? AND thread_ts = ?
                """,
                (channel_id, thread_ts),
            ).fetchone()
            if not row:
                return None
            return ThreadSession(
                channel_id=row["channel_id"],
                thread_ts=row["thread_ts"],
                conversation_id=row["conversation_id"],
                project_key=row["project_key"],
                model=row["model"],
                mode=row["mode"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def upsert_thread_session(
        self,
        channel_id: str,
        thread_ts: str,
        conversation_id: str,
        project_key: str,
        model: str,
        mode: str,
    ) -> ThreadSession:
        """스레드 세션을 생성하거나 갱신한다."""
        now = _utc_now_iso()
        with self._lock:
            existing = self.get_thread_session(channel_id, thread_ts)
            created_at = existing.created_at if existing else now
            self._conn.execute(
                """
                INSERT INTO thread_sessions (
                    channel_id, thread_ts, conversation_id, project_key, model, mode, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id, thread_ts) DO UPDATE SET
                    conversation_id = excluded.conversation_id,
                    project_key = excluded.project_key,
                    model = excluded.model,
                    mode = excluded.mode,
                    updated_at = excluded.updated_at
                """,
                (channel_id, thread_ts, conversation_id, project_key, model, mode, created_at, now),
            )
            self._conn.commit()
            return ThreadSession(
                channel_id=channel_id,
                thread_ts=thread_ts,
                conversation_id=conversation_id,
                project_key=project_key,
                model=model,
                mode=mode,
                created_at=created_at,
                updated_at=now,
            )

    # ------------------------------------------------------------------
    # 채널 설정 관리
    # ------------------------------------------------------------------
    def get_channel_setting(self, channel_id: str) -> ChannelSetting | None:
        """채널별 저장된 기본 설정을 조회한다."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT channel_id, project_key, model, mode, updated_at
                FROM channel_settings
                WHERE channel_id = ?
                """,
                (channel_id,),
            ).fetchone()
            if not row:
                return None
            return ChannelSetting(
                channel_id=row["channel_id"],
                project_key=row["project_key"],
                model=row["model"],
                mode=row["mode"],
                updated_at=row["updated_at"],
            )

    def set_channel_setting(
        self,
        channel_id: str,
        project_key: str,
        model: str,
        mode: str,
    ) -> ChannelSetting:
        """채널별 프로젝트/모델/모드 설정을 저장한다."""
        now = _utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO channel_settings (channel_id, project_key, model, mode, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    project_key = excluded.project_key,
                    model = excluded.model,
                    mode = excluded.mode,
                    updated_at = excluded.updated_at
                """,
                (channel_id, project_key, model, mode, now),
            )
            self._conn.commit()
            return ChannelSetting(
                channel_id=channel_id,
                project_key=project_key,
                model=model,
                mode=mode,
                updated_at=now,
            )

    # ------------------------------------------------------------------
    # 작업 이력 관리
    # ------------------------------------------------------------------
    def create_job(
        self,
        job_id: str,
        kind: str,
        project_key: str,
        target: str,
        user_id: str,
        channel_id: str,
        thread_ts: str = "",
        log_path: str = "",
    ) -> JobRecord:
        """새 작업 시작 이력을 기록한다."""
        now = _utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO job_history (
                    job_id, kind, project_key, target, user_id, channel_id, thread_ts,
                    status, summary, log_path, started_at, finished_at, duration_sec, conversation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', '', ?, ?, '', 0.0, '')
                """,
                (job_id, kind, project_key, target, user_id, channel_id, thread_ts, log_path, now),
            )
            self._conn.commit()
            return JobRecord(
                job_id=job_id,
                kind=kind,
                project_key=project_key,
                target=target,
                user_id=user_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                status="running",
                summary="",
                log_path=log_path,
                started_at=now,
                finished_at="",
                duration_sec=0.0,
                conversation_id="",
            )

    def finish_job(
        self,
        job_id: str,
        status: str,
        summary: str = "",
        duration_sec: float = 0.0,
        log_path: str = "",
        conversation_id: str = "",
    ) -> None:
        """작업 완료 상태와 결과를 기록한다."""
        now = _utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                UPDATE job_history
                SET status = ?,
                    summary = ?,
                    finished_at = ?,
                    duration_sec = ?,
                    log_path = CASE WHEN ? != '' THEN ? ELSE log_path END,
                    conversation_id = CASE WHEN ? != '' THEN ? ELSE conversation_id END
                WHERE job_id = ?
                """,
                (
                    status,
                    summary,
                    now,
                    duration_sec,
                    log_path,
                    log_path,
                    conversation_id,
                    conversation_id,
                    job_id,
                ),
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> JobRecord | None:
        """특정 작업 이력을 조회한다."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM job_history WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            return self._row_to_job(row) if row else None

    def list_recent_jobs(self, limit: int = 10) -> list[JobRecord]:
        """최근 작업 이력을 최신순으로 조회한다."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM job_history ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._row_to_job(r) for r in rows]

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            job_id=row["job_id"],
            kind=row["kind"],
            project_key=row["project_key"],
            target=row["target"],
            user_id=row["user_id"],
            channel_id=row["channel_id"],
            thread_ts=row["thread_ts"],
            status=row["status"],
            summary=row["summary"],
            log_path=row["log_path"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            duration_sec=float(row["duration_sec"]),
            conversation_id=row["conversation_id"],
        )

    # ------------------------------------------------------------------
    # 내장 예약 작업 관리
    # ------------------------------------------------------------------
    def add_schedule(
        self,
        time_hhmm: str,
        kind: str,
        project_key: str,
        payload: str,
        channel_id: str,
        created_by: str = "",
    ) -> ScheduleRecord:
        """새 일일 예약 작업을 등록한다."""
        now = _utc_now_iso()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO schedules (
                    time_hhmm, kind, project_key, payload, channel_id, enabled, last_run_date, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, 1, '', ?, ?)
                """,
                (time_hhmm, kind, project_key, payload, channel_id, created_by, now),
            )
            self._conn.commit()
            schedule_id = int(cur.lastrowid)
            return ScheduleRecord(
                schedule_id=schedule_id,
                time_hhmm=time_hhmm,
                kind=kind,
                project_key=project_key,
                payload=payload,
                channel_id=channel_id,
                enabled=True,
                last_run_date="",
                created_by=created_by,
                created_at=now,
            )

    def list_schedules(self, only_enabled: bool = False) -> list[ScheduleRecord]:
        """등록된 예약 목록을 조회한다."""
        with self._lock:
            sql = "SELECT * FROM schedules"
            if only_enabled:
                sql += " WHERE enabled = 1"
            sql += " ORDER BY time_hhmm ASC, schedule_id ASC"
            rows = self._conn.execute(sql).fetchall()
            return [self._row_to_schedule(r) for r in rows]

    def get_schedule(self, schedule_id: int) -> ScheduleRecord | None:
        """ID로 예약을 조회한다."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM schedules WHERE schedule_id = ?",
                (schedule_id,),
            ).fetchone()
            return self._row_to_schedule(row) if row else None

    def set_schedule_enabled(self, schedule_id: int, enabled: bool) -> bool:
        """예약 활성화/비활성화 상태를 변경한다."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE schedules SET enabled = ? WHERE schedule_id = ?",
                (1 if enabled else 0, schedule_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def delete_schedule(self, schedule_id: int) -> bool:
        """예약 작업을 삭제한다."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM schedules WHERE schedule_id = ?",
                (schedule_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def mark_schedule_ran(self, schedule_id: int, date_str: str) -> None:
        """예약 작업이 오늘 실행되었음을 기록한다."""
        with self._lock:
            self._conn.execute(
                "UPDATE schedules SET last_run_date = ? WHERE schedule_id = ?",
                (date_str, schedule_id),
            )
            self._conn.commit()

    @staticmethod
    def _row_to_schedule(row: sqlite3.Row) -> ScheduleRecord:
        return ScheduleRecord(
            schedule_id=int(row["schedule_id"]),
            time_hhmm=row["time_hhmm"],
            kind=row["kind"],
            project_key=row["project_key"],
            payload=row["payload"],
            channel_id=row["channel_id"],
            enabled=bool(row["enabled"]),
            last_run_date=row["last_run_date"],
            created_by=row["created_by"],
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------
    # 동적 프로젝트 관리
    # ------------------------------------------------------------------
    def save_dynamic_project(self, key: str, path: str, description: str = "") -> DynamicProjectRecord:
        """Slack에서 생성된 동적 프로젝트를 저장한다."""
        now = _utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO dynamic_projects (key, path, description, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    path = excluded.path,
                    description = excluded.description
                """,
                (key, path, description, now),
            )
            self._conn.commit()
            return DynamicProjectRecord(
                key=key,
                path=path,
                description=description,
                created_at=now,
            )

    def list_dynamic_projects(self) -> list[DynamicProjectRecord]:
        """저장된 동적 프로젝트 목록을 조회한다."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM dynamic_projects ORDER BY key ASC"
            ).fetchall()
            return [
                DynamicProjectRecord(
                    key=r["key"],
                    path=r["path"],
                    description=r["description"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]
