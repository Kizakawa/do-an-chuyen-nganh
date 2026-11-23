from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert

from app.database import SessionLocal
from app.models import WorkerHeartbeat


def record_heartbeat(worker_name: str, details: dict | None = None) -> None:
    now = datetime.now(UTC)
    statement = insert(WorkerHeartbeat).values(
        worker_name=worker_name, last_seen_at=now, details=details or {}
    )
    with SessionLocal.begin() as db:
        db.execute(
            statement.on_conflict_do_update(
                index_elements=[WorkerHeartbeat.worker_name],
                set_={"last_seen_at": now, "details": details or {}},
            )
        )
