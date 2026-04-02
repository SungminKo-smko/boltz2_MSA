from __future__ import annotations

import os
import signal
import sys

import structlog

from boltz2_service.config import get_settings
from platform_core.db import init_db
from boltz2_service.worker.job_processor import JobProcessor
from boltz2_service.worker.queue_consumer import QueueConsumer

logger = structlog.get_logger(__name__)

_current_consumer: QueueConsumer | None = None
_current_message = None


def _sigterm_handler(signum, frame):  # noqa: ARG001
    logger.info("graceful_shutdown_requested")
    if _current_consumer is not None and _current_message is not None:
        try:
            _current_consumer.ack(_current_message)
        except Exception:  # noqa: BLE001
            pass
    sys.exit(0)


def main() -> int:
    global _current_consumer, _current_message  # noqa: PLW0603

    init_db(create_tables=False)
    settings = get_settings()
    consumer = QueueConsumer(settings)
    _current_consumer = consumer

    signal.signal(signal.SIGTERM, _sigterm_handler)

    try:
        message = consumer.receive_one()
        if message is None:
            logger.info("no_message_available")
            return 0
        _current_message = message

        job_id = message.body["job_id"]
        logger.info("processing_job", job_id=job_id)

        JobProcessor(settings).process(
            job_id,
            pod_name=os.getenv("POD_NAME"),
            job_name=(
                os.getenv("CONTAINER_APP_JOB_EXECUTION_NAME")
                or os.getenv("JOB_NAME")
            ),
        )

        consumer.ack(message)
        _current_message = None
        logger.info("job_processing_finished", job_id=job_id)
        return 0
    finally:
        consumer.close()


if __name__ == "__main__":
    raise SystemExit(main())
