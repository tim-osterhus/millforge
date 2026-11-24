"""Installed Linux PTY proof. Run with the absolute path of the installed millforge bin."""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ESCAPES = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")


class Terminal:
    def __init__(self, command, env, cwd):
        self.master, self.slave = pty.openpty()
        self.before = termios.tcgetattr(self.slave)
        self.resize(100, 32)
        code = "import os,sys,fcntl,termios;fcntl.ioctl(0,termios.TIOCSCTTY,0);os.execv(sys.argv[1],sys.argv[1:])"
        self.proc = subprocess.Popen(
            [sys.executable, "-c", code, *command], env=env, cwd=cwd,
            stdin=self.slave, stdout=self.slave, stderr=self.slave, start_new_session=True,
        )
        self.output = b""

    def resize(self, cols, rows):
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def text(self):
        return ESCAPES.sub(b"", self.output).decode(errors="replace")

    def send(self, value):
        os.write(self.master, value)

    def pump(self, seconds):
        if select.select([self.master], [], [], seconds)[0]:
            self.output += os.read(self.master, 65536)

    def expect(self, marker, start=0, seconds=8):
        end = time.monotonic() + seconds
        while marker not in self.text()[start:] and time.monotonic() < end:
            self.pump(0.1)
        assert marker in self.text()[start:], (marker, self.text()[-2500:])

    def submit(self, text, expected):
        start = len(self.text())
        self.send(text.encode() + b"\r")
        self.expect(expected, start=start)

    def exit(self, expected):
        assert self.proc.wait(timeout=8) == expected, self.text()[-2500:]
        while select.select([self.master], [], [], 0)[0]:
            self.pump(0)
        assert termios.tcgetattr(self.slave) == self.before, "terminal settings changed"
        return self.text()

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=8)
        os.close(self.slave)
        os.close(self.master)

    def host_pid(self):
        children = Path(f"/proc/{self.proc.pid}/task/{self.proc.pid}/children").read_text().split()
        assert len(children) == 1, children
        return int(children[0])


def eventually(check, description):
    end = time.monotonic() + 7
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.025)
    raise AssertionError(description)


def write_json(file, value):
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(value))


