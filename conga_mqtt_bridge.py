#!/usr/bin/env python3
"""
Puente Conga 8090 <-> Home Assistant via MQTT.

Junta las dos mitades que ya funcionan:
  - Lado ROBOT: servidor TLS+WebSocket+JSON que suplanta la nube Cecotec.
    El robot se conecta, hace login, manda heart-beat y report_data.
  - Lado HA: cliente MQTT que publica autodiscovery (entidad vacuum) + estado,
    y recibe comandos de HA para reenviarlos al robot.

Requiere:
  pip install paho-mqtt

Config: crea un fichero .env junto a este script (ver .env.example) con las
credenciales MQTT. Coloca cert.pem y key.pem junto al script.
DNS tcp-cecotec -> esta maquina. Puerto 9090 abierto en el firewall.

Uso:
  python conga_mqtt_bridge.py

En Home Assistant aparecera automaticamente un dispositivo "Conga 8090" con
una entidad vacuum (start/pause/stop/return_home), bateria, sensores, botones de
limpieza por habitacion, y selectores de potencia/agua/mopa/x2.
"""

import socket, ssl, threading, hashlib, base64, struct, json, time, sys, os

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Falta paho-mqtt. Instala con:  pip install paho-mqtt")
    sys.exit(1)


# ==================== carga de .env ====================
def load_env(path=".env"):
    """Carga variables de un fichero .env simple (KEY=valor)."""
    env = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    # Prioridad: variable de entorno del sistema > .env > default
    def get(key, default=None):
        return os.environ.get(key, env.get(key, default))
    return get


_env = load_env()

# ==================== CONFIGURACION ====================
# --- MQTT (desde .env) ---
MQTT_HOST = _env("MQTT_HOST", "192.168.1.X")
MQTT_PORT = int(_env("MQTT_PORT", "1883"))
MQTT_USER = _env("MQTT_USER", "")
MQTT_PASS = _env("MQTT_PASS", "")

# --- Robot (desde .env, con valores de EJEMPLO como default) ---
# IMPORTANTE: los valores reales de TU robot van en el .env, no aqui.
# Se obtienen del login capturado con el MITM (ver documentacion).
LISTEN_PORT = int(_env("LISTEN_PORT", "9090"))
WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
ROBOT_DID = int(_env("ROBOT_DID", "123456"))
ROBOT_USERID = int(_env("ROBOT_USERID", "654321"))
ROBOT_SN = _env("ROBOT_SN", "500400000000")
ROBOT_MAC = _env("ROBOT_MAC", "12:34:56:78:9A:BC")
FACTORY_ID = _env("FACTORY_ID", "1003")
PROJECT_TYPE = _env("PROJECT_TYPE", "CECOTECCRL350-1001")


def make_synthetic_jwt(did, factory_id):
    """Genera un JWT con estructura valida, firma FALSA y SIN caducidad.

    El robot no valida la firma del JWT (solo necesita code:0 y respuesta bien
    formada), asi que un token sintetico sin 'timestamp' evita la caducidad del
    token capturado. Verifica con test_jwt_sintetico.py que tu unidad lo acepta.
    """
    def b64url(data):
        return base64.urlsafe_b64encode(data).decode().rstrip("=")
    header = {"typ": "JWT", "alg": "HS256"}
    payload = {
        "value": json.dumps({
            "data": {"FACTORY_ID": str(factory_id)},
            "clientType": "ROBOT",
            "id": str(did),
            "resetCode": 0,
        }),
        "version": None, "scope": None, "timestamp": None,  # sin caducidad
    }
    h = b64url(json.dumps(header, separators=(",", ":")).encode())
    p = b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = "SYNTHETIC0SIGNATURE0NO0VALIDATION0NEEDED000000000000000000000"
    return f"{h}.{p}.{sig}"


