"""crestron_cip - be a touch panel (XPanel) on a Crestron control processor's IP-ID and exchange joins over CIP.

Written from the public protocol notes of open-source CIP clients - klenae/python-cipclient, Antonio112009/pycrestron-cip
and rdriscoll/Crestron-CIP (MIT), CommandFusion/CIP and akellai/scip2cip (no licence; read, nothing copied) - no
Crestron code and no code copied from any of them.
Used live against a real control processor: registration, digital / analog / serial joins and smart-object joins.
Smart-object ids are 4 bytes (ids above 255 worked live). Since that test: text without the Unicode flag is decoded
with the encoding= option (checked when the Session is made), and the end-of-sync answer and multi-line old serials
(\r, \r\n or \n between values) are covered by tests only. Session.raw_log keeps the first frames received (hex).

Frame: [type 1][length 2, big-endian][payload]. Data frames (0x05) hold sub-packets [len 1][type 1][body] after a
2-byte handle; extended frames (0x12) hold [len 2][type 1][body]. Joins are 1-based here, 0-based on the wire.
    processor -> panel   0F 00 01 02                      'register now'
    panel -> processor   01 00 0B 00 00 00 00 00 IPID 40 FF FF F1 01
    processor -> panel   02 .. (FF FF 02 = IP-ID not defined in the program; anything else = accepted)
    panel -> processor   05 00 05 00 00 02 03 00          send me every join (update request)
    both                 0D 00 02 00 00 / 0E 00 02 00 00  heartbeat / answer (every 15 s)
    digital              sub 00 (processor) / 27 (panel press, auto-released unless re-sent within 0.5 s):
                         [join-1 low][join-1 high 7 bits | 0x80 when low]
    analog               sub 14 [join-1 2][value 2] (old processors: sub 01)
    serial               ext sub 34 / sub 15 [join-1 2][flags: 1 start, 2 end, 4 UTF-16LE][text]
    smart object         sub 38 / ext sub 39 [object id, 4 bytes big-endian][the sub-packets above]
    login (4-Series)     0B 00 LL 00 00 'user:password' 00 00 00 -> 0C ..level (0 = refused)
"""
import codecs
import re
import socket
import ssl
import time

PORT, TLS_PORT = 41794, 41796
HEARTBEAT = 15.0
DEAD_AFTER = 40.0                 # no frame at all for this long: the link is gone
REPEAT = 0.4                      # a held press is sent again this often (the processor releases it after 0.5 s)
MAX_HOLD = 30.0                   # a press held longer than this is released by us (a lost release)
SERIAL_CHUNK = 240


class CipError(Exception):
    pass


class LoginRefused(CipError):
    pass


def frame(kind, payload):
    return bytes([kind]) + len(payload).to_bytes(2, "big") + payload


def _join0(n):
    n = int(n)
    if not 1 <= n <= 0x8000:
        raise ValueError(f"join out of range: {n}")
    return n - 1


def connect_packet(ipid):
    if not 0x03 <= ipid <= 0xFE:
        raise ValueError(f"IP-ID out of range: {ipid:02X}")
    return frame(0x01, b"\x00\x00\x00\x00\x00" + bytes([ipid]) + b"\x40\xff\xff\xf1\x01")


def update_request():
    return frame(0x05, b"\x00\x00\x02\x03\x00")


def sync_done():
    """The panel's answer to the processor's end of the update dump (03 1C): 03 1D (klenae, Antonio112009)."""
    return frame(0x05, b"\x00\x00\x02\x03\x1d")


def heartbeat(answer=False):
    return frame(0x0E if answer else 0x0D, b"\x00\x00")


def login_packet(user, password):
    return frame(0x0B, b"\x00\x00" + f"{user}:{password}".encode("utf-8") + b"\x00\x00\x00")


def _digital_sub(join, value, press=True):
    j = _join0(join)
    return bytes([3, 0x27 if press else 0x00, j & 0xFF, (j >> 8) | (0 if value else 0x80)])


def _analog_sub(join, value):
    v = max(0, min(0xFFFF, int(value)))
    return bytes([5, 0x14]) + _join0(join).to_bytes(2, "big") + v.to_bytes(2, "big")


