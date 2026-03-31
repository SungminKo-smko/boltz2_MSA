from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from azure.servicebus import AutoLockRenewer, ServiceBusClient

from boltz2_service.config import Boltz2Settings


@dataclass
class ConsumedMessage:
    body: dict
    ack_token: object | None = None


class QueueConsumer:
    def __init__(self, settings: Boltz2Settings) -> None:
        self.settings = settings
        self.local_path = (
            Path(settings.local_storage_root)
            / "queue"
            / f"{settings.service_bus_queue_name}.jsonl"
        )
        self._client: ServiceBusClient | None = None
        self._receiver = None
        self._lock_renewer: AutoLockRenewer | None = None

    def _ensure_receiver(self):
        if self._receiver is not None:
            return self._receiver
        if not self.settings.service_bus_connection_string:
            raise ValueError(
                "SERVICE_BUS_CONNECTION_STRING is required when queue_backend='azure'"
            )
        self._client = ServiceBusClient.from_connection_string(
            self.settings.service_bus_connection_string
        )
        self._client.__enter__()
        self._receiver = self._client.get_queue_receiver(
            self.settings.service_bus_queue_name, max_wait_time=10
        )
        self._receiver.__enter__()
        self._lock_renewer = AutoLockRenewer()
        return self._receiver

    def receive_one(self) -> ConsumedMessage | None:
        if self.settings.queue_backend == "azure":
            receiver = self._ensure_receiver()
            messages = receiver.receive_messages(max_message_count=1, max_wait_time=10)
            if not messages:
                return None
            message = messages[0]
            if self._lock_renewer is not None:
                self._lock_renewer.register(
                    receiver,
                    message,
                    max_lock_renewal_duration=self.settings.boltz2_run_timeout_seconds,
                )
            payload = json.loads(b"".join(message.body).decode("utf-8"))
            return ConsumedMessage(body=payload, ack_token=message)

        if not self.local_path.exists():
            return None
        lines = self.local_path.read_text(encoding="utf-8").splitlines()
        if not lines:
            return None
        first, rest = lines[0], lines[1:]
        self.local_path.write_text(
            "\n".join(rest) + ("\n" if rest else ""), encoding="utf-8"
        )
        return ConsumedMessage(body=json.loads(first))

    def ack(self, consumed: ConsumedMessage) -> None:
        if consumed.ack_token is None:
            return
        if self._receiver is None:
            raise RuntimeError("Queue receiver is not available for ack.")
        self._receiver.complete_message(consumed.ack_token)

    def close(self) -> None:
        if self._lock_renewer is not None:
            self._lock_renewer.close()
            self._lock_renewer = None
        if self._receiver is not None:
            self._receiver.__exit__(None, None, None)
            self._receiver = None
        if self._client is not None:
            self._client.__exit__(None, None, None)
            self._client = None
