"""Bounded, correlated socket transport for the Ableton Remote Script.

Each connection has one exchange owner. A failed exchange is never replayed:
its request ID is included in the error so callers can reconcile uncertain
writes using ``get_command_status`` after reconnecting.
"""

import json
import logging
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("AbletonMCPServer.transport")
PROTOCOL_VERSION = "2.1"
MAX_FRAME_BYTES = 8 * 1024 * 1024


class CommandError(Exception):
    """A request failed or its outcome needs reconciliation."""

    def __init__(self, message, request_id=None, outcome="unknown", details=None):
        self.request_id = request_id
        self.outcome = outcome
        self.details = details or {}
        suffix = ""
        if request_id:
            suffix = " [request_id={0}; outcome={1}]".format(request_id, outcome)
        if outcome in ("unknown", "running") and request_id:
            suffix += " Poll get_command_status with this request_id; do not retry the edit."
        super().__init__(message + suffix)


class RemoteCommandError(CommandError):
    """A complete error response was received from Live."""


class OutcomePending(CommandError):
    """Live started a request whose final result is not yet available."""


@dataclass
class AbletonConnection:
    host: str
    port: int
    sock: socket.socket | None = None
    connect_timeout: float = 5.0
    response_timeout: float = 15.0
    command_timeout: float = 10.0
    max_frame_bytes: int = MAX_FRAME_BYTES
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _buffer: bytes = field(default=b"", init=False, repr=False)

    def connect(self) -> bool:
        """Connect with a bounded deadline, closing failed candidate sockets."""
        with self._lock:
            if self.sock is not None:
                return True
            candidate = None
            try:
                candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                candidate.settimeout(self.connect_timeout)
                candidate.connect((self.host, self.port))
                self.sock = candidate
                logger.info("Connected to Ableton at %s:%s", self.host, self.port)
                return True
            except OSError as exc:
                if candidate is not None:
                    candidate.close()
                logger.warning("Could not connect to Ableton: %s", exc)
                return False

    def disconnect(self):
        with self._lock:
            self._disconnect_locked()

    def _disconnect_locked(self):
        current, self.sock = self.sock, None
        self._buffer = b""
        if current is not None:
            try:
                current.close()
            except OSError:
                logger.debug("Socket close failed", exc_info=True)

    def receive_full_response(self, sock, buffer_size=8192, deadline=None):
        """Read one NDJSON frame, tolerating old unframed JSON replies.

        ``deadline`` spans the entire frame, including fragmented UTF-8. It
        is not renewed by each received fragment. Call under the exchange lock.
        """
        deadline = deadline if deadline is not None else time.monotonic() + self.response_timeout
        while True:
            self._buffer = self._buffer.lstrip(b"\r\n")
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                if newline > self.max_frame_bytes:
                    raise ValueError("Ableton response exceeds maximum frame size")
                frame, self._buffer = self._buffer[:newline], self._buffer[newline + 1:]
                json.loads(frame.decode("utf-8"))
                return frame
            if len(self._buffer) > self.max_frame_bytes:
                raise ValueError("Ableton response exceeds maximum frame size")
            # Legacy scripts reply with a single JSON object without newline.
            if self._buffer:
                try:
                    decoded = json.loads(self._buffer.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
                else:
                    if not isinstance(decoded, dict):
                        raise ValueError("Ableton response must be an object")
                    frame, self._buffer = self._buffer, b""
                    return frame
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Deadline exceeded while reading Ableton response")
            sock.settimeout(remaining)
            fragment = sock.recv(min(buffer_size, self.max_frame_bytes + 1))
            if not fragment:
                raise ConnectionError("Ableton disconnected before a complete response")
            self._buffer += fragment

    def send_command(self, command_type: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send once and receive only this request's response; never retry edits."""
        request_id = str(uuid.uuid4())
        envelope = {
            "id": request_id,
            "protocol_version": PROTOCOL_VERSION,
            "type": command_type,
            "params": params or {},
            "timeout_seconds": self.command_timeout,
        }
        wire = json.dumps(envelope, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(wire) - 1 > self.max_frame_bytes:
            raise CommandError("Command exceeds maximum frame size", request_id, "not_sent")
        if not self._lock.acquire(timeout=self.response_timeout):
            raise CommandError("Another command still owns the connection", request_id, "not_sent")
        sent = False
        try:
            if self.sock is None and not self.connect():
                raise CommandError("Could not connect to Ableton. Make sure the Remote Script is running.",
                                   request_id, "not_sent")
            deadline = time.monotonic() + self.response_timeout
            self.sock.settimeout(self.response_timeout)
            # A send failure may have delivered a prefix or the whole request.
            sent = True
            self.sock.sendall(wire)
            raw = self.receive_full_response(self.sock, deadline=deadline)
            reply = json.loads(raw.decode("utf-8"))
            if not isinstance(reply, dict):
                raise ValueError("Ableton response must be an object")
            if "id" in reply and reply["id"] != request_id:
                raise ValueError("Ableton response request ID does not match")
            result = reply.get("result", {})
            if not isinstance(result, dict):
                raise ValueError("Ableton response result must be an object")
            status = reply.get("status")
            if status == "pending":
                raise OutcomePending(result.get("message", "Command outcome is pending"),
                                     request_id, result.get("status", "running"), result)
            if status == "error":
                raise RemoteCommandError(reply.get("message", "Unknown error from Ableton"),
                                         request_id, result.get("status", "failed"), result)
            if status != "success":
                raise ValueError("Unrecognized Ableton response status: {0!r}".format(status))
            return result
        except (RemoteCommandError, OutcomePending):
            # Complete error frames leave the socket synchronized and usable.
            raise
        except CommandError:
            raise
        except Exception as exc:
            self._disconnect_locked()
            raise CommandError("Communication with Ableton failed: {0}".format(exc), request_id,
                               "unknown" if sent else "not_sent") from exc
        finally:
            self._lock.release()