def _serial_bodies(join, text):
    j = _join0(join)
    if str(text).isascii():
        data, extra = str(text).encode("ascii"), 0
    else:
        data, extra = str(text).encode("utf-16-le"), 4
    parts = [data[i:i + SERIAL_CHUNK] for i in range(0, len(data), SERIAL_CHUNK)] or [b""]
    out = []
    for n, part in enumerate(parts):
        flags = extra | (1 if n == 0 else 0) | (2 if n == len(parts) - 1 else 0)
        out.append(bytes([0x34]) + j.to_bytes(2, "big") + bytes([flags]) + part)
    return out


def digital(join, value, so=None, press=True):
    sub = _digital_sub(join, value, press)
    if so:
        sub = bytes([len(sub) + 5, 0x38]) + _so_id(so) + sub
    return frame(0x05, b"\x00\x00" + sub)


def _so_id(so):
    """A smart object's id: 4 bytes, big-endian. The public sources read only the last byte (ids up to 255); ids above
    255 (e.g. 401, 530) work this way on a real processor."""
    so = int(so)
    if not 1 <= so <= 0xFFFFFFFF:
        raise ValueError(f"smart object id out of range: {so}")
    return so.to_bytes(4, "big")


def analog(join, value, so=None):
    sub = _analog_sub(join, value)
    if so:
        sub = bytes([len(sub) + 5, 0x38]) + _so_id(so) + sub
    return frame(0x05, b"\x00\x00" + sub)


def serial(join, text, so=None):
    frames = []
    for body in _serial_bodies(join, text):
        sub = len(body).to_bytes(2, "big") + body
        if so:
            inner = bytes([0x39]) + _so_id(so) + sub
            sub = len(inner).to_bytes(2, "big") + inner
        frames.append(frame(0x12, b"\x00\x00" + sub))
    return frames


def text_codec(encoding):
    """Check an encoding= name: it must be a text code page that keeps ASCII as ASCII (latin-1, cp1256, utf-8 ...).
    Names like 'hex', 'base64' or 'utf-16' would garble every message, so they raise ValueError at once."""
    try:
        name = codecs.lookup(encoding or "latin-1").name
        if "#1,".encode(name) == b"#1," and b"#1,".decode(name) == "#1,":
            return name
    except (LookupError, TypeError, ValueError, UnicodeError):
        pass
    raise ValueError("encoding %r is not an ASCII-compatible text code page" % (encoding,))


