"""SSE fan-out for MAX events — a separate StreamManager instance.

The TG stream_manager has no alias filter; MAX events must never reach TG
subscribers and vice versa (ADR §1.1 p.5).
"""

from app.services.stream_service import StreamManager

max_stream_manager = StreamManager()
