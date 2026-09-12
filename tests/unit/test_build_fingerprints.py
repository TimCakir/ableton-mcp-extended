"""Build diagnostics must detect helper changes even when entrypoints match."""

import ast
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(params=["MCP_Server/server.py", "AbletonMCP_Remote_Script/__init__.py"])
def fingerprint(request):
    # Load only the pure helper, avoiding SDK and Live dependencies in these cases.
    tree = ast.parse((ROOT / request.param).read_text())
    helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_package_sha256")
    namespace = {"hashlib": hashlib, "os": os}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), request.param, "exec"), namespace)
    return namespace["_package_sha256"]


def package(directory):
    directory.mkdir()
    (directory / "__init__.py").write_text("# entrypoint\n")
    (directory / "helper.py").write_text("VALUE = 1\n")
    return directory


def test_package_hash_is_location_independent_and_ignores_bytecode(fingerprint, tmp_path):
    first, second = package(tmp_path / "first"), package(tmp_path / "second")
    expected = fingerprint(first)
    assert expected and expected == fingerprint(second)
    (second / "__pycache__").mkdir()
    (second / "__pycache__" / "helper.pyc").write_bytes(b"compiled data")
    assert fingerprint(second) == expected
    (second / "nested").mkdir()
    (second / "nested" / "extra.py").write_text("# nested source\n")
    assert fingerprint(second) != expected


def test_helper_edit_rename_and_removal_change_hash(fingerprint, tmp_path):
    directory = package(tmp_path / "package")
    initial = fingerprint(directory)
    helper = directory / "helper.py"
    helper.write_text("VALUE = 2\n")
    edited = fingerprint(directory)
    helper.rename(directory / "renamed.py")
    renamed = fingerprint(directory)
    (directory / "renamed.py").unlink()
    removed = fingerprint(directory)
    assert len({initial, edited, renamed, removed}) == 4
    (directory / "__init__.py").unlink()
    assert fingerprint(directory) is None
    assert fingerprint(tmp_path / "missing") is None


def test_read_failure_is_unknown_instead_of_partial_fingerprint(fingerprint, tmp_path, monkeypatch):
    directory = package(tmp_path / "package")
    def fail(*args, **kwargs):
        raise PermissionError("source unavailable")
    monkeypatch.setitem(fingerprint.__globals__, "open", fail)
    assert fingerprint(directory) is None


def test_diagnostics_detect_helpers_in_loaded_remote_disk_repo_and_server():
    source = '''
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from MCP_Server import server

        with TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "AbletonMCP_Remote_Script"
            local = root / "MCP_Server"
            remote.mkdir()
            local.mkdir()
            (remote / "__init__.py").write_text('BUILD_ID = "' + server.SERVER_BUILD_ID + '"\\n')
            (remote / "arrangement_builder.py").write_text("VALUE = 1\\n")
            (local / "__init__.py").write_text("")
            (local / "server.py").write_text("# server fixture\\n")
            (local / "workflow_tools.py").write_text("VALUE = 1\\n")
            server.__file__ = str(local / "server.py")
            server.SERVER_SOURCE_SHA256 = server._source_sha256(server.__file__)
            server.SERVER_PACKAGE_SHA256 = server._package_sha256(local)
            reply = {
                "remote_script_build": server.SERVER_BUILD_ID,
                "protocol_version": server.PROTOCOL_VERSION,
                "source_sha256": server._source_sha256(remote / "__init__.py"),
                "package_sha256": server._package_sha256(remote),
                "disk_package_sha256": server._package_sha256(remote),
            }
            class Connection:
                def send_command(self, *args):
                    return dict(reply)
            server.get_ableton_connection = lambda: Connection()
            assert server.get_build_info(None)["status"] == "match"

            # Updating/removing only a repo helper must invalidate the loaded remote.
            helper = remote / "arrangement_builder.py"
            helper.write_text("VALUE = 2\\n")
            result = server.get_build_info(None)
            assert result["status"] == "mismatch", result
            assert result["remote_source_sha256"] == result["repo_source_sha256"]
            assert result["remote_package_sha256"] != result["repo_package_sha256"]
            assert any("Ableton Live" in line for line in result["recovery"])
            helper.unlink()
            assert server.get_build_info(None)["status"] == "mismatch"
            helper.write_text("VALUE = 1\\n")
            assert server.get_build_info(None)["status"] == "match"

            # The deployed package may change before Live reloads it.
            reply["disk_package_sha256"] = "new-deployed-package"
            assert server.get_build_info(None)["status"] == "mismatch"
            reply["disk_package_sha256"] = reply["package_sha256"]

            # Entry-point equality is insufficient when an imported server helper changed.
            (local / "workflow_tools.py").write_text("VALUE = 2\\n")
            result = server.get_build_info(None)
            assert result["status"] == "mismatch", result
            assert result["server_loaded_source_sha256"] == result["server_disk_source_sha256"]
            assert any("MCP host" in line for line in result["recovery"])
            (local / "workflow_tools.py").write_text("VALUE = 1\\n")

            # Old remotes can report their entrypoint hash but cannot prove package agreement.
            del reply["package_sha256"]
            del reply["disk_package_sha256"]
            result = server.get_build_info(None)
            assert result["status"] == "unknown", result
            assert result["remote_source_sha256"] == result["repo_source_sha256"]
    '''
    result = subprocess.run([sys.executable, "-c", textwrap.dedent(source)], cwd=ROOT,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