class Decoder:
    """Bytes in -> events out: ('d'|'a'|'s', so or 0, join, value) | ('registered',) | ('refused', why) |
    ('login', level) | ('program', status) | ('heartbeat',) | ('disconnect',) | ('synced',)."""

    def __init__(self, encoding="latin-1"):
        self.buf = bytearray()
        self.serials = {}
        self.encoding = text_codec(encoding)

    def text(self, raw):
        """Text without the Unicode flag, decoded with the session's encoding (default latin-1)."""
        return raw.decode(self.encoding, "replace")

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) >= 3:
            n = int.from_bytes(self.buf[1:3], "big")
            if len(self.buf) < 3 + n:
                break
            kind, payload = self.buf[0], bytes(self.buf[3:3 + n])
            del self.buf[:3 + n]
            try:
                out += self.frame(kind, payload)
            except (IndexError, ValueError):
                out.append(("bad", kind, payload[:40].hex()))
        return out

    def frame(self, kind, p):
        if kind == 0x0F:
            return [("program", p[0] if p else 2)]
        if kind == 0x02:
            return [("refused", "not defined")] if p == b"\xff\xff\x02" else [("registered",)]
        if kind == 0x0C:
            return [("login", p[-1] if len(p) > 2 else 0)]
        if kind == 0x0D:
            return [("heartbeat",)]
        if kind == 0x0E:
            return []
        if kind in (0x03, 0x04):
            return [("disconnect",)]
        if kind == 0x05:
            return list(self.subs(p[2:], False, 0))
        if kind == 0x12:
            return list(self.subs(p[2:], True, 0))
        return []

    def subs(self, data, wide, so):
        i, h = 0, 2 if wide else 1
        while i + h < len(data):
            n = int.from_bytes(data[i:i + h], "big")
            if n == 0 or i + h + n > len(data):
                return
            t, body = data[i + h], data[i + h + 1:i + h + n]
            i += h + n
            if t in (0x00, 0x27):
                for k in range(0, len(body) - 1, 2):
                    yield ("d", so, (body[k] | ((body[k + 1] & 0x7F) << 8)) + 1, 0 if body[k + 1] & 0x80 else 1)
            elif t == 0x14:
                for k in range(0, len(body) - 3, 4):
                    yield ("a", so, int.from_bytes(body[k:k + 2], "big") + 1, int.from_bytes(body[k + 2:k + 4], "big"))
            elif t == 0x01:
                if len(body) == 3:
                    yield ("a", so, body[0] + 1, int.from_bytes(body[1:3], "big"))
                elif len(body) >= 4:
                    yield ("a", so, int.from_bytes(body[0:2], "big") + 1, int.from_bytes(body[2:4], "big"))
            elif t in (0x34, 0x15) and len(body) >= 3:
                ev = self.serial_part(so, int.from_bytes(body[0:2], "big") + 1, body[2], body[3:])
                if ev:
                    yield ev
            elif t == 0x02:                                      # old ASCII serial: '#<join>,text\r', one or more
                for txt in re.split(r"\r\n?|\n", self.text(body)):   # \r, \r\n or \n between them
                    if txt[:1] in "#@" and "," in txt:
                        j, _, v = txt[1:].partition(",")
                        if j.isdigit():
                            yield ("s", so, int(j), v)
            elif t == 0x03 and body[:1] == b"\x1c":
                yield ("synced",)
            elif t in (0x38, 0x39) and len(body) >= 4:
                yield from self.subs(body[4:], wide, int.from_bytes(body[0:4], "big"))

    def serial_part(self, so, join, flags, data):
        key = (so, join)
        if flags & 1:
            self.serials[key] = [flags, bytearray(data)]
        elif key in self.serials:
            self.serials[key][1] += data
            self.serials[key][0] |= flags
        else:
            self.serials[key] = [flags | 1, bytearray(data)]
            flags |= 2
        if not flags & 2:
            return None
        f, buf = self.serials.pop(key)
        if f & 4:
            return ("s", so, join, bytes(buf).decode("utf-16-le", "replace"))
        return ("s", so, join, self.text(bytes(buf)))


