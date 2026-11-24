"""Run with python3 -I. Imports sidecar helpers without starting a kernel."""

import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SIDECAR = Path(__file__).resolve().parents[3] / "optional" / "python-r1"


def load_source(name):
    spec = importlib.util.spec_from_file_location(name, SIDECAR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bridge = load_source("bridge")
collector = load_source("nano_output")


def framed(body):
    return struct.pack(">I", len(body)) + body


class Input(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class SidecarUnit(unittest.TestCase):
    def test_collector_charges_marker_and_preserves_utf8_order(self):
        limit = len(collector.MARKER) + 8
        capture = collector.OutputCapture(limit)
        capture.add("🙂á")
        capture.add("日" * limit)
        capture.add("🧪")
        text, truncated = capture.result()
        self.assertTrue(truncated)
        self.assertEqual(text, "🙂" + collector.MARKER.decode() + "🧪")
        self.assertEqual(len(text.encode()), limit)

    def test_collector_omits_marker_if_it_does_not_fit(self):
        capture = collector.OutputCapture(3)
        capture.add("éx")
        self.assertEqual(capture.result(), ("éx", False))
        capture.add("🙂")
        self.assertEqual(capture.result(), ("éx", True))
        capture = collector.OutputCapture(0)
        capture.add("é")
        self.assertEqual(capture.result(), ("", True))

    def test_read_rejects_an_oversized_header_before_body_read(self):
        self.assertEqual(bridge.MAX_FRAME, 32 * 1024 * 1024)
        source = Input(struct.pack(">I", bridge.MAX_FRAME + 1))
        with patch.object(sys, "stdin", SimpleNamespace(buffer=source)):
            with self.assertRaisesRegex(ValueError, "exceeds byte limit"):
                bridge.read_call()
        self.assertEqual(source.read_sizes, [4])

    def test_read_preserves_code_and_refuses_invalid_frame_values(self):
        call = {"call_id": "fixture", "code": "print('日本語')\n", "remaining_ms": 100, "limit": 8}
        body = json.dumps(call, ensure_ascii=False).encode()
        with patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(framed(body)))):
            self.assertEqual(bridge.read_call(), call)
            self.assertIsNone(bridge.read_call())
        for value in [b"\x00", framed(body)[:-1], framed(body + b"{}"), framed(b'{"code":"a","code":"b"}'),
                      framed(json.dumps({**call, "extra": 1}).encode()), framed(body.replace(b"fixture", b"\xff"))]:
            with self.subTest(value=value), patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(value))):
                with self.assertRaises(ValueError):
                    bridge.read_call()

    def test_write_checks_encoded_bytes_before_writing_a_header(self):
        body = '{"text":"🧪"}'.encode()
        output = io.BytesIO()
        with patch.object(sys, "stdout", SimpleNamespace(buffer=output)):
            with patch.object(bridge, "MAX_FRAME", len(body) - 1), self.assertRaisesRegex(ValueError, "exceeds frame limit"):
                bridge.frame({"text": "🧪"})
            self.assertEqual(output.getvalue(), b"")
            with patch.object(bridge, "MAX_FRAME", len(body)):
                bridge.frame({"text": "🧪"})
        self.assertEqual(output.getvalue(), framed(body))


if __name__ == "__main__":
    unittest.main()
