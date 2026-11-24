"""Drive one owned local Jupyter kernel with a serial, bounded private protocol."""

import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import struct
import subprocess
import sys
import sysconfig
import time
import uuid


MAX_FRAME = 32 * 1024 * 1024
MAX_PARTS = 64
MAX_ARGS = 16_384
MAX_TEXT = 51_200
stopping = False


def stop(_signal, _frame):
    global stopping
    stopping = True


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate bridge frame field")
        result[key] = value
    return result


def read_call():
    prefix = sys.stdin.buffer.read(4)
    if not prefix:
        return None
    if len(prefix) != 4:
        raise ValueError("partial bridge frame header")
    length = struct.unpack(">I", prefix)[0]
    if not 0 < length <= MAX_FRAME:
        raise ValueError("bridge frame exceeds byte limit")
    data = sys.stdin.buffer.read(length)
    if len(data) != length:
        raise ValueError("partial bridge frame body")
    call = json.loads(data.decode("utf-8"), object_pairs_hook=unique_pairs)
    if not isinstance(call, dict) or set(call) != {"call_id", "code", "remaining_ms", "limit"}:
        raise ValueError("invalid bridge call fields")
    code = call["code"]
    if (not isinstance(call["call_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", call["call_id"])
            or not isinstance(code, str) or not code.strip() or "\0" in code
            or len(json.dumps({"code": code}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_ARGS
            or type(call["remaining_ms"]) is not int or not 1 <= call["remaining_ms"] <= 2_147_483_000
            or type(call["limit"]) is not int or not 0 <= call["limit"] <= MAX_TEXT):
        raise ValueError("invalid bridge call values")
    return call


def frame(value):
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not 0 < len(data) <= MAX_FRAME:
        raise ValueError("bridge response exceeds frame limit")
    sys.stdout.buffer.write(struct.pack(">I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


class Kernel:
    """One socket reader and one cell at a time. The kernel stays in the worker group."""

    def __init__(self, directory, zmq, session, connection):
        self.zmq = zmq
        self.session = session
        self.context = zmq.Context()
        self.poller = zmq.Poller()
        self.sockets = []
        self.child = None
        self.remaining = MAX_FRAME
        self.in_flight = False
        prefix = str(directory / "kernel")
        filename = str(directory / "connection.json")
        try:
            _, addresses = connection(fname=filename, ip=prefix, transport="ipc", key=session.key)
            for name in ("shell", "iopub", "stdin", "hb", "control"):
                if len(os.fsencode(f"{prefix}-{addresses[f'{name}_port']}")) >= 108:
                    raise ValueError("kernel socket path is too long")
            self.shell = self._connect(zmq.DEALER, prefix, addresses["shell_port"])
            self.iopub = self._connect(zmq.SUB, prefix, addresses["iopub_port"])
            self.child = subprocess.Popen(
                [sys.executable, "-I", "-B", "-m", "ipykernel_launcher", "-f", filename,
                 "--HistoryManager.enabled=False", "--IPKernelApp.no_stdout=False", "--IPKernelApp.no_stderr=False"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except BaseException:
            self.close()
            raise

    def _connect(self, kind, prefix, port):
        socket = self.context.socket(kind)
        self.sockets.append(socket)
        socket.setsockopt(self.zmq.LINGER, 0)
        socket.setsockopt(self.zmq.RCVHWM, MAX_PARTS)
        socket.setsockopt(self.zmq.MAXMSGSIZE, MAX_FRAME)
        socket.setsockopt(self.zmq.SNDTIMEO, 100)
        if kind == self.zmq.SUB:
            socket.setsockopt(self.zmq.SUBSCRIBE, b"")
        socket.connect(f"ipc://{prefix}-{port}")
        self.poller.register(socket, self.zmq.POLLIN)
        return socket

    def _receive(self, socket):
        parts = []
        while True:
            if len(parts) >= MAX_PARTS:
                raise OverflowError("kernel multipart part count exceeds limit")
            part = socket.recv(copy=False)
            size = len(part)
            if size > self.remaining:
                raise OverflowError("kernel traffic exceeds invocation byte limit")
            self.remaining -= size
            parts.append(bytes(part))
            if not socket.getsockopt(self.zmq.RCVMORE):
                break
        _, signed = self.session.feed_identities(parts)
        return self.session.deserialize(signed)

    def _messages(self, deadline):
        if stopping:
            raise InterruptedError("kernel stop requested")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("kernel deadline reached")
        if self.child.poll() is not None:
            raise RuntimeError("kernel exited before settlement")
        ready = dict(self.poller.poll(max(1, min(100, int(remaining * 1000)))))
        for socket in self.sockets:
            if ready.get(socket, 0) & self.zmq.POLLIN:
                yield socket, self._receive(socket)

    def ready(self, deadline):
        # Retry only the readiness message. No submitted cell is ever retried.
        while True:
            attempt = min(deadline, time.monotonic() + 0.3)
            request = self.session.send(self.shell, "kernel_info_request")
            try:
                shell_ready = iopub_ready = False
                while not (shell_ready and iopub_ready):
                    for socket, message in self._messages(attempt):
                        if message["parent_header"].get("msg_id") != request["msg_id"]:
                            continue
                        if socket is self.shell and message["msg_type"] == "kernel_info_reply":
                            shell_ready = True
                        if socket is self.iopub and message["msg_type"] == "status":
                            iopub_ready = True
                return
            except TimeoutError:
                if time.monotonic() >= deadline:
                    raise

    def execute(self, cell, deadline, output, report_dispatch):
        if stopping:
            raise InterruptedError("kernel stop requested")
        if time.monotonic() >= deadline:
            raise TimeoutError("kernel deadline reached")
        request = self.session.msg("execute_request", content={"code": cell, "silent": False,
            "store_history": False, "user_expressions": {}, "allow_stdin": False, "stop_on_error": True})
        self.in_flight = True
        report_dispatch("unknown")
        self.session.send(self.shell, request)
        report_dispatch("sent")
        reply = None
        idle = False
        while reply is None or not idle:
            for socket, message in self._messages(deadline):
                if message["parent_header"].get("msg_id") != request["msg_id"]:
                    continue
                kind, content = message["msg_type"], message["content"]
                if socket is self.shell and kind == "execute_reply":
                    if reply is not None or content.get("status") not in ("ok", "error"):
                        raise ValueError("invalid shell reply")
                    reply = content
                if socket is self.iopub and not idle:
                    if kind == "status" and content.get("execution_state") == "idle":
                        idle = True
                    if kind == "stream" and content.get("name") in ("stdout", "stderr"):
                        output.add(content["text"])
        self.in_flight = False
        if reply["status"] == "error":
            name = str(reply.get("ename", "Error"))[:128]
            detail = str(reply.get("evalue", ""))[:512]
            line = " ".join(f"{name}: {detail}".split()).replace("\0", "")
            output.add(f"\n{line}\n")
        return "completed" if reply["status"] == "ok" else "failed"

    def close(self):
        try:
            if self.child is not None and self.child.poll() is None:
                if self.in_flight:
                    self.child.send_signal(signal.SIGINT)
                    try:
                        self.child.wait(timeout=0.25)
                    except subprocess.TimeoutExpired:
                        pass
                if self.child.poll() is None:
                    self.child.terminate()
                    try:
                        self.child.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        self.child.kill()
                        self.child.wait(timeout=0.5)
        finally:
            for socket in self.sockets:
                socket.close(linger=0)
            self.context.term()


def main():
    collector_path, socket_dir = (Path(value) for value in sys.argv[1:])
    expected_env = Path(sys.executable).parent.parent.resolve()
    if (sys.implementation.name != "cpython" or sys.version_info[:3] != (3, 12, 14)
            or sysconfig.get_config_var("SOABI") != "cpython-312-x86_64-linux-gnu"
            or Path(sys.prefix) != expected_env or sys.prefix == sys.base_prefix):
        raise RuntimeError("interpreter environment differs from admission")

    import ipykernel
    import jupyter_client
    import zmq
    from jupyter_client.connect import write_connection_file
    from jupyter_client.session import Session

    if any(not Path(module.__file__).resolve().is_relative_to(expected_env)
           for module in (ipykernel, jupyter_client, zmq)):
        raise RuntimeError("Python import roots differ from admission")
    if (jupyter_client.__version__, ipykernel.__version__) != ("8.8.0", "7.2.0"):
        raise RuntimeError("Python kernel dependency versions differ from admission")

    spec = importlib.util.spec_from_file_location("millforge_r1_output", collector_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("no loader for admitted output collector")
    collector = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = collector
    spec.loader.exec_module(collector)

    signal.signal(signal.SIGUSR1, stop)
    signal.signal(signal.SIGTERM, stop)
    os.umask(0o077)
    kernel = None
    seen = set()
    try:
        while not stopping:
            call = read_call()
            if call is None:
                break
            call_id = call["call_id"]
            if call_id in seen:
                raise ValueError("bridge repeated call ID")
            seen.add(call_id)
            deadline = time.monotonic() + call["remaining_ms"] / 1000
            dispatch = "not_sent"

            def report_dispatch(value):
                nonlocal dispatch
                dispatch = value
                if value == "sent":
                    frame({"type": "dispatch", "call_id": call_id, "dispatch": value})

            output = collector.OutputCapture(call["limit"])
            problem = None
            settled = False
            try:
                if kernel is None:
                    session = Session(key=uuid.uuid4().hex.encode("ascii"))
                    kernel = Kernel(socket_dir, zmq, session, write_connection_file)
                    kernel.ready(deadline)
                status = kernel.execute(call["code"], deadline, output, report_dispatch)
                settled = True
            except InterruptedError:
                status, problem = "cancelled", "CANCELLED"
            except TimeoutError:
                status, problem = "timeout", "TIMEOUT"
            except OverflowError:
                status, problem = "failed", "OUTPUT_LIMIT"
            except Exception:
                status, problem = "failed", "TOOL_ERROR"
            text, truncated = output.result() if settled else ("", False)
            frame({"type": "end", "call_id": call_id, "status": status, "text": text, "truncated": truncated,
                   "dispatch": dispatch, "cell_settled": settled, "problem": problem})
            if problem is not None:
                break
    finally:
        if kernel is not None:
            kernel.close()


if __name__ == "__main__":
    main()
