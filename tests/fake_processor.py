"""A stand-in Crestron processor for the tests: speaks CIP as the public notes describe it (crestron_cip docstring).
NOT qualified against a real processor - it checks the client is consistent with those notes, nothing more.
Records every frame the panel sends."""
import socket
import threading
import time


def frame(kind, payload):
    return bytes([kind]) + len(payload).to_bytes(2, "big") + payload


class FakeProcessor:
    def __init__(self, ipids=(0x03,), joins=None, need_login=None):
        self.ipids = set(ipids)
        self.need_login = need_login            # (user, password) or None
        self.joins = joins or []                # raw sub-packets sent after the update request
        self.received = []                      # (type, payload) from the panel
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.conns = []
        self.stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.stop:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.conns.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def push(self, payload_subs):
        """Send join sub-packets to every connected panel (feedback)."""
        for c in list(self.conns):
            try:
                c.sendall(frame(0x05, b"\x00\x00" + payload_subs))
            except OSError:
                pass

    def push_ext(self, sub):
        for c in list(self.conns):
            try:
                c.sendall(frame(0x12, b"\x00\x00" + sub))
            except OSError:
                pass

    def _serve(self, c):
        c.sendall(frame(0x0F, b"\x02"))
        buf = bytearray()
        try:
            while not self.stop:
                d = c.recv(4096)
                if not d:
                    return
                buf += d
                while len(buf) >= 3:
                    n = int.from_bytes(buf[1:3], "big")
                    if len(buf) < 3 + n:
                        break
                    k, p = buf[0], bytes(buf[3:3 + n])
                    del buf[:3 + n]
                    self.received.append((k, p))
                    if k == 0x01:
                        if p[5] in self.ipids:
                            c.sendall(frame(0x02, b"\x00\x00\x00\x1f"))
                        else:
                            c.sendall(frame(0x02, b"\xff\xff\x02"))
                    elif k == 0x0B:
                        ok = self.need_login and p[2:].split(b"\x00")[0] == f"{self.need_login[0]}:{self.need_login[1]}".encode()
                        c.sendall(frame(0x0C, b"\x00\x00" + (b"\x64" if ok else b"\x00")))
                    elif k == 0x05 and p == b"\x00\x00\x02\x03\x00":
                        for sub in self.joins:
                            c.sendall(frame(0x05, b"\x00\x00" + sub))
                        c.sendall(frame(0x05, b"\x00\x00\x02\x03\x1c"))
                    elif k == 0x0D:
                        c.sendall(frame(0x0E, b"\x00\x00"))
                    elif k == 0x03:
                        return
        except OSError:
            return
        finally:
            c.close()

    def sent_digitals(self):
        """[(so, join, value, press)] the panel sent."""
        out = []
        for k, p in self.received:
            if k != 0x05 or len(p) < 4:
                continue
            sub = p[2:]
            so = 0
            if sub[0] != len(sub) - 1:
                raise AssertionError(f"sub-packet length {sub[0]} for {len(sub) - 1} bytes: {p.hex()}")
            if sub[1] == 0x38:
                so, sub = sub[5], sub[6:]
            if sub[1] in (0x00, 0x27):
                j = (sub[2] | ((sub[3] & 0x7F) << 8)) + 1
                out.append((so, j, 0 if sub[3] & 0x80 else 1, sub[1] == 0x27))
        return out

    def close(self):
        self.stop = True
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass
        self.srv.close()


def wait(cond, t=5.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False
