from __future__ import annotations

import os

import structlog

from boltz2_service.config import get_settings
from platform_core.db import init_db
from boltz2_service.worker.job_processor import JobProcessor
from boltz2_service.worker.queue_consumer import QueueConsumer

logger = structlog.get_logger(__name__)


def main() -> int:
    init_db(create_tables=False)
    settings = get_settings()
    consumer = QueueConsumer(settings)
    try:
        message = consumer.receive_one()
        if message is None:
            logger.info("no_message_available")
            return 0

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
        logger.info("job_processing_finished", job_id=job_id)
        return 0
    finally:
        consumer.close()


if __name__ == "__main__":
    raise SystemExit(main())
