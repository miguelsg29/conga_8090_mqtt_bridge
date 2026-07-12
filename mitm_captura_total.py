#!/usr/bin/env python3
"""
MITM de captura EXHAUSTIVA para el Conga 8090.

Puente robot <-> nube real que captura, decodifica y CLASIFICA todos los
comandos de control que manda la app oficial. Pensado para descubrir:
  - boton localizar / buscar robot
  - limpieza por habitaciones / zonas / segmentos
  - modos de potencia de aspiracion (fan speed)
  - niveles de agua (mopa)
  - limpieza programada
  - cualquier otro control que probemos en la app

Requisitos: cert.pem, key.pem, DNS tcp-cecotec -> este PC, puerto 9090 abierto.
IP real de la nube ya puesta en CLOUD_IP.

Uso:
  python mitm_captura_total.py

Cada comando aparece en consola con >>>, decodificado y legible, y se guarda:
  - cap_comandos.log     : solo los comandos de control (limpio, legible)
  - cap_mitm_full.log    : todo el trafico (por si acaso)

Metodo recomendado: pulsa UN boton en la app, espera a ver el >>> en consola,
apunta que boton era, y pasa al siguiente. Asi cada comando queda etiquetado.
"""

import socket, ssl, threading, hashlib, base64, struct, os, json
from datetime import datetime

ROBOT_LISTEN_PORT = 9090
CLOUD_IP = "43.158.121.228"
CLOUD_HOST = "tcp-cecotec.3irobotix.net"
CLOUD_PORT = 9090
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Servicios de ruido que NO son comandos de control interesantes
NOISE_SERVICES = (
    "heart-beat", "report_data", "info_report",
    "get_notice_config", "get_pets", "stuff/config",
)
# Controles de ruido (estado periodico que no nos interesa cazar)
NOISE_CONTROLS = ("status",)


def ts():
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log_full(direction, data):
    try:
        pretty = data.decode("utf-8")
    except Exception:
        pretty = data.hex(" ")
    with open("cap_mitm_full.log", "a", encoding="utf-8") as f:
        f.write(f"\n[{ts()}] {direction} ({len(data)}b)\n{pretty}\n")


def classify_and_log(direction, data):
    """Detecta comandos de control, los decodifica y los resalta."""
    try:
        text = data.decode("utf-8")
    except Exception:
        return
    if len(data) > 3000:
        # Mensajes enormes = mapa u OTA, no comando. Anotar breve.
        note = f"[{ts()}] {direction}: <mensaje grande {len(data)}b, probable mapa/datos>"
        print(f"\n>>> {note}\nmitm> ", end="", flush=True)
        return

    # Intentar parsear como JSON para extraer el control
    control = None
    service = None
    inner = None
    try:
        msg = json.loads(text)
        service = msg.get("service") or msg.get("tag", "")
        content = msg.get("content")
        if content:
            try:
                inner = json.loads(content)
                control = inner.get("control")
            except Exception:
                inner = content
    except Exception:
        # No es JSON limpio; buscar "control" a pelo
        if '"control"' in text or 'set_mode' in text:
            control = "?"

    # Filtrar ruido
    if service and any(n in service for n in NOISE_SERVICES) and not control:
        return
    if control in NOISE_CONTROLS:
        return
    # Si no hay ni control ni nada interesante, ignorar
    if not control and (not service or any(n in service for n in NOISE_SERVICES)):
        return

    # Resaltar y guardar
    if control and control != "?":
        summary = f"CONTROL={control}"
        if isinstance(inner, dict):
            extras = {k: v for k, v in inner.items() if k != "control"}
            if extras:
                summary += f"  params={json.dumps(extras, ensure_ascii=False)}"
    else:
        summary = f"service={service}"

    line = f"[{ts()}] {direction}: {summary}"
    print(f"\n>>> {line}\n    raw: {text[:300]}\nmitm> ", end="", flush=True)
    with open("cap_comandos.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.write(f"    raw: {text}\n")


# ---------- WebSocket helpers ----------
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
    try:
        cctx.set_ciphers("ALL:@SECLEVEL=0")
    except Exception:
        pass
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
    accept = base64.b64encode(hashlib.sha1((key + WS_MAGIC).encode()).digest()).decode()
    tls.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                 "Connection: Upgrade\r\n"
                 f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
    print("  [ROBOT] WS OK")


def handle(robot_tls):
    ws_handshake_robot(robot_tls)
    print("  conectando a la nube real...")
    cloud = connect_cloud()
    print("\n  === PUENTE LISTO ===")
    print("  Pulsa botones en la app oficial, UNO A UNO, y observa los >>>")
    print("  Sugerencia: prueba localizar, habitaciones, potencia, agua,")
    print("  programada, spot, bordes, etc. Apunta que boton es cada >>>\n")

    def r2c():
        try:
            while True:
                op, pl = ws_read_frame(robot_tls)
                if op is None or op == 0x8:
                    break
                if pl:
                    log_full("ROBOT->CLOUD", pl)
                    classify_and_log("ROBOT->CLOUD", pl)
                ws_write_frame(cloud, pl, opcode=op or 0x1, mask=True)
        except Exception as e:
            print("  r2c fin:", e)
        finally:
            for s in (robot_tls, cloud):
                try: s.close()
                except: pass

    def c2r():
        try:
            while True:
                op, pl = ws_read_frame(cloud)
                if op is None or op == 0x8:
                    break
                if pl:
                    log_full("CLOUD->ROBOT", pl)
                    classify_and_log("CLOUD->ROBOT", pl)
                ws_write_frame(robot_tls, pl, opcode=op or 0x1, mask=False)
        except Exception as e:
            print("  c2r fin:", e)
        finally:
            for s in (robot_tls, cloud):
                try: s.close()
                except: pass

    threading.Thread(target=r2c, daemon=True).start()
    threading.Thread(target=c2r, daemon=True).start()


def main():
    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(certfile="cert.pem", keyfile="key.pem")
    sctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        sctx.set_ciphers("ALL:@SECLEVEL=0")
    except Exception:
        pass

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", ROBOT_LISTEN_PORT))
    s.listen(5)
    print(f"[MITM] escuchando robot en {ROBOT_LISTEN_PORT} -> nube {CLOUD_IP}")
    print("[MITM] Los comandos apareceran con >>>\n")
    while True:
        raw, addr = s.accept()
        print(f"[conexion] desde {addr[0]}")
        try:
            robot_tls = sctx.wrap_socket(raw, server_side=True)
            print("  [ROBOT] TLS OK")
            handle(robot_tls)
        except Exception as e:
            print("  error:", e)
            raw.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[MITM] detenido.")
