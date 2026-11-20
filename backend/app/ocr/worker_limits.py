"""Native worker resource boundaries without web, PDF or model imports."""

from __future__ import annotations

import sys
from math import ceil

from app.core.exceptions import ApiError

_WORKER_RESOURCE_FAILURE = {
    "ok": False,
    "code": "WORKER_RESOURCE_LIMIT",
    "message": "本地文件处理超过资源限制",
    "status": 422,
}


def apply_worker_limits(raw_limits: dict[str, int]) -> None:
    """Apply process limits before decoding untrusted files on Linux workers."""

    if sys.platform != "linux":
        return
    try:
        import resource

        memory = int(raw_limits["memory_bytes"])
        file_size = int(raw_limits["file_size_bytes"])
        open_files = int(raw_limits["open_files"])
        cpu_seconds = int(raw_limits["cpu_seconds"])

        def bounded_soft(resource_id: int, requested: int) -> tuple[int, int]:
            _old_soft, hard = resource.getrlimit(resource_id)
            soft = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
            return soft, hard

        resource.setrlimit(resource.RLIMIT_AS, bounded_soft(resource.RLIMIT_AS, memory))
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            bounded_soft(resource.RLIMIT_FSIZE, file_size),
        )
        _nofile_soft, nofile_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        nofile_soft = min(open_files, nofile_hard) if nofile_hard >= 0 else open_files
        resource.setrlimit(resource.RLIMIT_NOFILE, (nofile_soft, nofile_hard))
        _core_soft, core_hard = resource.getrlimit(resource.RLIMIT_CORE)
        resource.setrlimit(resource.RLIMIT_CORE, (0, core_hard))
        usage = resource.getrusage(resource.RUSAGE_SELF)
        cpu_soft = ceil(usage.ru_utime + usage.ru_stime) + cpu_seconds
        _old_soft, cpu_hard = resource.getrlimit(resource.RLIMIT_CPU)
        if cpu_hard >= 0:
            cpu_soft = min(cpu_soft, cpu_hard)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_soft, cpu_hard))
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise ApiError("WORKER_LIMIT_SETUP_FAILED", "本地文件处理资源限制初始化失败", 500) from exc


def worker_resource_failure() -> dict[str, object]:
    """Return a sanitized marker for worker-side allocation failure."""

    return dict(_WORKER_RESOURCE_FAILURE)


def safe_worker_failure(exc: ApiError) -> dict[str, object]:
    return {"ok": False, "code": exc.code, "message": exc.message, "status": exc.status_code}