def run(cli, root):
    package = cli.resolve().parents[3]
    home, workspace, temp = [root / name for name in ("home", "work", "tmp")]
    for directory in (home, workspace, temp):
        directory.mkdir()
    canary = workspace / "ambient-loaded"
    ambient = f'import {{writeFileSync}} from "node:fs"; writeFileSync({json.dumps(str(canary))}, "loaded"); export default () => {{}};'
    for parent in (home / ".pi" / "agent", workspace / ".pi", workspace / ".pi-agent"):
        extension = parent / "extensions" / "ambient.ts"
        extension.parent.mkdir(parents=True)
        extension.write_text(ambient)
        package_root = parent / "ambient-package"
        write_json(parent / "settings.json", {"packages": [str(package_root)]})
        write_json(package_root / "package.json", {"name": "ambient-canary", "pi": {"extensions": ["./canary.ts"]}})
        (package_root / "canary.ts").write_text(ambient)
    write_json(home / ".config/millforge/auth/secret.json", {"sentinel": "finite oauth"})
    manifest = workspace / "python-r1-manifest.json"
    manifest.write_text("unavailable")
    (workspace / "input.txt").write_text("SCOPED_TOOL_RESULT\n")
    ambient_before = {file: file.read_bytes() for file in [*home.rglob("*"), *workspace.rglob("*")] if file.is_file()}

    env = {
        "HOME": str(home), "PATH": os.environ.get("PATH", os.defpath), "TERM": "xterm-256color", "LANG": "C.UTF-8",
        "TMPDIR": str(temp), "PI_CODING_AGENT_DIR": str(home / ".pi" / "agent"),
        "MILLFORGE_PYTHON_R1_MANIFEST": str(manifest),
        "LOCAL_TUI_CREDENTIAL": "fixture-selected-env", "OPENAI_API_KEY": "unselected-fixture-key",
    }

    def run_pty(args):
        return Terminal([str(cli), *args], env, workspace)

    def validate(profile):
        result = subprocess.run([cli, "profile", "validate", profile], env=env, cwd=workspace, capture_output=True, timeout=5)
        assert result.returncode == 0 and json.loads(result.stdout)["valid"] is True, result.stderr.decode()

    def one(args, result, message):
        terminal = run_pty(args)
        try:
            output = terminal.exit(result)
            assert message in output, output
            return output
        finally:
            terminal.close()

    # Preserve the previous bare redirected failure and enforce explicit redirected refusal.
    for args, message in [([], "expected profile, inspect or run command"), (["tui"], "requires terminal input and output")]:
        result = subprocess.run([cli, *args], env=env, cwd=workspace, capture_output=True, timeout=5)
        assert result.returncode == 2 and message.encode() in result.stderr and not result.stdout
    guidance = one([], 2, "millforge profile select")
    select_line = next(line for line in guidance.splitlines() if line.strip().startswith("millforge profile select "))
    assert shlex.split(select_line) == ["millforge", "profile", "select", str(package / "profiles/agent")], guidance
    one(["tui", "--profile", str(package / "profiles/generate")], 2, "Use millforge run")
    one(["tui", "--profile", str(Path(__file__).parent / "codemode")], 2, "Use millforge run")

    # The peer is the only selected endpoint. It observes editor input, credentials and tool continuation.
    observed, cancelled, errors = [], threading.Event(), []
    writer = workspace / "writer.py"
    heartbeat, writer_pid = workspace / "heartbeat", workspace / "writer-pid"
    writer.write_text("import os,time,pathlib,sys\n"
                      "assert not any(k in os.environ for k in ('LOCAL_TUI_CREDENTIAL','OPENAI_API_KEY','MILLFORGE_PYTHON_R1_MANIFEST'))\n"
                      "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
                      "while True:\n with open(sys.argv[2],'a') as f: f.write('x')\n time.sleep(.05)\n")
    writer_command = "exec " + shlex.join([sys.executable, str(writer), str(writer_pid), str(heartbeat)])

    class Peer(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                index = len(observed)
                observed.append((body, self.headers.get("Authorization")))
                assert self.path == "/v1/chat/completions"
                assert body["model"] == "operator-model" and body["stream"] is True
                assert body["max_tokens"] == 4096 and body["temperature"] == 0
                assert self.headers.get("x-api-key") is None
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()

                def frame(delta, reason=None):
                    value = {"id": "test", "object": "chat.completion.chunk", "created": 1, "model": "operator-model",
                             "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
                    self.wfile.write(b"data: " + json.dumps(value).encode() + b"\n\n")
                    self.wfile.flush()

                if index in (1, 5):
                    tool = ("read", {"path": "input.txt"}) if index == 1 else ("bash", {"command": writer_command})
                    frame({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                           "function": {"name": tool[0], "arguments": json.dumps(tool[1])}}]})
                    frame({}, "tool_calls")
                elif index == 3:
                    frame({"role": "assistant", "content": "CANCELLATION_PARTIAL"})
                    try:
                        for _ in range(700):
                            time.sleep(.01)
                            frame({"content": "."})
                    except OSError:
                        cancelled.set()
                        return
                    raise AssertionError("stream stayed connected after Escape")
                else:
                    responses = {0: ("STREAM_HEAD", " STREAM_TAIL"), 2: ("READ_COMPLETE", ""), 4: ("ENV_COMPLETE", "")}
                    head, tail = responses[index]
                    frame({"role": "assistant", "content": head})
                    time.sleep(.55)
                    frame({"content": tail})
                    frame({}, "stop")
                self.wfile.write(b"data: [DONE]\n\n")
            except Exception as error:
                errors.append(error)
                raise

    peer = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
    server = threading.Thread(target=peer.serve_forever, daemon=True)
    server.start()
    try:
        python_example = Path(__file__).resolve().parents[2] / "optional/python-r1/profile.example.json"
        if python_example.is_file():
            python_profile = json.loads(python_example.read_text())
            python_profile["model"]["base_url"] = f"http://127.0.0.1:{peer.server_port}/v1"
            python_dir = root / "v4"
            write_json(python_dir / "profile.json", python_profile)
            validate(python_dir)
            refusal = one(["tui", "--profile", str(python_dir)], 2, "Use millforge run")
            assert '"code":"UNSUPPORTED_CAPABILITY"' in refusal and "in-memory session" not in refusal
            assert not observed and not list(temp.iterdir()), "unsupported Python profile started interactive work"
            print("validated Python R1 v4 profile refused by installed TUI before launch")

        base = json.loads((package / "profiles/agent/profile.json").read_text())
        base["model"]["base_url"] = f"http://127.0.0.1:{peer.server_port}/v1"
        base["tools"] = ["read", "bash"]
        base["instructions"] = "Selected fixture instructions."
        context = "Profile-supplied context.\n"
        base["assets"] = [{"kind": "context", "path": "context.md", "sha256": hashlib.sha256(context.encode()).hexdigest()}]
        profile = root / "v1"
        write_json(profile / "profile.json", base)
        (profile / "context.md").write_text(context)
        subprocess.run([cli, "profile", "select", profile], env=env, cwd=workspace, check=True, capture_output=True, timeout=5)
        terminal = run_pty([])
        try:
            terminal.expect("in-memory session")
            child = terminal.host_pid()
            _, _, group, _, tty, foreground, *_ = Path(f"/proc/{child}/stat").read_text().split(") ", 1)[1].split()
            assert int(tty) != 0 and int(group) == int(foreground) == terminal.proc.pid
            terminal.send(b"/mill")
            terminal.expect("Show Millforge session")
            terminal.send(b"\t")
            terminal.send(b"\r")
            terminal.expect("millforge optimize inspect --campaign DIR")
            terminal.expect("Tools read, bash")
            assert not observed
            for command in ["/login openai", "/logout", "/trust", "/model", "/scoped-models", "/settings", "/resume",
                            "/import unowned.jsonl", "/fork", "/clone", "/reload", "/share", "/bug"]:
                terminal.submit(command, "unavailable in a profile-scoped")
            terminal.send(b"\x1a")  # Ctrl-Z has no keybinding for the inherited process group.
            terminal.resize(115, 36)
            terminal.send(b"hello!\x7f\r")
            terminal.expect("STREAM_HEAD")
            assert "STREAM_TAIL" not in terminal.text(), "render did not stream before finish"
            terminal.expect("STREAM_TAIL")
            terminal.submit("tool fixture", "READ_COMPLETE")
            terminal.submit("abort fixture", "CANCELLATION_PARTIAL")
            terminal.send(b"\x1b")
            assert cancelled.wait(4), "provider stream did not close"
            terminal.send(b"/quit\r")
            terminal.exit(0)
            assert not Path(f"/proc/{child}").exists()
        finally:
            terminal.close()
        assert len(observed) == 4 and all(auth is None for _, auth in observed)
        user_texts = [message["content"] for message in observed[0][0]["messages"] if message["role"] == "user"]
        assert user_texts == ["hello"] or user_texts == [[{"type": "text", "text": "hello"}]], user_texts
        assert "Selected fixture instructions." in json.dumps(observed[0][0])
        assert "Profile-supplied context." in json.dumps(observed[0][0])
        assert {tool["function"]["name"] for tool in observed[1][0]["tools"]} == {"read", "bash"}
        assert "SCOPED_TOOL_RESULT" in json.dumps(observed[2][0])

        base["schema"] = "millforge-profile/v2"
        base["runtime"]["binding_version"] = 2
        base["model"].pop("credential_env")
        base["model"].update(provider="fixture-provider", auth={"kind": "env", "name": "LOCAL_TUI_CREDENTIAL"},
                             metadata={"source": "operator", "overrides": {"name": "operator-model", "input": ["text"],
                                 "cost": None, "reasoning": False, "thinkingLevelMap": {},
                                 "contextWindow": 32768, "maxTokens": 4096}})
        oauth_profile = json.loads(json.dumps(base))
        oauth_profile["model"].update(provider="openai", api="openai-responses", id="gpt-6-luna",
                                      base_url="https://api.openai.com/v1", auth={"kind": "oauth", "provider": "openai"},
                                      max_output_tokens=None, compat={},
                                      metadata={"source": "pinned-catalog", "overrides": {}})
        oauth_profile["sampling"] = {"temperature": None, "top_p": None}
        oauth_profile["assets"] = []
        write_json(root / "oauth/profile.json", oauth_profile)
        validate(root / "oauth")
        one(["tui", "--profile", str(root / "oauth")], 2, "Use millforge run")
        profile = root / "v2"
        write_json(profile / "profile.json", base)
        (profile / "context.md").write_text(context)
        validate(profile)
        terminal = run_pty(["tui", "--profile", str(profile)])
        try:
            terminal.expect("in-memory session")
            child = terminal.host_pid()
            terminal.submit("selected env", "ENV_COMPLETE")
            terminal.send(b"forced exit tool fixture\r")
            eventually(lambda: writer_pid.is_file() and heartbeat.is_file(), "fixture writer did not start")
            pid = int(writer_pid.read_text())
            terminal.proc.terminate()
            terminal.exit(143)
            assert not Path(f"/proc/{child}").exists()
            eventually(lambda: not Path(f"/proc/{pid}").exists(), "fixture writer still present")
            size = heartbeat.stat().st_size
            time.sleep(.15)
            assert heartbeat.stat().st_size == size, "fixture writer still writing"
        finally:
            terminal.close()
        assert len(observed) == 6 and all(auth == "Bearer fixture-selected-env" for _, auth in observed[4:])
        assert not errors, errors
        assert not canary.exists() and not list(temp.iterdir())
        assert all(file.read_bytes() == content for file, content in ambient_before.items() if file.name != "selection.json")
        assert not (workspace / ".pi/agent").exists(), "legacy workspace migrated"
    finally:
        if writer_pid.is_file():
            cmdline = Path(f"/proc/{int(writer_pid.read_text())}/cmdline")
            try:
                if os.fsencode(writer) in cmdline.read_bytes().split(b"\0"):
                    os.kill(int(writer_pid.read_text()), signal.SIGKILL)
            except (FileNotFoundError, ProcessLookupError):
                pass
        peer.shutdown()
        peer.server_close()
        server.join(timeout=5)


if __name__ == "__main__":
    if not sys.platform.startswith("linux"):
        raise SystemExit("Run the installed acceptance fixture on Linux")
    with tempfile.TemporaryDirectory(prefix="millforge-pty-fixture-") as directory:
        run(Path(sys.argv[1]).absolute(), Path(directory))
    print("installed PTY proof passed")
