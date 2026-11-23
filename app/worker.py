import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import logging
import signal
import time

from app.config import get_settings
from app.database import SessionLocal
from app.heartbeat import record_heartbeat
from app.judging.judge0 import Judge0Client
from app.judging.service import GradingService


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
_stop_requested = False


def _request_stop(signum: int, _frame: object) -> None:
    global _stop_requested
    logger.info("grading_worker_stop_requested signal=%s", signum)
    _stop_requested = True


def _safe_heartbeat(workers: int) -> None:
    try:
        record_heartbeat("grading", {"status": "running", "workers": workers})
    except Exception:
        logger.exception("grading_worker_heartbeat_failed")


def _safe_process_next(service: GradingService) -> bool:
    try:
        return bool(service.process_next())
    except Exception:
        logger.exception("grading_worker_unhandled_error")
        return False


def run(*, once: bool, idle_seconds: float, concurrency: int | None = None) -> None:
    settings = get_settings()
    workers = max(
        1, concurrency if concurrency is not None else settings.worker_concurrency
    )
    judge = Judge0Client(settings)
    service = GradingService(
        SessionLocal,
        judge,
        stale_run_timeout_seconds=settings.grading_stale_run_timeout_seconds,
    )
    last_heartbeat = 0.0
    try:
        if once or workers == 1:
            while not _stop_requested:
                if not once:
                    monotonic_now = time.monotonic()
                    if monotonic_now - last_heartbeat >= 10:
                        _safe_heartbeat(1)
                        last_heartbeat = monotonic_now
                processed = _safe_process_next(service)
                if once:
                    return
                if not processed and not _stop_requested:
                    time.sleep(idle_seconds)
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                in_flight: set[Future[bool]] = set()
                while not _stop_requested or in_flight:
                    if not _stop_requested:
                        monotonic_now = time.monotonic()
                        if monotonic_now - last_heartbeat >= 10:
                            _safe_heartbeat(workers)
                            last_heartbeat = monotonic_now
                    while not _stop_requested and len(in_flight) < workers:
                        in_flight.add(pool.submit(_safe_process_next, service))
                    if not in_flight:
                        break
                    done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                    any_processed = any(bool(f.result()) for f in done)
                    if not any_processed and not _stop_requested:
                        if in_flight:
                            more_done, in_flight = wait(in_flight, timeout=0.05)
                            if any(bool(f.result()) for f in more_done):
                                continue
                        time.sleep(idle_seconds)
    finally:
        judge.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Worker chấm bài qua Judge0 (hỗ trợ xử lý song song FOR UPDATE SKIP LOCKED)"
    )
    parser.add_argument("--once", action="store_true", help="Xử lý tối đa một lượt rồi thoát")
    parser.add_argument("--idle-seconds", type=float, default=1.0)
    parser.add_argument("--concurrency", type=int, default=None, help="Số luồng chấm bài song song")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    logger.info(
        "grading_worker_started once=%s concurrency=%s", args.once, args.concurrency
    )
    run(once=args.once, idle_seconds=args.idle_seconds, concurrency=args.concurrency)


if __name__ == "__main__":
    main()
