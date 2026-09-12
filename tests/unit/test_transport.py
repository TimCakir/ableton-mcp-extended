"""Socket-level regressions; no Live process or listening network port needed."""

import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from MCP_Server.transport import AbletonConnection, CommandError, OutcomePending, RemoteCommandError


def _pair(**options):
    client, peer = socket.socketpair()
    peer.settimeout(2)
    return AbletonConnection("unused", 0, sock=client, **options), peer


def _read_request(peer):
    raw = b""
    while not raw.endswith(b"\n"):
        raw += peer.recv(4096)
    return json.loads(raw)


def _reply(peer, request, result, **extra):
    payload = {"id": request["id"], "status": "success", "result": result, **extra}
    peer.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")


def test_fragmented_utf8_and_matching_id_are_reassembled():
    connection, peer = _pair()
    def respond():
        request = _read_request(peer)
        assert request["protocol_version"] == "2.1"
        assert request["timeout_seconds"] == 10
        payload = json.dumps({"id": request["id"], "status": "success", "result": {"name": "Bäsš"}},
                             ensure_ascii=False).encode("utf-8") + b"\n"
        for byte in payload:
            peer.sendall(bytes([byte]))
    with ThreadPoolExecutor() as pool:
        response = pool.submit(respond)
        assert connection.send_command("get_track_info") == {"name": "Bäsš"}
        response.result()
    connection.disconnect()
    peer.close()


def test_concurrent_callers_receive_only_their_own_values():
    connection, peer = _pair()
    seen = []
    def respond():
        for _ in range(8):
            request = _read_request(peer)
            seen.append(request["id"])
            time.sleep(0.01)
            _reply(peer, request, {"value": request["params"]["value"]})
    with ThreadPoolExecutor(max_workers=10) as pool:
        remote = pool.submit(respond)
        calls = [pool.submit(connection.send_command, "test", {"value": i}) for i in range(8)]
        assert [call.result()["value"] for call in calls] == list(range(8))
        remote.result()
    assert len(set(seen)) == 8
    connection.disconnect()
    peer.close()


def test_legacy_unframed_response_is_supported():
    connection, peer = _pair()
    def respond():
        _read_request(peer)
        peer.sendall(b'{"status":"success","result":{"legacy":true}}')
    with ThreadPoolExecutor() as pool:
        remote = pool.submit(respond)
        assert connection.send_command("get_build_info")["legacy"]
        remote.result()
    connection.disconnect()
    peer.close()


def test_mismatched_id_closes_connection_instead_of_misattributing_result():
    connection, peer = _pair()
    def respond():
        request = _read_request(peer)
        _reply(peer, request, {}, id="someone-elses-command")
    with ThreadPoolExecutor() as pool:
        remote = pool.submit(respond)
        with pytest.raises(CommandError, match="request ID does not match") as caught:
            connection.send_command("set_tempo", {"tempo": 99})
        remote.result()
    assert caught.value.request_id
    assert caught.value.outcome == "unknown"
    assert connection.sock is None
    peer.close()


def test_disconnect_reports_reconciliation_id_and_does_not_replay():
    connection, peer = _pair()
    requests = []
    def respond():
        requests.append(_read_request(peer))
        peer.close()
    with ThreadPoolExecutor() as pool:
        remote = pool.submit(respond)
        with pytest.raises(CommandError, match="get_command_status") as caught:
            connection.send_command("create_midi_track")
        remote.result()
    assert len(requests) == 1
    assert caught.value.request_id == requests[0]["id"]
    assert connection.sock is None


def test_total_response_deadline_is_not_extended_by_fragments():
    connection, peer = _pair(response_timeout=0.12)
    def respond():
        _read_request(peer)
        try:
            for _ in range(20):
                peer.sendall(b" ")
                time.sleep(0.025)
        except OSError:
            pass
    with ThreadPoolExecutor() as pool:
        remote = pool.submit(respond)
        started = time.monotonic()
        with pytest.raises(CommandError):
            connection.send_command("create_midi_track")
        elapsed = time.monotonic() - started
        remote.result()
    assert elapsed < 0.4
    assert connection.sock is None
    peer.close()


def test_oversized_response_closes_socket():
    connection, peer = _pair(max_frame_bytes=256)
    def respond():
        _read_request(peer)
        peer.sendall(b'{"result":"' + b'a' * 300)
    with ThreadPoolExecutor() as pool:
        remote = pool.submit(respond)
        with pytest.raises(CommandError, match="maximum frame size"):
            connection.send_command("test")
        remote.result()
    assert connection.sock is None
    peer.close()


def test_failed_connect_closes_candidate_socket(monkeypatch):
    candidate = Mock()
    candidate.connect.side_effect = OSError("unavailable")
    monkeypatch.setattr(socket, "socket", lambda *args: candidate)
    connection = AbletonConnection("unavailable", 9877)
    assert connection.connect() is False
    candidate.settimeout.assert_called_once_with(5.0)
    candidate.close.assert_called_once()
    assert connection.sock is None


def test_remote_error_and_pending_are_not_retried_and_keep_framing():
    connection, peer = _pair()
    requests = []
    def respond():
        for status, result in (("error", {}), ("pending", {"status": "running"}), ("success", {"ok": True})):
            request = _read_request(peer)
            requests.append(request)
            _reply(peer, request, result, status=status, message="rejected")
    with ThreadPoolExecutor() as pool:
        remote = pool.submit(respond)
        with pytest.raises(RemoteCommandError, match="rejected"):
            connection.send_command("create_midi_track")
        with pytest.raises(OutcomePending) as caught:
            connection.send_command("batch")
        assert caught.value.request_id == requests[1]["id"]
        assert connection.send_command("get_command_status")["ok"]
        remote.result()
    assert len(requests) == 3
    connection.disconnect()
    peer.close()


def test_busy_connection_times_out_before_sending():
    connection, peer = _pair(response_timeout=0.05)
    held = threading.Event()
    release = threading.Event()
    def holder():
        with connection._lock:
            held.set()
            release.wait(2)
    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(1)
    try:
        with pytest.raises(CommandError) as caught:
            connection.send_command("delete_track")
        assert caught.value.outcome == "not_sent"
    finally:
        release.set()
        thread.join()
        connection.disconnect()
        peer.close()
