#!/usr/bin/env python3
"""
Servidor suplantador de la nube Cecotec para el Conga 8090 Ultra.

FASE 1 (este esqueleto): que el robot se conecte a NUESTRO servidor y se quede
estable, respondiendo al login y al heart-beat, y registrando el report_data.
NO controla el robot todavia; solo sostiene la conexion y observa.

Requisitos:
  - cert.pem y key.pem en la misma carpeta (los autofirmados que ya generaste).
  - DNS: tcp-cecotec.3irobotix.net -> IP de la maquina que corre este script.
  - Puerto 9090 abierto en el firewall.

Uso:
  python servidor_conga.py

Que esperar si funciona:
  [ROBOT] TLS OK
  [WS] handshake completado
  <-- LOGIN  ...
  --> respondo LOGIN code:0
  <-- HEARTBEAT
  --> respondo HEARTBEAT
  <-- REPORT_DATA (bateria=..., modo=...)
  ... y el robot NO deberia resetear ni reconectar en bucle.
"""

import socket, ssl, threading, hashlib, base64, struct, json, time, sys

LISTEN_PORT = 9090
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# --- Datos de ESTE robot (del login capturado). Ajusta si cambian. ---
ROBOT_DID = 123456
ROBOT_USERID = 654321
ROBOT_SN = "500400000000"
ROBOT_MAC = "12:34:56:78:9A:BC"
FACTORY_ID = "1003"
PROJECT_TYPE = "CECOTECCRL350-1001"
# JWT capturado de la nube real. Probamos a devolverlo tal cual.
AUTH_JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiJFWEFNUExFIiwibmFtZSI6IkVYQU1QTEUiLCJpYXQiOjE1MTZ9."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")


def now_ms():
    return str(int(time.time() * 1000))


# ---------------- utilidades WebSocket ----------------
def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return buf if buf else None
        buf += chunk
    return buf


def recv_http_headers(sock):
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = sock.recv(1)
        if not c:
            break
        buf += c
    return buf


def ws_read_frame(sock):
    """Devuelve (opcode, payload_bytes) o (None, None) si se cierra."""
    hdr = recv_exact(sock, 2)
    if not hdr:
        return None, None
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