# --- Selección del JWT ---
# Opciones (via .env):
#   AUTH_JWT=<token>   -> usa ese token tal cual (el capturado de tu robot)
#   AUTH_JWT vacio     -> genera uno SINTÉTICO automaticamente (sin caducidad)
#   USE_SYNTHETIC_JWT=on -> fuerza el sintético aunque haya AUTH_JWT en el .env
_auth_jwt_env = _env("AUTH_JWT", "").strip()
_force_synthetic = _env("USE_SYNTHETIC_JWT", "off").lower() in ("on", "true", "1", "yes")

if _force_synthetic or not _auth_jwt_env:
    AUTH_JWT = make_synthetic_jwt(ROBOT_DID, FACTORY_ID)
    _jwt_mode = "SINTÉTICO (generado, sin caducidad)"
else:
    AUTH_JWT = _auth_jwt_env
    _jwt_mode = "capturado (del .env)"

# --- Topics MQTT ---
DISC_PREFIX = "homeassistant"
NODE = "conga8090"
T_STATE = f"conga/{NODE}/state"
T_CMD = f"conga/{NODE}/command"
T_AVAIL = f"conga/{NODE}/availability"
UID = f"conga_{ROBOT_DID}"

# Mapeos confirmados
DIR_FWD, DIR_LEFT, DIR_RIGHT, DIR_BACK, DIR_STOP = 1, 2, 3, 4, 0
# set_mode: (type, value)
SM_CLEAN = (0, 1)
SM_PAUSE = (2, 2)
SM_RESUME = (2, 1)
SM_HOME = (3, 1)
SM_HOME_CANCEL = (3, 0)

# Habitaciones del mapa (id -> nombre). Confirmados del mapa decodificado.
ROOMS = {
    10: "Dormitorio", 11: "Baño privado", 12: "Baño", 13: "Cocina",
    14: "Dormitorio Principal", 15: "Salón", 16: "Pasillo",
}

# Ajustes: valores confirmados cruzando plan + capturas de la app
FAN_LEVELS = {"Off": 0, "Eco": 1, "Normal": 2, "Turbo": 3}
WATER_LEVELS = {"Off": 10, "Bajo": 11, "Medio": 12, "Alto": 13}
MOP_LEVELS = {"Off": 0, "Estándar": 1, "Fuerte": 2, "Potente": 3}


# Ajustes: valores confirmados cruzando plan + capturas de la app
FAN_LEVELS = {"Off": 0, "Eco": 1, "Normal": 2, "Turbo": 3}
WATER_LEVELS = {"Off": 10, "Bajo": 11, "Medio": 12, "Alto": 13}
MOP_LEVELS = {"Off": 0, "Estándar": 1, "Fuerte": 2, "Potente": 3}

# Configuracion de limpieza POR DEFECTO (mutable desde HA via selectores).
# Se aplica antes de setRoomClean. Valores iniciales desde .env o sensatos.
_prefs = {
    "fan": _env("DEFAULT_FAN", "Normal"),       # Off/Eco/Normal/Turbo
    "water": _env("DEFAULT_WATER", "Medio"),    # Off/Bajo/Medio/Alto
    "mop": _env("DEFAULT_MOP", "Estándar"),     # Off/Estándar/Fuerte/Potente
    "twice": _env("DEFAULT_TWICE", "off") == "on",  # doble pasada
}


def apply_prefs():
    """Envia los set_preference con la config actual (potencia, agua, mopa)."""
    robot_send_control({"control": "set_preference", "ctrltype": 1,
                        "value": FAN_LEVELS.get(_prefs["fan"], 2)})
    robot_send_control({"control": "set_preference", "ctrltype": 2,
                        "value": WATER_LEVELS.get(_prefs["water"], 12)})
    robot_send_control({"control": "set_preference", "ctrltype": 15,
                        "value": MOP_LEVELS.get(_prefs["mop"], 1)})


def clean_rooms(room_ids):
    """Limpieza inmediata de habitaciones, aplicando la config por defecto."""
    apply_prefs()
    time.sleep(0.3)  # dar un instante a que el robot procese los ajustes
    twice = 1 if _prefs["twice"] else 0
    robot_send_control({"control": "setRoomClean", "ctrlValue": 1,
                        "roomsID": list(room_ids), "clean_type": twice})

