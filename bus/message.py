"""
Define massage format in system
"""
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MessageBody:
    content: str
    attachments: list


@dataclass
class Message:
    id: str
    session_id: str
    sender: str
    recipient: str
    subject: str
    body: MessageBody
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

