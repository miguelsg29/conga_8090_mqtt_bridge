#!/usr/bin/env python3
"""
Servidor suplantador de la nube Cecotec para el Conga 8090 Ultra.
FASE 3: control interactivo. Mantiene la conexion (login + heartbeat +
report_data) Y permite enviar comandos al robot escribiendolos por teclado.

Requisitos:
  - cert.pem y key.pem en la misma carpeta.
  - DNS: tcp-cecotec.3irobotix.net -> IP de esta maquina.
  - Puerto 9090 abierto en el firewall.

Uso:
  python servidor_conga_control.py

Con el robot conectado, escribe en la consola:
  fwd        -> mover adelante (set_direct dir 1)
  back       -> mover atras    (set_direct dir 2)
  left       -> girar izquierda(set_direct dir 3)
  right      -> girar derecha  (set_direct dir 4)
  stopmove   -> parar joystick (set_direct dir 0)
  clean      -> iniciar limpieza (set_mode type 0 value 1)  [probar]
  pause      -> pausar          (set_mode type 0 value 2)  [probar]
  home       -> volver a base   (set_mode type 5 value 1)  [probar]
  status     -> pedir estado    (get_status)
  map        -> pedir mapa       (get_map)
  raw <json> -> enviar control crudo, ej:  raw {"control":"set_direct","direction":1,"angle":0}
  help       -> ayuda
  quit       -> salir

NOTA: los valores de direction y de set_mode (type/value) son los DEDUCIDOS de la
captura. Si alguno no hace lo esperado, usa 'raw' para experimentar y anota el
que funcione. Empieza siempre por movimientos cortos y con el robot a la vista.
"""

import socket, ssl, threading, hashlib, base64, struct, json, time, sys

LISTEN_PORT = 9090
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

ROBOT_DID = 123456
ROBOT_USERID = 654321
ROBOT_SN = "500400000000"
ROBOT_MAC = "12:34:56:78:9A:BC"
FACTORY_ID = "1003"
PROJECT_TYPE = "CECOTECCRL350-1001"
AUTH_JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiJFWEFNUExFIiwibmFtZSI6IkVYQU1QTEUiLCJpYXQiOjE1MTZ9."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")

# Socket del robot conectado (uno solo). Protegido por lock para escribir.
_client = {"sock": None}
_client_lock = threading.Lock()
_trace = {"n": 900000000}
# Ultimo estado mostrado, para no repetir report_data identicos y no
# ensuciar la consola mientras se escriben comandos.
_last_state = {"s": None}
QUIET_STATE = True  # True = solo imprime estado cuando cambia


def now_ms():
    return str(int(time.time() * 1000))


def next_trace():
    _trace["n"] += 1
    return _trace["n"]


# ---------------- WebSocket ----------------
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


# ---------------- envio de comandos al robot ----------------
def send_command(control_obj):
    """Envia un comando push al robot con el formato tag/to_bind."""
    with _client_lock:
        sock = _client["sock"]
        if not sock:
            print("  [!] no hay robot conectado")
            return
        msg = {
            "tag": "sweeper-transmit/to_bind",
            "content": json.dumps(control_obj),
        }
        try:
            ws_send(sock, json.dumps(msg))
            print(f"  --> comando enviado: {control_obj}")
        except Exception as e:
            print(f"  [!] error enviando: {e}")


# Mapeo de direcciones CONFIRMADO por observacion en vivo:
#   adelante=1, izquierda=2, derecha=3, atras=4  (0 = soltar)
DIR_FWD = 1
DIR_LEFT = 2
DIR_RIGHT = 3
DIR_BACK = 4
DIR_STOP = 0

# Comandos de alto nivel
def cmd_move(direction, angle=0.0):
    send_command({"angle": angle, "control": "set_direct", "direction": direction})

def cmd_set_mode(type_, value):
    send_command({"control": "set_mode", "mapid": 0, "type": type_, "value": value})

