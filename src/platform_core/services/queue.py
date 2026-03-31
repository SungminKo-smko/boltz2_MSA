from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from azure.servicebus import ServiceBusClient, ServiceBusMessage

from platform_core.config import PlatformSettings


@dataclass
class QueueSendResult:
    message_id: str


class QueueService:
    def __init__(self, settings: PlatformSettings, queue_name: str) -> None:
        self.settings = settings
        self.queue_name = queue_name
        self.local_path = Path(settings.local_storage_root) / "queue" / f"{queue_name}.jsonl"
        if settings.queue_backend == "azure":
            self.client = ServiceBusClient.from_connection_string(settings.service_bus_connection_string)
        else:
            self.client = None
            self.local_path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, payload: dict) -> QueueSendResult:
        message_id = str(uuid4())
        payload = {**payload, "message_id": message_id}
        if self.settings.queue_backend == "azure":
            with self.client.get_queue_sender(self.queue_name) as sender:
                sender.send_messages(ServiceBusMessage(json.dumps(payload), message_id=message_id))
            return QueueSendResult(message_id=message_id)

        with self.local_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
        return QueueSendResult(message_id=message_id)
