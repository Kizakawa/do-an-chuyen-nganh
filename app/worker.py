import argparse
import logging
import signal
import time

from app.config import get_settings
from app.database import SessionLocal
from app.judging.judge0 import Judge0Client
from app.judging.service import GradingService
from app.heartbeat import record_heartbeat


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
_stop_requested = False


def _request_stop(signum: int, _frame: object) -> None:
    global _stop_requested
    logger.info("grading_worker_stop_requested signal=%s", signum)
    _stop_requested = True


def run(*, once: bool, idle_seconds: float) -> None:
    settings = get_settings()
    judge = Judge0Client(settings)
    service = GradingService(
        SessionLocal,
        judge,
        stale_run_timeout_seconds=settings.grading_stale_run_timeout_seconds,
    )
    last_heartbeat = 0.0
    while not _stop_requested:
        monotonic_now = time.monotonic()
        if monotonic_now - last_heartbeat >= 10:
            record_heartbeat("grading", {"status": "running"})
            last_heartbeat = monotonic_now
        processed = service.process_next()
        if once:
            return
        if not processed:
            time.sleep(idle_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Worker chấm bài qua Judge0")
    parser.add_argument("--once", action="store_true", help="Xử lý tối đa một lượt rồi thoát")
    parser.add_argument("--idle-seconds", type=float, default=1.0)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    logger.info("grading_worker_started once=%s", args.once)
    run(once=args.once, idle_seconds=args.idle_seconds)


if __name__ == "__main__":
    main()
