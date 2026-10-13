"""
Simple in-process background job tracker.

Each long-running action (download, split, convert) runs in its own thread
and reports progress into a dict that the browser polls over HTTP. This is
intentionally lightweight — fine for a single-user personal tool, not meant
to survive multiple server processes (keep the web server to 1 process /
many threads, which the provided Dockerfile + Procfile already set up).
"""

import threading
import uuid
import time

_jobs = {}
_lock = threading.Lock()


def create_job(kind):
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "kind": kind,
            "status": "running",       # running | done | error
            "progress": 0,             # 0-100
            "message": "Starting...",
            "log": [],
            "result": None,            # e.g. {"files": [...], "zip": "..."}
            "error": None,
            "created_at": time.time(),
        }
    return job_id


def update_job(job_id, **fields):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(fields)


def append_log(job_id, line):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["log"].append(line)
            # keep log from growing unbounded
            if len(_jobs[job_id]["log"]) > 500:
                _jobs[job_id]["log"] = _jobs[job_id]["log"][-500:]


def get_job(job_id):
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def finish_job(job_id, result=None):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["progress"] = 100
            _jobs[job_id]["result"] = result


def fail_job(job_id, error_message):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = error_message


def run_in_background(target, *args, **kwargs):
    thread = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
    thread.start()
    return thread
