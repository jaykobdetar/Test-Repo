"""Fixed timing shared by public calibration startup and its stop watchdog."""
import math


COLLECTION_DELETION_RESERVE_SECONDS = 120


def startup_dispatch_cutoff(absolute_deadline: float, job_runtime_seconds: int) -> float:
    """Reserve the entire job and teardown inside the original approval.

    This is an absolute cutoff, never a grace period starting at a poll, restart,
    ready notification or provider observation. A cutoff in the past is expired.
    """
    if (type(absolute_deadline) not in (int, float) or not math.isfinite(absolute_deadline)
            or absolute_deadline <= 0 or type(job_runtime_seconds) is not int
            or not 1 <= job_runtime_seconds <= 86400):
        raise ValueError('invalid fixed startup timing')
    return float(absolute_deadline) - job_runtime_seconds - COLLECTION_DELETION_RESERVE_SECONDS
