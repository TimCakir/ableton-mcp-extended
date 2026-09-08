"""Register synchronous domain functions without blocking MCP's event loop."""

import asyncio
import contextvars
import functools
import inspect
import json
import re
import threading
import time

# Explicitly audited observational tools. Mixed get/set tools and arbitrary LOM
# invocation are deliberately omitted. Unknown tools get conservative hints.
READ_ONLY_TOOLS = frozenset({
    "get_session_info", "get_track_info", "get_track_volume", "get_session_overview",
    "get_clip_notes", "get_drift_modulation", "get_device_modes", "get_plugin_info",
    "get_device_io", "get_rack_map", "get_simpler_info", "get_application_info",
    "get_meters", "get_modulation_targets", "inspect_lom", "get_clip_automation",
    "get_performance_report", "get_automation_record_status", "get_routing_options",
    "get_grooves", "get_browser_tree", "get_browser_items_at_path", "list_external_plugins",
    "get_arrangement_info", "get_cue_points", "get_build_info", "get_device_parameters",
    "get_chain_info", "get_drum_pad_info", "get_track_deletion_status", "get_edit_targets",
    "get_session_snapshot", "get_command_status", "analyze_audio_file",
    "compare_session_snapshots", "verify_export_outputs",
    "get_latency_report", "analyze_recording_timing",
})


def _annotations(name):
    read_only = name in READ_ONLY_TOOLS
    return {
        "readOnlyHint": read_only,
        "destructiveHint": not read_only,
        "idempotentHint": read_only,
        # Domain calls may load external devices/assets and invoke arbitrary LOM.
        "openWorldHint": True,
    }


_tool_cancellation = contextvars.ContextVar("ableton_tool_cancellation", default=None)


class ToolCancelled(RuntimeError):
    """Cooperative stop for local work; already-issued remote calls may finish."""


def raise_if_tool_cancelled():
    event = _tool_cancellation.get()
    if event is not None and event.is_set():
        raise ToolCancelled("Tool call cancelled; no further local work will be started")


def wait_for_tool_cancellation(seconds):
    """Sleep between samples, waking immediately when the MCP call is cancelled."""
    event = _tool_cancellation.get()
    if event is None:
        time.sleep(seconds)
        return False
    return event.wait(seconds)


class ResponsiveMCP:
    """Small registration adapter; direct Python callers keep their old API.

    Cancellation signals cooperative workers such as measure_section. It
    does not kill threads, withdraw already-issued remote calls or roll back
    Live edits. Recording jobs have separate domain cancellation tools.
    Socket exchange ownership remains serialized by AbletonConnection.
    """

    def __init__(self, server):
        self._server = server

    def __getattr__(self, name):
        return getattr(self._server, name)

    def tool(self, *args, **kwargs):
        def register(fn):
            @functools.wraps(fn)
            async def responsive(*call_args, **call_kwargs):
                cancelled = threading.Event()
                token = _tool_cancellation.set(cancelled)
                try:
                    if inspect.iscoroutinefunction(fn):
                        result = await fn(*call_args, **call_kwargs)
                    else:
                        def invoke():
                            # A cancelled tool may still be queued in the pool.
                            raise_if_tool_cancelled()
                            return fn(*call_args, **call_kwargs)
                        result = await asyncio.to_thread(invoke)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                finally:
                    _tool_cancellation.reset(token)
                # Existing wrappers expose human-friendly error strings. At the
                # MCP boundary these must be execution errors, never successes.
                if isinstance(result, str) and re.match(r"^\s*error\b", result, re.IGNORECASE):
                    raise ValueError(result)
                if isinstance(result, dict) and result.get("status") in {"error", "partial", "unreachable"}:
                    from mcp.types import CallToolResult, TextContent
                    return CallToolResult(
                        content=[TextContent(type="text", text=json.dumps(result, indent=2))],
                        structuredContent=result,
                        isError=True,
                    )
                return result

            options = dict(kwargs)
            options.setdefault("annotations", _annotations(options.get("name") or fn.__name__))
            self._server.tool(*args, **options)(responsive)
            return fn
        return register
