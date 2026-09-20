"""
WorkerTransport implementations for supervisor-to-agent communication.

Implementations:
- NativeStreamTransport: persistent subprocess with JSON/JSONL stdin/stdout
- ResumeTransport: per-event subprocess fallback
"""

from surface.transports.native_stream import NativeStreamTransport
from surface.transports.resume import ResumeTransport

__all__ = ["NativeStreamTransport", "ResumeTransport"]
