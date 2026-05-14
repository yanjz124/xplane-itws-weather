"""Minimal stdlib-only WebSocket client for SwimReader ITWS invalidation events.

Connects to /itws/ws (or /itws/ws/{ICAO}), reads update messages, calls a
callback with ('update', productType, site) so the plugin can refetch the
specific product immediately instead of waiting for the next 60s poll tick.

Falls back silently if the connection fails — caller should treat WS as a
latency optimization, not a requirement.
"""

import base64
import json
import os
import socket
import ssl
import struct
import threading
import urllib.parse


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def _handshake(sock, host, path):
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"User-Agent: xplane-itws-weather/0.4\r\n"
        f"\r\n"
    )
    sock.sendall(req.encode("ascii"))
    # Read until \r\n\r\n
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("server closed during handshake")
        buf += chunk
        if len(buf) > 65536:
            raise ConnectionError("oversized handshake")
    head, _, leftover = buf.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    if "101" not in status_line:
        raise ConnectionError(f"bad handshake status: {status_line!r}")
    return leftover  # any frame bytes already received


def _read_frame(sock, prebuf=b""):
    """Read one WebSocket frame, return (opcode, payload, leftover_buffer)."""
    def need(n, buf):
        while len(buf) < n:
            chunk = sock.recv(max(4096, n - len(buf)))
            if not chunk:
                raise ConnectionError("server closed mid-frame")
            buf += chunk
        return buf

    buf = need(2, prebuf)
    b1, b2 = buf[0], buf[1]
    opcode = b1 & 0x0F
    masked = (b2 & 0x80) != 0
    plen = b2 & 0x7F
    used = 2
    if plen == 126:
        buf = need(used + 2, buf)
        plen = struct.unpack("!H", buf[used:used + 2])[0]
        used += 2
    elif plen == 127:
        buf = need(used + 8, buf)
        plen = struct.unpack("!Q", buf[used:used + 8])[0]
        used += 8
    if masked:
        buf = need(used + 4, buf)
        mask = buf[used:used + 4]
        used += 4
    buf = need(used + plen, buf)
    payload = buf[used:used + plen]
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload, buf[used + plen:]


def _send_frame(sock, opcode, payload=b""):
    # Client frames must be masked.
    b1 = 0x80 | (opcode & 0x0F)
    plen = len(payload)
    if plen < 126:
        header = struct.pack("!BB", b1, 0x80 | plen)
    elif plen < 65536:
        header = struct.pack("!BBH", b1, 0x80 | 126, plen)
    else:
        header = struct.pack("!BBQ", b1, 0x80 | 127, plen)
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(header + mask + masked)


class WSClient(threading.Thread):
    """Background thread: maintain WS connection and call on_event(msg_dict).

    msg_dict is the parsed JSON payload from each text frame. Schema is
    SwimReader-defined (typically includes 'type', 'productType', 'site',
    'messageTime'). The plugin decides what to do with it.
    """

    def __init__(self, base_url, on_event, icao=None, on_error=None):
        super().__init__(daemon=True, name="ITWS-WS")
        self.base_url = base_url
        self.on_event = on_event
        self.on_error = on_error or (lambda exc: None)
        self.icao = icao
        self._stop = threading.Event()
        self._sock = None

    def stop(self):
        self._stop.set()
        try:
            if self._sock is not None:
                self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def run(self):
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self._connect_and_pump()
                backoff = 2.0
            except (ConnectionError, OSError, ssl.SSLError, ValueError) as e:
                self.on_error(e)
                # Sleep with stop-check.
                for _ in range(int(backoff)):
                    if self._stop.is_set():
                        return
                    self._stop.wait(1.0)
                backoff = min(backoff * 2, 60.0)

    def _connect_and_pump(self):
        u = urllib.parse.urlparse(self.base_url)
        host = u.hostname
        port = u.port or (443 if u.scheme in ("https", "wss") else 80)
        use_tls = u.scheme in ("https", "wss")
        path = "/itws/ws" + (f"/{urllib.parse.quote(self.icao)}" if self.icao else "")

        sock = socket.create_connection((host, port), timeout=15)
        if use_tls:
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=host)
        self._sock = sock

        try:
            leftover = _handshake(sock, host, path)
            sock.settimeout(60)  # ping if idle longer than this
            while not self._stop.is_set():
                opcode, payload, leftover = _read_frame(sock, leftover)
                if opcode == OP_TEXT:
                    try:
                        msg = json.loads(payload.decode("utf-8", errors="replace"))
                    except ValueError:
                        continue
                    try:
                        self.on_event(msg)
                    except Exception as e:  # noqa: BLE001 — never crash WS thread
                        self.on_error(e)
                elif opcode == OP_PING:
                    _send_frame(sock, OP_PONG, payload)
                elif opcode == OP_CLOSE:
                    _send_frame(sock, OP_CLOSE, b"")
                    return
        finally:
            try:
                sock.close()
            except OSError:
                pass
            self._sock = None