# set_mode CONFIRMADOS por captura dirigida (app oficial):
#   iniciar limpieza : type=0 value=1
#   pausar           : type=2 value=2
#   reanudar         : type=2 value=1
#   ir a base (home) : type=3 value=1
#   cancelar ir base : type=3 value=0
def cmd_clean():   cmd_set_mode(0, 1)
def cmd_pause():   cmd_set_mode(2, 2)
def cmd_resume():  cmd_set_mode(2, 1)
def cmd_home():    cmd_set_mode(3, 1)
def cmd_home_cancel(): cmd_set_mode(3, 0)

def cmd_get_status():
    send_command({"control": "get_status", "userid": ROBOT_USERID})

def cmd_get_map():
    send_command({"control": "get_map", "mapid": 0, "type": 0, "mask": 1})


# ---------------- consola interactiva ----------------
HELP = """
Comandos disponibles:
  fwd / back / left / right   movimiento manual (set_direct)
  stopmove                    soltar joystick (direction 0)
  clean                       iniciar limpieza (set_mode 0/1)
  pause                       pausar          (set_mode 2/2)
  resume                      reanudar        (set_mode 2/1)
  home                        volver a base   (set_mode 3/1)
  cancelhome                  cancelar ir base (set_mode 3/0)
  status                      get_status
  map                         get_map
  raw {json}                  envia {"...control..."} crudo
  help                        esta ayuda
  quit                        salir
"""

def console_loop():
    print(HELP)
    while True:
        try:
            line = input("conga> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nsaliendo...")
            break
        if not line:
            continue
        cmd = line.split(maxsplit=1)
        c = cmd[0].lower()

        if c == "quit":
            break
        elif c == "help":
            print(HELP)
        elif c == "fwd":
            cmd_move(DIR_FWD)
        elif c == "back":
            cmd_move(DIR_BACK)
        elif c == "left":
            cmd_move(DIR_LEFT)
        elif c == "right":
            cmd_move(DIR_RIGHT)
        elif c == "stopmove":
            cmd_move(DIR_STOP)
        elif c == "clean":
            cmd_clean()
        elif c == "pause":
            cmd_pause()
        elif c == "resume":
            cmd_resume()
        elif c == "home":
            cmd_home()
        elif c == "cancelhome":
            cmd_home_cancel()
        elif c == "status":
            cmd_get_status()
        elif c == "map":
            cmd_get_map()
        elif c == "raw":
            if len(cmd) < 2:
                print("  uso: raw {\"control\":\"...\"}")
                continue
            try:
                obj = json.loads(cmd[1])
                send_command(obj)
            except Exception as e:
                print(f"  JSON invalido: {e}")
        else:
            print(f"  comando desconocido: {c} (help para ayuda)")


