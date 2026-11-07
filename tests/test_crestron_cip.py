"""Tests for crestron_cip: the frame formats, the decoder and a session against a stand-in processor.

These check the client against the public protocol notes it was written from (see the module docstring). They do not
prove it against a real processor.
"""
import socket
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import crestron_cip as cip  # noqa: E402
from fake_processor import FakeProcessor, frame, wait  # noqa: E402


class Frames(unittest.TestCase):
    def test_connect_and_update_request(self):
        self.assertEqual(cip.connect_packet(0x03).hex(), "01000b000000000003" "40fffff101")
        self.assertEqual(cip.update_request().hex(), "05000500000203" "00")
        self.assertEqual(cip.sync_done().hex(), "05000500000203" "1d")
        self.assertEqual(cip.heartbeat().hex(), "0d00020000")
        self.assertEqual(cip.heartbeat(True).hex(), "0e00020000")
        with self.assertRaises(ValueError):
            cip.connect_packet(0x02)

    def test_digital_press_release_and_level(self):
        self.assertEqual(cip.digital(1, True).hex(), "050006000003" "270000")
        self.assertEqual(cip.digital(1, False).hex(), "050006000003" "270080")
        self.assertEqual(cip.digital(300, True, press=False).hex(), "050006000003" "002b01")
        with self.assertRaises(ValueError):
            cip.digital(0, True)

    def test_analog(self):
        self.assertEqual(cip.analog(7, 1234).hex(), "050008000005" "14" "0006" "04d2")
        self.assertEqual(cip.analog(7, 70000).hex()[-4:], "ffff")              # clamped

    def test_smart_object_ids(self):
        self.assertEqual(cip.analog(7, 1, so=200).hex(), "05000e00000b38000000c8" "05140006" "0001")
        with self.assertRaises(ValueError):
            cip.analog(7, 1, so=256)                                           # beyond the 1-byte id field
        d = cip.Decoder()
        self.assertEqual(d.feed(cip.analog(7, 1234, so=200)), [("a", 200, 7, 1234)])
        self.assertEqual(d.feed(cip.digital(5, True, so=12, press=False)), [("d", 12, 5, 1)])
        self.assertEqual([d.feed(f) for f in cip.serial(3, "hi", so=50)], [[("s", 50, 3, "hi")]])

    def test_serial_chunks_and_unicode(self):
        long = "x" * (cip.SERIAL_CHUNK * 2 + 5)
        frames = cip.serial(9, long)
        self.assertEqual(len(frames), 3)
        d = cip.Decoder()
        got = [ev for f in frames for ev in d.feed(f)]
        self.assertEqual(got, [("s", 0, 9, long)])
        arabic = "مرحبا"
        self.assertEqual(cip.Decoder().feed(cip.serial(4, arabic)[0]), [("s", 0, 4, arabic)])   # UTF-16LE flag


class Decoding(unittest.TestCase):
    def test_split_frames_and_packed_joins(self):
        d = cip.Decoder()
        f = frame(0x05, b"\x00\x00" + bytes([5, 0x00, 0x00, 0x00, 0x01, 0x80]))  # two digitals in one sub-packet
        self.assertEqual(d.feed(f[:4]), [])                                    # a frame split across reads
        self.assertEqual(d.feed(f[4:]), [("d", 0, 1, 1), ("d", 0, 2, 0)])
        f = frame(0x05, b"\x00\x00" + bytes([9, 0x14, 0, 0, 0, 5, 0, 1, 0, 6]))  # two analogs
        self.assertEqual(d.feed(f), [("a", 0, 1, 5), ("a", 0, 2, 6)])

    def test_old_ascii_serials(self):
        body = b"#1,\r#1,aaa\r#2,b\r"
        f = frame(0x05, b"\x00\x00" + bytes([1 + len(body), 0x02]) + body)
        self.assertEqual(cip.Decoder().feed(f), [("s", 0, 1, ""), ("s", 0, 1, "aaa"), ("s", 0, 2, "b")])

    def test_encoding_option(self):
        self.assertEqual(cip.Decoder().serial_part(0, 3, 3, "caf\xe9".encode("latin-1")), ("s", 0, 3, "caf\xe9"))
        self.assertEqual(cip.Decoder("cp1256").serial_part(0, 3, 3, "م".encode("cp1256")), ("s", 0, 3, "م"))

    def test_control_frames(self):
        d = cip.Decoder()
        self.assertEqual(d.feed(frame(0x0F, b"\x02")), [("program", 2)])
        self.assertEqual(d.feed(frame(0x02, b"\xff\xff\x02")), [("refused", "not defined")])
        self.assertEqual(d.feed(frame(0x02, b"\x00\x00\x00\x1f")), [("registered",)])
        self.assertEqual(d.feed(frame(0x0D, b"\x00\x00")), [("heartbeat",)])
        self.assertEqual(d.feed(frame(0x05, b"\x00\x00\x02\x03\x1c")), [("synced",)])
        self.assertEqual(d.feed(frame(0x03, b"")), [("disconnect",)])


class SessionWithStandIn(unittest.TestCase):
    def setUp(self):
        self.p = FakeProcessor(ipids=(0x03,), joins=[bytes([3, 0x00, 0x09, 0x00]), bytes([5, 0x14, 0, 4, 0, 42])])

    def tearDown(self):
        self.p.close()

    def _session(self, **kw):
        s = cip.Session("127.0.0.1", 0x03, port=self.p.port, timeout=3, **kw)
        s.open()
        return s

    def _drain(self, s, t=2.0):
        out, end = [], time.time() + t
        while time.time() < end:
            try:
                out += s.events()
            except socket.timeout:
                pass
            if ("synced",) in out:
                break
        return out

    def test_registers_receives_joins_and_answers_sync(self):
        s = self._session()
        evs = self._drain(s)
        self.assertIn(("d", 0, 10, 1), evs)
        self.assertIn(("a", 0, 5, 42), evs)
        self.assertTrue(wait(lambda: (0x05, b"\x00\x00\x02\x03\x1d") in self.p.received))   # end of sync answered
        s.close()

    def test_press_is_held_until_released(self):
        s = self._session()
        s.set("d", 17, 1)
        s.tick()
        s.set("d", 17, 0)
        self.assertTrue(wait(lambda: self.p.sent_digitals()[-1:] == [(0, 17, 0, True)]))
        self.assertGreaterEqual([d for d in self.p.sent_digitals() if d == (0, 17, 1, True)].__len__(), 2)   # re-sent while held
        s.close()

    def test_undefined_ip_id_is_refused(self):
        s = cip.Session("127.0.0.1", 0x09, port=self.p.port, timeout=3)
        with self.assertRaisesRegex(cip.CipError, "not defined"):
            s.open()

    def test_login(self):
        self.p.need_login = ("user", "pw")
        s = self._session(user="user", password="pw")
        self.assertEqual(s.login_note, "")
        s.close()
        s = self._session(user="user", password="wrong")                     # refused: tried once more without
        self.assertIn("refused", s.login_note)
        s.close()


if __name__ == "__main__":
    unittest.main()