# Anti-rebote: ignora comandos identicos repetidos en menos de N segundos
_last_cmd = {"cmd": None, "t": 0.0}
CMD_DEBOUNCE = 1.5

# ==================== estado compartido ====================
_robot = {"sock": None}
_robot_lock = threading.Lock()
_state = {"battery": None, "mode": None, "charge": None, "fault": None,
          "area": None, "time": None, "water": None, "room": None}
_mqttc = None


def now_ms():
    return str(int(time.time() * 1000))


# ==================== WebSocket helpers ====================
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


# ==================== envio de comandos al robot ====================
def robot_send_control(control_obj):
    with _robot_lock:
        sock = _robot["sock"]
        if not sock:
            print("  [!] robot no conectado, comando descartado")
            return
        msg = {"tag": "sweeper-transmit/to_bind",
               "content": json.dumps(control_obj)}
        try:
            ws_send(sock, json.dumps(msg))
            print(f"  --> robot: {control_obj}")
        except Exception as e:
            print(f"  [!] error enviando al robot: {e}")


def do_set_mode(tv):
    robot_send_control({"control": "set_mode", "mapid": 0,
                        "type": tv[0], "value": tv[1]})


# ==================== MQTT ====================
def mqtt_publish_discovery(client):
    """Publica el autodiscovery de la entidad vacuum (esquema state)."""
    device = {
        "identifiers": [UID],
        "name": "Conga 8090",
        "manufacturer": "Cecotec",
        "model": "Conga 8090 Ultra",
        "sw_version": "local-bridge",
    }
    cfg = {
        "name": "Conga 8090",
        "unique_id": UID,
        "schema": "state",
        "supported_features": [
            "start", "pause", "stop", "return_home", "status", "locate",
        ],
        "command_topic": T_CMD,
        "state_topic": T_STATE,
        "availability_topic": T_AVAIL,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device,
    }
    topic = f"{DISC_PREFIX}/vacuum/{NODE}/config"
    client.publish(topic, json.dumps(cfg), retain=True)
    print(f"[MQTT] discovery vacuum publicado (retain) en {topic}")

    # --- Sensores adicionales (batería, área, tiempo) ---
    device_ref = {"identifiers": [UID]}
    sensors = [
        {"name": "Conga Batería", "uid": f"{UID}_bat",
         "topic": T_STATE, "tmpl": "{{ value_json.battery_level }}",
         "unit": "%", "dclass": "battery"},
        {"name": "Conga Área limpiada", "uid": f"{UID}_area",
         "topic": f"conga/{NODE}/area", "tmpl": "{{ value }}",
         "unit": "m²", "dclass": None},
        {"name": "Conga Tiempo limpieza", "uid": f"{UID}_time",
         "topic": f"conga/{NODE}/time", "tmpl": "{{ value }}",
         "unit": "min", "dclass": "duration"},
    ]
    for s in sensors:
        scfg = {
            "name": s["name"], "unique_id": s["uid"],
            "state_topic": s["topic"], "value_template": s["tmpl"],
            "availability_topic": T_AVAIL,
            "payload_available": "online", "payload_not_available": "offline",
            "device": device_ref,
        }
        if s["unit"]:
            scfg["unit_of_measurement"] = s["unit"]
        if s["dclass"]:
            scfg["device_class"] = s["dclass"]
        client.publish(f"{DISC_PREFIX}/sensor/{s['uid']}/config",
                       json.dumps(scfg), retain=True)

    # --- Botones de limpieza por habitación ---
    for rid, rname in ROOMS.items():
        bcfg = {
            "name": f"Limpiar {rname}",
            "unique_id": f"{UID}_room_{rid}",
            "command_topic": f"conga/{NODE}/room_command",
            "payload_press": str(rid),
            "availability_topic": T_AVAIL,
            "payload_available": "online", "payload_not_available": "offline",
            "device": device_ref,
        }
        client.publish(f"{DISC_PREFIX}/button/{UID}_room_{rid}/config",
                       json.dumps(bcfg), retain=True)

    print(f"[MQTT] sensores y botones de habitación publicados")

    # --- Selectores de configuración (potencia, agua, mopa) ---
    selects = [
        ("fan", "Conga Potencia succión", list(FAN_LEVELS.keys())),
        ("water", "Conga Nivel agua", list(WATER_LEVELS.keys())),
        ("mop", "Conga Vibración mopa", list(MOP_LEVELS.keys())),
    ]
    for key, name, options in selects:
        selcfg = {
            "name": name, "unique_id": f"{UID}_sel_{key}",
            "command_topic": f"conga/{NODE}/pref/{key}/set",
            "state_topic": f"conga/{NODE}/pref/{key}",
            "options": options,
            "availability_topic": T_AVAIL,
            "payload_available": "online", "payload_not_available": "offline",
            "device": device_ref,
        }
        client.publish(f"{DISC_PREFIX}/select/{UID}_sel_{key}/config",
                       json.dumps(selcfg), retain=True)
        # publicar estado inicial
        client.publish(f"conga/{NODE}/pref/{key}", _prefs[key], retain=True)

    # --- Switch de doble pasada (x2) ---
    swcfg = {
        "name": "Conga Doble pasada (x2)", "unique_id": f"{UID}_twice",
        "command_topic": f"conga/{NODE}/pref/twice/set",
        "state_topic": f"conga/{NODE}/pref/twice",
        "payload_on": "on", "payload_off": "off",
        "availability_topic": T_AVAIL,
        "payload_available": "online", "payload_not_available": "offline",
        "device": device_ref,
    }
    client.publish(f"{DISC_PREFIX}/switch/{UID}_twice/config",
                   json.dumps(swcfg), retain=True)
    client.publish(f"conga/{NODE}/pref/twice",
                   "on" if _prefs["twice"] else "off", retain=True)

    print(f"[MQTT] selectores de configuración publicados")


