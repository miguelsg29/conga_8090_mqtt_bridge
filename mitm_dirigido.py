#!/usr/bin/env python3
"""
MITM dirigido: puente robot <-> nube real de Cecotec, que RESALTA y guarda
aparte los comandos de control (set_mode, set_direct, get_map, etc.).

Objetivo: pulsar en la app oficial, uno a uno, los botones de:
  - INICIAR limpieza
  - PAUSAR
  - REANUDAR
  - ENVIAR A BASE (home)
  - (y lo que quieras: spot, spiral, fan speed, water level...)
...y ver EXACTAMENTE que set_mode/control genera cada uno.

Requisitos: cert.pem, key.pem, DNS tcp-cecotec -> este PC, puerto 9090 abierto.
IMPORTANTE: pon la IP real de la nube en CLOUD_IP (nslookup tcp-cecotec... 8.8.8.8)

Uso:
  python mitm_dirigido.py

Los comandos de control se muestran en consola con >>> y se guardan en
cap_comandos.log (solo los control, limpio). Todo el trafico va a cap_mitm_full.log
"""

import socket, ssl, threading, hashlib, base64, struct, os, json, sys
from datetime import datetime

# Salida en UTF-8: en Windows (cp1252) imprimir "✓" lanzaba UnicodeEncodeError y
# tiraba la conexion del robot. Con esto los prints nunca fallan.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROBOT_LISTEN_PORT = 9090
CLOUD_IP = "43.158.121.228"      # IP real de tcp-cecotec.3irobotix.net
CLOUD_HOST = "tcp-cecotec.3irobotix.net"
CLOUD_PORT = 9090
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Servicios de "ruido" que no queremos resaltar como comando
NOISE = ("heart-beat", "report_data", "syn_no_cache", "info_report",
         "get_notice_config", "get_pets", "stuff/config")


def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log_full(direction, data):
    try:
        pretty = data.decode("utf-8")
    except Exception:
        pretty = data.hex(" ")
    with open("cap_mitm_full.log", "a", encoding="utf-8") as f:
        f.write(f"\n[{ts()}] {direction} ({len(data)}b)\n{pretty}\n")