def ws_send(sock, data, opcode=0x1):
    """Envia un frame SIN mascara (lado servidor). data puede ser str o bytes."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    b1 = 0x80 | opcode
    length = len(data)
    if length < 126:
        header = struct.pack("!BB", b1, length)
    elif length < 65536:
        header = struct.pack("!BBH", b1, 126, length)
    else:
        header = struct.pack("!BBQ", b1, 127, length)
    sock.sendall(header + data)


# ---------------- logica de la aplicacion ----------------
def handle_message(sock, payload):
    """Recibe un payload (bytes) del robot y responde lo que corresponda."""
    # Frames keep-alive tipo 'libuwsc'
    if payload.strip() == b"libuwsc":
        print("    [keepalive] libuwsc")
        ws_send(sock, b"libuwsc")
        return

    try:
        msg = json.loads(payload.decode("utf-8"))
    except Exception:
        print(f"    [??] payload no-JSON ({len(payload)}b): {payload[:80]!r}")
        return

    service = msg.get("service", "")
    trace = msg.get("traceId", "")
    trace_str = str(trace)

    # ---- LOGIN ----
    if service.endswith("auth/login"):
        print(f"    <-- LOGIN (trace {trace})")
        resp = {
            "code": 0,
            "traceId": trace_str,
            "service": "sweeper-robot-center/auth/login",
            "result": {
                "data": {
                    "AUTH": AUTH_JWT,
                    "FACTORY_ID": FACTORY_ID,
                    "USERNAME": ROBOT_SN,
                    "CONNECTION_TYPE": "sweeper",
                    "PROJECT_TYPE": PROJECT_TYPE,
                    "ROBOT_TYPE": "sweeper",
                    "SN": ROBOT_SN,
                    "MAC": ROBOT_MAC,
                    "BIND_LIST": f"[\"{ROBOT_USERID}\"]",
                },
                "clientType": "ROBOT",
                "id": str(ROBOT_DID),
                "resetCode": 0,
            },
        }
        ws_send(sock, json.dumps(resp))
        print("    --> LOGIN OK enviado")
        return

    # ---- HEART-BEAT ----
    if service == "heart-beat":
        resp = {
            "code": 0,
            "traceId": trace_str,
            "service": "heart-beat",
            "result": now_ms(),
        }
        ws_send(sock, json.dumps(resp))
        print(f"    <-> heartbeat (trace {trace})")
        return

    # ---- REPORT_DATA (estado) ----
    if service.endswith("device/report_data"):
        # Intentamos leer bateria/estado del content para mostrarlo
        info = ""
        try:
            content = json.loads(msg.get("content", "{}"))
            data = content.get("data", {})
            info = (f"bateria={data.get('battary')} "
                    f"charge={data.get('chargeStatus')} "
                    f"workMode={data.get('workMode')} "
                    f"fault={data.get('faultCode')}")
        except Exception:
            pass
        print(f"    <-- REPORT_DATA {info}")
        resp = {
            "code": 0,
            "traceId": trace_str,
            "service": "sweeper-robot-center/device/report_data",
            "result": True,
        }
        ws_send(sock, json.dumps(resp))
        return

    # ---- Cualquier otro service: respondemos genericamente code:0 ----
    print(f"    <-- {service} (trace {trace}) [respondo generico]")
    resp = {
        "code": 0,
        "traceId": trace_str,
        "service": service.split("?")[0],
        "result": True,
    }
    ws_send(sock, json.dumps(resp))


def ws_handshake(tls_sock):
    req = recv_http_headers(tls_sock)
    key = None
    for line in req.split(b"\r\n"):
        if line.lower().startswith(b"sec-websocket-key:"):
            key = line.split(b":", 1)[1].strip().decode()
    if not key:
        return False
    accept = base64.b64encode(
        hashlib.sha1((key + WS_MAGIC).encode()).digest()
    ).decode()
    tls_sock.sendall((
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode())
    return True


def handle_client(tls_sock, addr):
    try:
        if not ws_handshake(tls_sock):
            print("  [WS] handshake fallido")
            tls_sock.close()
            return
        print("  [WS] handshake completado ✓  (robot conectado)")
        while True:
            opcode, payload = ws_read_frame(tls_sock)
            if opcode is None:
                print("  [conn] robot cerro la conexion")
                break
            if opcode == 0x8:
                print("  [conn] frame CLOSE recibido")
                break
            if opcode in (0x9,):  # ping
                ws_send(tls_sock, payload or b"", opcode=0xA)  # pong
                continue
            if payload:
                handle_message(tls_sock, payload)
    except Exception as e:
        print(f"  [conn] error: {e}")
    finally:
        try:
            tls_sock.close()
        except Exception:
            pass


def main():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(certfile="cert.pem", keyfile="key.pem")
    except Exception as e:
        print(f"ERROR cargando cert.pem/key.pem: {e}")
        print("Genera los certificados o ponlos en esta carpeta.")
        sys.exit(1)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.set_ciphers("ALL:@SECLEVEL=0")
    except Exception:
        pass

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", LISTEN_PORT))
    s.listen(5)
    print(f"[SERVIDOR] Suplantador Conga escuchando en 0.0.0.0:{LISTEN_PORT}")
    print("[SERVIDOR] Esperando al robot... (reinicia el robot con corte real)")

    while True:
        raw, addr = s.accept()
        print(f"\n[conexion] desde {addr[0]}")
        try:
            tls_sock = ctx.wrap_socket(raw, server_side=True)
            print("  [TLS] handshake OK ✓")
        except ssl.SSLError as e:
            print(f"  [TLS] rechazado: {e}")
            raw.close()
            continue
        except Exception as e:
            print(f"  [TLS] error: {e}")
            raw.close()
            continue
        threading.Thread(target=handle_client, args=(tls_sock, addr),
                         daemon=True).start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[SERVIDOR] Detenido.")