def mqtt_publish_state(client):
    """Publica el estado actual en formato esquema 'state'.

    workMode observados (del robot real):
      0     = idle / parado
      5     = ? (transicion)
      36,37 = limpiando (variantes de modo de limpieza)
      2     = manual (joystick)
      20    = volviendo a base
    chargeStatus: 1 = cargando/en base
    faultCode: 21xx = avisos de estacion (NO error), 5xx = avisos consumibles.
    """
    mode = _state["mode"]
    charge = _state["charge"]
    fault = _state["fault"]

    # workMode REALES confirmados en este robot (via log en vivo):
    #   0 + charge=1 : en la base (docked)
    #   0 + charge=0 : parado (idle)
    #   5            : volviendo a la base (returning)
    #   36           : limpiando (cleaning)
    #   37           : pausado durante limpieza (paused)
    #   2            : control manual (cleaning)
    if charge == 1:
        ha_state = "docked"
    elif mode == 5:
        ha_state = "returning"
    elif mode == 37:
        ha_state = "paused"
    elif mode in (36, 2):
        ha_state = "cleaning"
    else:                          # 0 sin carga, y cualquier otro -> idle
        ha_state = "idle"

    # Error solo para codigos que NO son avisos de estacion (21xx) ni
    # consumibles (5xx).
    if fault:
        f = int(fault) if str(fault).isdigit() else 0
        if f != 0 and not (2100 <= f <= 2199) and not (500 <= f <= 599):
            ha_state = "error"

    bat = _state["battery"]
    bat_pct = int(bat / 2) if isinstance(bat, int) else None  # 0-200 -> 0-100

    payload = {"state": ha_state}
    if bat_pct is not None:
        payload["battery_level"] = bat_pct
    client.publish(T_STATE, json.dumps(payload), retain=True)


def on_mqtt_connect(client, userdata, flags, rc, *a):
    print(f"[MQTT] conectado (rc={rc})")
    if rc != 0:
        print("  [!] rc!=0 indica fallo de credenciales o conexion")
        return
    client.publish(T_AVAIL, "online", retain=True)
    # Limpiar cualquier comando retained viejo que pudiera reenviarse
    client.publish(T_CMD, "", retain=True)
    client.subscribe(T_CMD)
    client.subscribe(f"conga/{NODE}/room_command")
    client.subscribe(f"conga/{NODE}/pref/+/set")
    # Escuchar el birth message de HA para republicar discovery si HA reinicia
    client.subscribe("homeassistant/status")
    mqtt_publish_discovery(client)
    mqtt_publish_state(client)