def maybe_highlight_command(direction, data):
    """Si el mensaje contiene un 'control' interesante, lo resalta y guarda."""
    try:
        text = data.decode("utf-8")
    except Exception:
        return
    if '"control"' not in text and '"set_mode"' not in text:
        return
    if any(n in text for n in NOISE):
        # aun asi, si trae set_mode explicito lo dejamos pasar
        if "set_mode" not in text and "set_direct" not in text:
            return
    # Intentar extraer el objeto control legible
    line = f"[{ts()}] {direction}: {text}"
    print(f"\n>>> {line}\nconga-mitm> ", end="", flush=True)
    with open("cap_comandos.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------- WebSocket helpers ----------
def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk: return buf if buf else None
        buf += chunk
    return buf

def recv_http_headers(sock):
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = sock.recv(1)
        if not c: break
        buf += c
    return buf

def ws_read_frame(sock):
    hdr = recv_exact(sock, 2)
    if not hdr: return None, None
    b1, b2 = hdr[0], hdr[1]
    opcode = b1 & 0x0f
    masked = (b2 & 0x80) != 0
    length = b2 & 0x7f
    if length == 126:
        length = struct.unpack(">H", recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recv_exact(sock, 8))[0]
    mask = recv_exact(sock, 4) if masked else b""
    payload = recv_exact(sock, length) if length else b""
    if masked and payload:
        payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
    return opcode, payload

def ws_write_frame(sock, data, opcode=0x1, mask=False):
    b1 = 0x80 | opcode
    length = len(data)
    m_bit = 0x80 if mask else 0
    if length < 126:
        header = struct.pack("!BB", b1, length | m_bit)
    elif length < 65536:
        header = struct.pack("!BBH", b1, 126 | m_bit, length)
    else:
        header = struct.pack("!BBQ", b1, 127 | m_bit, length)
    if mask:
        mk = os.urandom(4)
        md = bytes(data[i] ^ mk[i % 4] for i in range(len(data)))
        sock.sendall(header + mk + md)
    else:
        sock.sendall(header + data)


def connect_cloud():
    raw = socket.create_connection((CLOUD_IP, CLOUD_PORT))
    cctx = ssl.create_default_context()
    cctx.check_hostname = False
    cctx.verify_mode = ssl.CERT_NONE
    try: cctx.set_ciphers("ALL:@SECLEVEL=0")
    except Exception: pass
    tls = cctx.wrap_socket(raw, server_hostname=CLOUD_HOST)
    key = base64.b64encode(os.urandom(16)).decode()
    tls.sendall((f"GET / HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                 f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                 f"Host: {CLOUD_HOST}:{CLOUD_PORT}\r\n\r\n").encode())
    resp = recv_http_headers(tls)
    print("  [CLOUD] WS:", resp.split(b"\r\n")[0].decode(errors="replace"))
    return tls


def ws_handshake_robot(tls):
    req = recv_http_headers(tls)
    key = None
    for l in req.split(b"\r\n"):
        if l.lower().startswith(b"sec-websocket-key:"):
            key = l.split(b":", 1)[1].strip().decode()
    accept = base64.b64encode(hashlib.sha1((key+WS_MAGIC).encode()).digest()).decode()
    tls.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                 "Connection: Upgrade\r\n"
                 f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
    print("  [ROBOT] WS OK ✓")


def handle(robot_tls):
    ws_handshake_robot(robot_tls)
    print("  conectando a la nube real...")
    cloud = connect_cloud()
    print("  puente establecido. AHORA pulsa botones en la app y observa >>>\n")

    def r2c():
        try:
            while True:
                op, pl = ws_read_frame(robot_tls)
                if op is None or op == 0x8: break
                if pl:
                    log_full("ROBOT->CLOUD", pl)
                    maybe_highlight_command("ROBOT->CLOUD", pl)
                ws_write_frame(cloud, pl, opcode=op or 0x1, mask=True)
        except Exception as e: print("  r2c fin:", e)
        finally:
            for s in (robot_tls, cloud):
                try: s.close()
                except: pass

    def c2r():
        try:
            while True:
                op, pl = ws_read_frame(cloud)
                if op is None or op == 0x8: break
                if pl:
                    log_full("CLOUD->ROBOT", pl)
                    maybe_highlight_command("CLOUD->ROBOT", pl)
                ws_write_frame(robot_tls, pl, opcode=op or 0x1, mask=False)
        except Exception as e: print("  c2r fin:", e)
        finally:
            for s in (robot_tls, cloud):
                try: s.close()
                except: pass

    threading.Thread(target=r2c, daemon=True).start()
    threading.Thread(target=c2r, daemon=True).start()


def main():
    if CLOUD_IP == "PON_LA_IP":
        print("Edita CLOUD_IP con la IP de: nslookup tcp-cecotec.3irobotix.net 8.8.8.8")
        return
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(certfile="cert.pem", keyfile="key.pem")
    sctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try: sctx.set_ciphers("ALL:@SECLEVEL=0")
    except Exception: pass

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", ROBOT_LISTEN_PORT)); s.listen(5)
    print(f"[MITM] escuchando robot en {ROBOT_LISTEN_PORT} -> nube {CLOUD_IP}")
    print("[MITM] Los comandos de control apareceran con >>>\n")
    while True:
        raw, addr = s.accept()
        print(f"[conexion] desde {addr[0]}")
        try:
            robot_tls = sctx.wrap_socket(raw, server_side=True)
            print("  [ROBOT] TLS OK ✓")
            handle(robot_tls)
        except Exception as e:
            print("  error:", e)
            raw.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[MITM] detenido.")
