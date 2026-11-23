import argparse
import logging
import signal
import time

from app.config import get_settings
from app.database import SessionLocal
from app.hints.service import HintOrchestrationService
from app.heartbeat import record_heartbeat


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)
_stop_requested = False


def _request_stop(signum: int, _frame: object) -> None:
    global _stop_requested
    logger.info("hint_worker_stop_requested signal=%s", signum)
    _stop_requested = True


def run(*, once: bool, idle_seconds: float) -> None:
    service = HintOrchestrationService(SessionLocal, settings=get_settings())
    last_heartbeat = 0.0
    try:
        while not _stop_requested:
            if not once:
                monotonic_now = time.monotonic()
                if monotonic_now - last_heartbeat >= 10:
                    try:
                        record_heartbeat("hint", {"status": "running"})
                        last_heartbeat = monotonic_now
                    except Exception:
                        logger.exception("hint_worker_heartbeat_failed")
            try:
                processed = service.process_next()
            except Exception:
                logger.exception("hint_worker_loop_error")
                processed = False
            if once:
                return
            if not processed and not _stop_requested:
                time.sleep(idle_seconds)
    finally:
        close_fn = getattr(service.adapter, "close", None)
        if callable(close_fn):
            close_fn()


def main() -> None:
    parser = argparse.ArgumentParser(description="Worker tạo gợi ý AI từ hàng đợi PostgreSQL")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--idle-seconds", type=float, default=1.0)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    logger.info("hint_worker_started once=%s", args.once)
    run(once=args.once, idle_seconds=args.idle_seconds)


if __name__ == "__main__":
    main()
