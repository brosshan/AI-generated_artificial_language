from __future__ import annotations

import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_JOB_EVENTS: int = 100
DEFAULT_MAX_QUEUE_SIZE: int = 10


class QueueFullError(Exception):
    """當背景執行緒佇列達到上限時拋出。"""
    pass


class BoundedThreadPoolExecutor(ThreadPoolExecutor):
    """具備工作佇列長度上限的執行緒池，防止 DoS 資源耗盡。"""

    def __init__(self, max_workers: int = 1, max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE, **kwargs: Any) -> None:
        super().__init__(max_workers=max_workers, **kwargs)
        self.max_queue_size = max_queue_size

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        if self._work_queue.qsize() >= self.max_queue_size:
            raise QueueFullError(f"背景工作佇列已達上限 ({self.max_queue_size})，請稍後再試。")
        return super().submit(fn, *args, **kwargs)


@dataclass
class Job:
    job_id: str
    filename: str
    root: Path
    status: str = "PROCESSING"
    stage: str = "DOCUMENT_PARSING"
    message: str = "正在讀取簡報..."
    page_count: int = 0
    current_step: int = 0
    total_steps: int = 0
    progress_percentage: float = 0
    error: str | None = None
    condition: threading.Condition = field(default_factory=threading.Condition)
    events: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.publish()

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "filename": self.filename,
            "status": self.status,
            "stage": self.stage,
            "message": self.message,
            "page_count": self.page_count,
            "current_step": self.current_step,
            "total_steps": self.total_steps,
            "progress_percentage": self.progress_percentage,
            "error": self.error,
        }

    def _record_event_locked(self) -> None:
        self.events.append(self.snapshot())
        if len(self.events) > MAX_JOB_EVENTS:
            self.events = self.events[-MAX_JOB_EVENTS:]

    def publish(self) -> None:
        with self.condition:
            self._record_event_locked()
            self.condition.notify_all()

    def update(self, **values: Any) -> None:
        with self.condition:
            for name, value in values.items():
                setattr(self, name, value)
            self._record_event_locked()
            self.condition.notify_all()

    def begin(self, expected_statuses: set[str], stage: str, message: str) -> bool:
        with self.condition:
            if self.status not in expected_statuses:
                return False
            self.status = "PROCESSING"
            self.stage = stage
            self.message = message
            self.error = None
            self._record_event_locked()
            self.condition.notify_all()
            return True


class JobManager:
    def __init__(self, max_jobs: int = 50, max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self.max_jobs = max_jobs
        self.max_queue_size = max_queue_size
        self.executor = BoundedThreadPoolExecutor(
            max_workers=1,
            max_queue_size=max_queue_size,
            thread_name_prefix="presentation",
        )

    def _evict_oldest_locked(self) -> None:
        while len(self._jobs) > self.max_jobs:
            evict_candidates = [
                j for j in self._jobs.values() if j.status in {"COMPLETED", "FAILED"}
            ]
            target = (
                evict_candidates[0]
                if evict_candidates
                else next(iter(self._jobs.values()))
            )
            del self._jobs[target.job_id]
            shutil.rmtree(target.root, ignore_errors=True)

    def create(self, filename: str, workspace: Path) -> Job:
        job_id = str(uuid.uuid4())
        root = workspace / job_id
        job = Job(job_id=job_id, filename=filename, root=root)
        with self._lock:
            self._jobs[job_id] = job
            self._evict_oldest_locked()
        return job

    def get(self, job_id: str) -> Job | None:
        try:
            uuid.UUID(job_id)
        except (ValueError, AttributeError):
            return None
        with self._lock:
            return self._jobs.get(job_id)

    def delete(self, job_id: str) -> bool:
        try:
            uuid.UUID(job_id)
        except (ValueError, AttributeError):
            return False
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job:
            shutil.rmtree(job.root, ignore_errors=True)
            return True
        return False

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            current_jobs = list(self._jobs.values())
        return [job.snapshot() for job in reversed(current_jobs)]


jobs = JobManager()


def event_stream(job: Job):
    event_index = 0
    while True:
        with job.condition:
            if event_index >= len(job.events) and job.status in {"COMPLETED", "FAILED"}:
                return
            if event_index >= len(job.events):
                event_index = len(job.events)
                job.condition.wait(timeout=15)
            if event_index < len(job.events):
                snapshot = job.events[event_index]
                event_index += 1
            else:
                snapshot = None
                is_terminal = job.status in {"COMPLETED", "FAILED"}
        if snapshot is None:
            if is_terminal:
                return
            yield ": keepalive\n\n"
        else:
            import json

            yield f"event: update\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