def on_mqtt_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload.decode("utf-8", "replace").strip()
    # Birth message de HA: republicar discovery + estado
    if topic == "homeassistant/status":
        print(f"[MQTT] HA status = {payload}")
        if payload == "online":
            time.sleep(1)
            mqtt_publish_discovery(client)
            mqtt_publish_state(client)
        return
    # Comando de limpieza por habitación
    if topic == f"conga/{NODE}/room_command":
        try:
            rid = int(payload)
            rname = ROOMS.get(rid, f"hab {rid}")
            print(f"[MQTT] limpiar habitación: {rname} (id {rid}) "
                  f"[fan={_prefs['fan']} water={_prefs['water']} "
                  f"mop={_prefs['mop']} x2={_prefs['twice']}]")
            clean_rooms([rid])
        except Exception as e:
            print(f"  [!] room_command inválido: {payload} ({e})")
        return
    # Cambio de preferencia (potencia/agua/mopa/x2) desde HA
    if topic.startswith(f"conga/{NODE}/pref/") and topic.endswith("/set"):
        key = topic.split("/")[-2]  # fan, water, mop o twice
        if key == "twice":
            _prefs["twice"] = (payload == "on")
            client.publish(f"conga/{NODE}/pref/twice", payload, retain=True)
            # Aplicar tambien el toggle en el robot si procede (ctrltype 5?)
        elif key in ("fan", "water", "mop"):
            _prefs[key] = payload
            client.publish(f"conga/{NODE}/pref/{key}", payload, retain=True)
            # Aplicar el ajuste al robot al instante
            if key == "fan":
                robot_send_control({"control": "set_preference", "ctrltype": 1,
                                    "value": FAN_LEVELS.get(payload, 2)})
            elif key == "water":
                robot_send_control({"control": "set_preference", "ctrltype": 2,
                                    "value": WATER_LEVELS.get(payload, 12)})
            elif key == "mop":
                robot_send_control({"control": "set_preference", "ctrltype": 15,
                                    "value": MOP_LEVELS.get(payload, 1)})
        print(f"[MQTT] preferencia {key} = {payload}")
        return
    # Comando de la entidad vacuum
    cmd = payload
    if not cmd:
        return  # mensaje vacio (limpieza de retained), ignorar
    print(f"[MQTT] comando de HA: {payload}  (mode actual={_state['mode']})")

    # Anti-rebote solo para comandos IDENTICOS muy seguidos (evita doble envio)
    tnow = time.time()
    if cmd == _last_cmd["cmd"] and (tnow - _last_cmd["t"]) < 0.8:
        print(f"  [debounce] ignorado '{cmd}' repetido <0.8s")
        return
    _last_cmd["cmd"] = cmd
    _last_cmd["t"] = tnow

    mode = _state["mode"]

    if cmd == "start":
        # Si esta pausado (37) o volviendo (5), reanudar; si no, iniciar
        if mode in (2, 5, 37):
            do_set_mode(SM_RESUME)
        else:
            do_set_mode(SM_CLEAN)
    elif cmd == "pause":
        if mode == 5:             # volviendo a base -> cancelar retorno
            do_set_mode(SM_HOME_CANCEL)
        else:                     # limpiando -> pausa normal
            do_set_mode(SM_PAUSE)
    elif cmd == "stop":
        if mode == 5:             # volviendo a base -> cancelar retorno
            do_set_mode(SM_HOME_CANCEL)
        else:                     # limpiando/pausado -> pausar
            do_set_mode(SM_PAUSE)
    elif cmd == "return_to_base":
        do_set_mode(SM_HOME)
    elif cmd == "clean_spot":
        do_set_mode(SM_CLEAN)
    elif cmd == "locate":
        # Localizar: el robot pita para encontrarlo (device_ctrl ctrltype 3)
        robot_send_control({"result": 0, "control": "device_ctrl",
                            "ctrltype": 3, "operation": 1})
    else:
        print(f"  [MQTT] comando no mapeado: {cmd}")