class Session:
    """One XPanel connection to a processor. Not thread-safe: one thread reads (events()), sends and ticks."""

    def __init__(self, host, ipid, port=0, user="", password="", timeout=10.0, log=None, encoding="latin-1"):
        self.host, self.ipid, self.port = host, int(ipid), int(port or 0)
        self.enc = text_codec(encoding)
        self.user, self.password, self.timeout = user or "", password or "", timeout
        self.sock, self.dec = None, Decoder(self.enc)
        self.last_rx = self.last_tx = time.monotonic()
        self.held = {}                  # (so, join) -> pressed at
        self.log = log or (lambda *a: None)
        self.used_port, self.tls = 0, False
        self.raw_log = []               # first frames received, hex
        self.login_note = ""

    # ---- connecting
    def _open(self, port, tls):
        s = socket.create_connection((self.host, port), timeout=self.timeout)
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE          # processors use self-signed certificates
            s = ctx.wrap_socket(s, server_hostname=self.host)
        return s

    def open(self):
        """Connect and register. Returns the first events (join values follow). Raises CipError with a reason.
        A refused login is tried once more without one: a processor without authentication may refuse any login."""
        try:
            return self._open_once()
        except LoginRefused:
            if not self.user:
                raise
            self.user, self.password = "", ""
            self.dec = Decoder(self.enc)
            try:
                out = self._open_once()
            except CipError as e:
                raise CipError(f"The processor refused the login (its saved user name / password), and without a login: {e}") from None
            self.login_note = "The processor refused the saved login; connected without a login."
            return out

    def _open_once(self):
        tries = [(self.port, self.port == TLS_PORT)] if self.port else [(PORT, False), (TLS_PORT, True)]
        last = None
        for port, tls in tries:
            try:
                self.sock = self._open(port, tls)
                self.used_port, self.tls = port, tls
                break
            except (OSError, ssl.SSLError) as e:
                last = e
        if not self.sock:
            raise CipError(f"Cannot reach {self.host} on CIP port{'s' if len(tries) > 1 else ''} "
                           f"{' / '.join(str(p) for p, _ in tries)}: {getattr(last, 'strerror', None) or last}")
        deadline = time.monotonic() + self.timeout
        got_prompt, early = False, []
        self.sock.settimeout(1.0)
        while time.monotonic() < deadline and not got_prompt:
            for ev in self._recv():
                if ev[0] == "program":
                    got_prompt = True
                    if ev[1] in (0, 1):
                        self.close()
                        raise CipError("The processor's program is " + ("loading" if ev[1] == 0 else "not running")
                                       + " - try again when it runs.")
                else:
                    early.append(ev)
        if not got_prompt:                           # some firmware does not prompt: register anyway
            self.log("No 'register' prompt from the processor in %ss - registering anyway" % self.timeout)
        self.send_raw(connect_packet(self.ipid))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            for ev in self._recv():
                if ev[0] == "refused":
                    self.close()
                    raise CipError(f"The processor refused IP-ID {self.ipid:02X}: it is not defined in the running program "
                                   "(or the program is still starting).")
                if ev[0] == "registered":
                    if self.user:
                        self.send_raw(login_packet(self.user, self.password))
                        if not self._wait_login():
                            self.close()
                            raise LoginRefused("The processor refused the login (its saved user name / password).")
                    self.send_raw(update_request())
                    return early
                early.append(ev)
        self.close()
        raise CipError(f"No answer from the processor to the IP-ID {self.ipid:02X} registration within {self.timeout:.0f} s.")

    def _wait_login(self):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            for ev in self._recv():
                if ev[0] == "login":
                    return ev[1] > 0
        return True                                  # no answer: carry on (the processor may not need a login)

    # ---- I/O
    def fileno(self):
        return self.sock.fileno()

    def pending(self):
        return isinstance(self.sock, ssl.SSLSocket) and self.sock.pending() > 0

    def _recv(self):
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return []
        except ssl.SSLWantReadError:
            return []
        if not data:
            raise CipError("The processor closed the connection.")
        self.last_rx = time.monotonic()
        evs = self.dec.feed(data)
        if len(self.raw_log) < 40:
            self.raw_log.append(data[:300].hex())
        return evs

    def events(self):
        """Read what is waiting (call when the socket is readable). Answers heartbeats itself."""
        out = []
        for ev in self._recv():
            if ev[0] == "heartbeat":
                self.send_raw(heartbeat(True))
            elif ev[0] == "disconnect":
                raise CipError("The processor ended the connection (its program stopped or restarted).")
            elif ev[0] in ("d", "a", "s", "synced", "bad"):
                if ev[0] == "synced":                    # acknowledge the end of the dump, as panels do
                    self.send_raw(sync_done())
                    self.send_raw(heartbeat())
                out.append(ev)
        return out

    def send_raw(self, b):
        self.sock.sendall(b)
        self.last_tx = time.monotonic()

    def set(self, kind, join, value, so=0, press=True):
        """A join from the panel: 'd' (press: 1 / release: 0 - held until released; press=False sets a level, as a
        check box does), 'a' (0-65535), 's' (text)."""
        so = int(so or 0)
        if kind == "d":
            on = bool(int(value))
            self.send_raw(digital(join, on, so, press))
            if on and press:
                self.held[(so, int(join))] = time.monotonic()
            else:
                self.held.pop((so, int(join)), None)
        elif kind == "a":
            self.send_raw(analog(join, int(value), so))
        elif kind == "s":
            for f in serial(join, str(value)[:2000], so):
                self.send_raw(f)
        else:
            raise ValueError("join kind: d, a or s")

    def tick(self):
        """Call at least every 0.4 s: heartbeat, held presses, dead link."""
        now = time.monotonic()
        if now - self.last_rx > DEAD_AFTER:
            raise CipError(f"No answer from the processor for {DEAD_AFTER:.0f} s - the connection is lost.")
        for (so, j), t0 in list(self.held.items()):
            if now - t0 > MAX_HOLD:
                self.held.pop((so, j), None)
                self.send_raw(digital(j, False, so))
            else:
                self.send_raw(digital(j, True, so))
        if now - self.last_tx >= HEARTBEAT:
            self.send_raw(heartbeat())

    def close(self):
        if self.sock:
            try:
                for (so, j) in list(self.held):
                    self.sock.sendall(digital(j, False, so))
                self.sock.sendall(frame(0x03, b""))
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.held.clear()
