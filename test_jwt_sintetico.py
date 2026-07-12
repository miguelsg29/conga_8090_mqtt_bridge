#!/usr/bin/env python3
"""
PRUEBA: ¿el Conga 8090 valida el JWT del login, o acepta cualquiera?

Hipotesis: el robot NO valida la firma del JWT; solo necesita code:0 y una
respuesta bien formada. Si es cierto, podemos generar un JWT sintetico en el
propio puente y OLVIDARNOS de la caducidad del token capturado.

Este script es un servidor de login minimo que responde con un JWT SINTETICO
(firma falsa, sin timestamp/caducidad). Si el robot se conecta, hace login y se
queda mandando heart-beat y report_data de forma estable => hipotesis CONFIRMADA.

Como usarlo:
  1. Para el puente/servidor de produccion (no pueden compartir el puerto 9090).
  2. cert.pem y key.pem en esta carpeta.
  3. DNS tcp-cecotec -> IP de esta maquina, puerto 9090 abierto.
  4. python test_jwt_sintetico.py
  5. Reinicia el robot (corte de energia real).

Que observar:
  - "LOGIN recibido" y "LOGIN OK (JWT sintetico) enviado"
  - luego heart-beat y report_data repitiendose SIN reconexiones en bucle
  => el robot acepta el JWT falso. Podemos generar uno propio y no depender del real.

  Si en cambio el robot se desconecta y reintenta el login una y otra vez,
  => el robot SI valida algo del JWT y hay que usar el capturado (o investigar mas).
"""

import socket, ssl, threading, hashlib, base64, struct, json, time, sys

LISTEN_PORT = 9090
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Datos de ejemplo (rellena con los de tu .env si quieres, no afectan a la prueba)
ROBOT_DID = 123456
ROBOT_USERID = 654321
ROBOT_SN = "500400000000"
ROBOT_MAC = "12:34:56:78:9A:BC"
FACTORY_ID = "1003"
PROJECT_TYPE = "CECOTECCRL350-1001"


