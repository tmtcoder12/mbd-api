"""Retrieval-augmented response orchestration used by the chat service."""

from .core import (
    DEFAULT_SESSION_LANGUAGE,
    DEFAULT_SYSTEM_INSTRUCTIONS,
    MAX_SOURCES_SENT,
    Retriever,
    build_image_payload_from_decision,
    handle_chat_turn,
)
from .core import (
    _copy_session_state as copy_session_state,
)

__all__ = [
    "DEFAULT_SESSION_LANGUAGE",
    "DEFAULT_SYSTEM_INSTRUCTIONS",
    "MAX_SOURCES_SENT",
    "Retriever",
    "copy_session_state",
    "build_image_payload_from_decision",
    "handle_chat_turn",
]
