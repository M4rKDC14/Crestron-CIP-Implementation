# Crestron CIP Implementation

A small Python client for Crestron's CIP protocol. It connects to a control processor as a touch panel (XPanel)
on an IP-ID and exchanges digital, analog and serial joins, including smart-object joins.

> **Status: used live against a real control processor** (registration, digital, analog, serial and smart-object
> joins, including smart-object ids above 255). The frame formats follow what several open-source CIP projects agree
> on (see *Sources*). Since that test: non-Unicode text uses the `encoding=` option (checked up front), and two
> additions (the end-of-sync answer, several old ASCII serials in one packet, with any line ending) are covered by
> the tests only.

## Use

Python 3.8+, standard library only. Copy `crestron_cip.py` into your project.

```python
import select
import crestron_cip as cip

s = cip.Session("192.0.2.10", 0x03)          # processor address, IP-ID defined in its program
s.open()                                      # register (and log in, if user= / password= are given)
s.set("d", 17, 1)                             # press digital join 17 ...
s.set("d", 17, 0)                             # ... and release it
s.set("a", 5, 32768)                          # analog join 5
s.set("s", 3, "Hello")                        # serial join 3
s.set("d", 1, 1, so=10)                       # digital join 1 of smart object 10

while True:
    if select.select([s], [], [], 0.4)[0] or s.pending():
        for ev in s.events():                 # ('d'|'a'|'s', smart object or 0, join, value), ('synced',) ...
            print(ev)
    s.tick()                                  # heartbeat, held presses, dead-link check: call every 0.4 s
```

- Joins are 1-based. A digital press is held, and re-sent, until you release it. Pass `press=False` to set a
  level instead.
- `Session(..., port=41796)` uses TLS (4-Series). With no port, 41794 is tried first, then 41796.
- Text without the Unicode flag is decoded with `encoding=` (default `latin-1`; e.g. `cp1256` for Arabic). The name
  is checked when the `Session` is made: an unknown name, or one that does not keep ASCII as ASCII (`utf-16`, `hex`
  ...), raises `ValueError` instead of breaking the connection later. Text with non-ASCII characters is sent as
  UTF-16LE.
- Old-style text packets with several values are split on `\r`, `\r\n` or `\n`.
- Smart-object ids are sent and read as 4 bytes, big-endian, so ids above 255 work (the public sources read only
  the last byte).
- `Session.raw_log` keeps the first frames received, in hex, for troubleshooting.

## Protocol summary

See the docstring at the top of `crestron_cip.py`: frame layout, registration, update request and end of sync,
heartbeat, digital / analog / serial / smart-object sub-packets, and the 4-Series login.

## Tests

```
python -m unittest discover -s tests
```

`tests/fake_processor.py` is a stand-in processor written from the same notes, so the tests run without hardware.

## Sources

Written from the public protocol notes of these projects. No code was copied from any of them, and no Crestron code
was used:

- [klenae/python-cipclient](https://github.com/klenae/python-cipclient) (MIT)
- [Antonio112009/pycrestron-cip](https://github.com/Antonio112009/pycrestron-cip) (MIT)
- [rdriscoll/Crestron-CIP](https://github.com/rdriscoll/Crestron-CIP) (MIT)
- [CommandFusion/CIP](https://github.com/CommandFusion/CIP) (no licence; read only)
- [akellai/scip2cip](https://github.com/akellai/scip2cip) (no licence; read only)

Crestron, XPanel and the CIP protocol are Crestron Electronics, Inc.'s. This project is not affiliated with or
endorsed by Crestron.

## Licence

MIT. See [LICENSE](LICENSE).