def b64url(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def make_synthetic_jwt():
    """Genera un JWT con estructura valida, firma FALSA y SIN caducidad."""
    header = {"typ": "JWT", "alg": "HS256"}
    payload = {
        "value": json.dumps({
            "data": {"FACTORY_ID": FACTORY_ID},
            "clientType": "ROBOT",
            "id": str(ROBOT_DID),
            "resetCode": 0,
        }),
        "version": None, "scope": None, "timestamp": None,  # sin caducidad
    }
    h = b64url(json.dumps(header, separators=(",", ":")).encode())
    p = b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = "SYNTHETIC0SIGNATURE0NO0VALIDATION0NEEDED000000000000000000000"
    return f"{h}.{p}.{sig}"


AUTH_JWT = make_synthetic_jwt()


def now_ms():
    return str(int(time.time() * 1000))


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            return buf if buf else None
        buf += c
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


def handle_message(sock, payload, stats):
    if payload.strip() == b"libuwsc":
        ws_send(sock, b"libuwsc")
        return
    try:
        msg = json.loads(payload.decode("utf-8"))
    except Exception:
        return
    service = msg.get("service", "")
    trace = str(msg.get("traceId", ""))

    if service.endswith("auth/login"):
        stats["logins"] += 1
        print(f"  <-- LOGIN recibido (nº {stats['logins']})")
        if stats["logins"] > 1:
            print("      ⚠️ OJO: es un login repetido. Si se repite en bucle,")
            print("         el robot podria estar rechazando el JWT sintetico.")
        resp = {"code": 0, "traceId": trace,
                "service": "sweeper-robot-center/auth/login",
                "result": {"data": {
                    "AUTH": AUTH_JWT, "FACTORY_ID": FACTORY_ID,
                    "USERNAME": ROBOT_SN, "CONNECTION_TYPE": "sweeper",
                    "PROJECT_TYPE": PROJECT_TYPE, "ROBOT_TYPE": "sweeper",
                    "SN": ROBOT_SN, "MAC": ROBOT_MAC,
                    "BIND_LIST": f"[\"{ROBOT_USERID}\"]"},
                    "clientType": "ROBOT", "id": str(ROBOT_DID),
                    "resetCode": 0}}
        ws_send(sock, json.dumps(resp))
        print("  --> LOGIN OK (JWT SINTÉTICO) enviado")
        return

    if service == "heart-beat":
        stats["heartbeats"] += 1
        ws_send(sock, json.dumps({"code": 0, "traceId": trace,
                                  "service": "heart-beat", "result": now_ms()}))
        # Cada 5 latidos, informar de estabilidad
        if stats["heartbeats"] % 5 == 0:
            print(f"  <-> {stats['heartbeats']} heartbeats, "
                  f"{stats['reports']} reports, {stats['logins']} login(s). "
                  f"{'ESTABLE ✓' if stats['logins']==1 else 'revisar logins'}")
        return

    if service.endswith("device/report_data"):
        stats["reports"] += 1
        ws_send(sock, json.dumps({"code": 0, "traceId": trace,
                "service": "sweeper-robot-center/device/report_data",
                "result": True}))
        return

    ws_send(sock, json.dumps({"code": 0, "traceId": trace,
            "service": service.split("?")[0], "result": True}))


def ws_handshake(tls):
    req = recv_http_headers(tls)
    key = None
    for l in req.split(b"\r\n"):
        if l.lower().startswith(b"sec-websocket-key:"):
            key = l.split(b":", 1)[1].strip().decode()
    if not key:
        return False
    accept = base64.b64encode(
        hashlib.sha1((key + WS_MAGIC).encode()).digest()).decode()
    tls.sendall((
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
    return True


def handle_client(tls, addr, stats):
    try:
        if not ws_handshake(tls):
            tls.close(); return
        print("  [WS] conectado ✓")
        while True:
            opcode, payload = ws_read_frame(tls)
            if opcode is None or opcode == 0x8:
                print("  [conn] robot desconectado")
                print(f"        Resumen: {stats['logins']} login(s), "
                      f"{stats['heartbeats']} heartbeats, {stats['reports']} reports")
                if stats["logins"] == 1 and stats["heartbeats"] > 3:
                    print("        ✅ Se mantuvo estable con 1 solo login "
                          "=> JWT sintetico ACEPTADO")
                break
            if opcode == 0x9:
                ws_send(tls, payload or b"", opcode=0xA)
                continue
            if payload:
                handle_message(tls, payload, stats)
    except Exception as e:
        print(f"  [conn] error: {e}")
    finally:
        try: tls.close()
        except Exception: pass


def main():
    print("=== PRUEBA JWT SINTÉTICO ===")
    print(f"JWT que se enviara al robot:\n  {AUTH_JWT}\n")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(certfile="cert.pem", keyfile="key.pem")
    except Exception as e:
        print(f"ERROR cert.pem/key.pem: {e}")
        sys.exit(1)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try: ctx.set_ciphers("ALL:@SECLEVEL=0")
    except Exception: pass

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", LISTEN_PORT)); s.listen(5)
    print(f"[SERVIDOR] escuchando en 0.0.0.0:{LISTEN_PORT}")
    print("[SERVIDOR] reinicia el robot con corte de energia real.\n")
    while True:
        raw, addr = s.accept()
        print(f"[conexion] desde {addr[0]}")
        stats = {"logins": 0, "heartbeats": 0, "reports": 0}
        try:
            tls = ctx.wrap_socket(raw, server_side=True)
            print("  [TLS] OK ✓")
        except Exception as e:
            print(f"  [TLS] error: {e}")
            raw.close(); continue
        threading.Thread(target=handle_client, args=(tls, addr, stats),
                         daemon=True).start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[SERVIDOR] detenido.")