# ---------------- manejo de mensajes del robot ----------------
def handle_message(sock, payload):
    if payload.strip() == b"libuwsc":
        ws_send(sock, b"libuwsc")
        return
    try:
        msg = json.loads(payload.decode("utf-8"))
    except Exception:
        print(f"    [??] no-JSON: {payload[:80]!r}")
        return

    service = msg.get("service", "")
    trace_str = str(msg.get("traceId", ""))

    if service.endswith("auth/login"):
        print("    <-- LOGIN")
        resp = {"code": 0, "traceId": trace_str,
                "service": "sweeper-robot-center/auth/login",
                "result": {"data": {
                    "AUTH": AUTH_JWT, "FACTORY_ID": FACTORY_ID,
                    "USERNAME": ROBOT_SN, "CONNECTION_TYPE": "sweeper",
                    "PROJECT_TYPE": PROJECT_TYPE, "ROBOT_TYPE": "sweeper",
                    "SN": ROBOT_SN, "MAC": ROBOT_MAC,
                    "BIND_LIST": f"[\"{ROBOT_USERID}\"]"},
                    "clientType": "ROBOT", "id": str(ROBOT_DID), "resetCode": 0}}
        ws_send(sock, json.dumps(resp))
        print("    --> LOGIN OK")
        return

    if service == "heart-beat":
        ws_send(sock, json.dumps({"code": 0, "traceId": trace_str,
                                  "service": "heart-beat", "result": now_ms()}))
        return

    if service.endswith("device/report_data"):
        try:
            data = json.loads(msg.get("content", "{}")).get("data", {})
            state = (f"bat={data.get('battary')} "
                     f"charge={data.get('chargeStatus')} "
                     f"mode={data.get('workMode')} fault={data.get('faultCode')}")
            if (not QUIET_STATE) or state != _last_state["s"]:
                print(f"\n    <-- ESTADO {state}\nconga> ", end="", flush=True)
                _last_state["s"] = state
        except Exception:
            pass
        ws_send(sock, json.dumps({"code": 0, "traceId": trace_str,
                "service": "sweeper-robot-center/device/report_data",
                "result": True}))
        return

    if service.endswith("transmit/to_bind"):
        # Acuse de un comando que enviamos: mostrar resultado
        try:
            data = json.loads(msg.get("content", "{}")).get("data", {})
            print(f"    <== ACK {data.get('control')} result={data.get('result')}")
        except Exception:
            pass
        ws_send(sock, json.dumps({"code": 0, "traceId": trace_str,
                "service": "sweeper-transmit/transmit/to_bind", "result": True}))
        return

    # generico
    ws_send(sock, json.dumps({"code": 0, "traceId": trace_str,
            "service": service.split("?")[0], "result": True}))


def ws_handshake(tls_sock):
    req = recv_http_headers(tls_sock)
    key = None
    for line in req.split(b"\r\n"):
        if line.lower().startswith(b"sec-websocket-key:"):
            key = line.split(b":", 1)[1].strip().decode()
    if not key:
        return False
    accept = base64.b64encode(
        hashlib.sha1((key + WS_MAGIC).encode()).digest()).decode()
    tls_sock.sendall((
        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
    return True


def handle_client(tls_sock, addr):
    try:
        if not ws_handshake(tls_sock):
            print("  [WS] handshake fallido")
            tls_sock.close(); return
        print("  [WS] conectado ✓  (ya puedes enviar comandos)")
        with _client_lock:
            _client["sock"] = tls_sock
        while True:
            opcode, payload = ws_read_frame(tls_sock)
            if opcode is None or opcode == 0x8:
                print("  [conn] robot desconectado")
                break
            if opcode == 0x9:
                ws_send(tls_sock, payload or b"", opcode=0xA)
                continue
            if payload:
                handle_message(tls_sock, payload)
    except Exception as e:
        print(f"  [conn] error: {e}")
    finally:
        with _client_lock:
            if _client["sock"] is tls_sock:
                _client["sock"] = None
        try: tls_sock.close()
        except Exception: pass


def server_loop():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        ctx.load_cert_chain(certfile="cert.pem", keyfile="key.pem")
    except Exception as e:
        print(f"ERROR cargando cert.pem/key.pem: {e}")
        sys.exit(1)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try: ctx.set_ciphers("ALL:@SECLEVEL=0")
    except Exception: pass

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", LISTEN_PORT)); s.listen(5)
    print(f"[SERVIDOR] escuchando en 0.0.0.0:{LISTEN_PORT}")
    print("[SERVIDOR] esperando al robot...")
    while True:
        raw, addr = s.accept()
        print(f"\n[conexion] desde {addr[0]}")
        try:
            tls_sock = ctx.wrap_socket(raw, server_side=True)
            print("  [TLS] OK ✓")
        except Exception as e:
            print(f"  [TLS] error: {e}")
            raw.close(); continue
        threading.Thread(target=handle_client, args=(tls_sock, addr),
                         daemon=True).start()


def main():
    # Servidor en hilo de fondo, consola en el hilo principal
    t = threading.Thread(target=server_loop, daemon=True)
    t.start()
    time.sleep(0.5)
    console_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[SERVIDOR] detenido.")