def start_mqtt():
    global _mqttc
    c = mqtt.Client(client_id="conga_bridge")
    if MQTT_USER:
        c.username_pw_set(MQTT_USER, MQTT_PASS)
    c.will_set(T_AVAIL, "offline", retain=True)
    c.on_connect = on_mqtt_connect
    c.on_message = on_mqtt_message
    c.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    _mqttc = c
    c.loop_start()
    return c


# ==================== manejo del robot ====================
def handle_robot_message(sock, payload):
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
        print("  [robot] LOGIN")
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
        return

    if service == "heart-beat":
        ws_send(sock, json.dumps({"code": 0, "traceId": trace,
                                  "service": "heart-beat", "result": now_ms()}))
        return

    if service.endswith("device/report_data"):
        try:
            data = json.loads(msg.get("content", "{}")).get("data", {})
            new_mode = data.get("workMode")
            new_charge = data.get("chargeStatus")
            new_fault = data.get("faultCode")
            # Log solo cuando cambia algo relevante
            if (new_mode != _state["mode"] or new_charge != _state["charge"]):
                print(f"  [ESTADO robot] workMode={new_mode} "
                      f"charge={new_charge} fault={new_fault} "
                      f"bat={data.get('battary')}")
            _state["battery"] = data.get("battary")
            _state["charge"] = new_charge
            _state["mode"] = new_mode
            _state["fault"] = new_fault
            _state["area"] = data.get("cleanSize")
            _state["time"] = data.get("cleanTime")
            _state["water"] = data.get("waterlevel")
            _state["room"] = data.get("cleaning_roomId")
            if _mqttc:
                mqtt_publish_state(_mqttc)
                # Publicar sensores extra
                if _state["area"] is not None:
                    _mqttc.publish(f"conga/{NODE}/area", _state["area"])
                if _state["time"] is not None:
                    _mqttc.publish(f"conga/{NODE}/time", _state["time"])
        except Exception:
            pass
        ws_send(sock, json.dumps({"code": 0, "traceId": trace,
                "service": "sweeper-robot-center/device/report_data",
                "result": True}))
        return

    if service.endswith("transmit/to_bind"):
        ws_send(sock, json.dumps({"code": 0, "traceId": trace,
                "service": "sweeper-transmit/transmit/to_bind", "result": True}))
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


def handle_robot(tls, addr):
    try:
        if not ws_handshake(tls):
            tls.close(); return
        print("  [robot] conectado ✓")
        with _robot_lock:
            _robot["sock"] = tls
        while True:
            opcode, payload = ws_read_frame(tls)
            if opcode is None or opcode == 0x8:
                print("  [robot] desconectado")
                break
            if opcode == 0x9:
                ws_send(tls, payload or b"", opcode=0xA)
                continue
            if payload:
                handle_robot_message(tls, payload)
    except Exception as e:
        print(f"  [robot] error: {e}")
    finally:
        with _robot_lock:
            if _robot["sock"] is tls:
                _robot["sock"] = None
        try: tls.close()
        except Exception: pass


def robot_server():
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
    print(f"[ROBOT] servidor escuchando en 0.0.0.0:{LISTEN_PORT}")
    while True:
        raw, addr = s.accept()
        print(f"\n[robot] conexion desde {addr[0]}")
        try:
            tls = ctx.wrap_socket(raw, server_side=True)
        except Exception as e:
            print(f"  [robot] TLS error: {e}")
            raw.close(); continue
        threading.Thread(target=handle_robot, args=(tls, addr),
                         daemon=True).start()


def main():
    print("=== Puente Conga 8090 <-> Home Assistant (MQTT) ===")
    print(f"[JWT] modo: {_jwt_mode}")
    start_mqtt()
    robot_server()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        if _mqttc:
            _mqttc.publish(T_AVAIL, "offline", retain=True)
        print("\n[BRIDGE] detenido.")
