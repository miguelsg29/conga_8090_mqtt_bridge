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

# Salida en UTF-8: en consolas/redirecciones Windows (cp1252) un caracter como el
# tick "✓" lanzaria UnicodeEncodeError; dentro del hilo del robot eso tiraba la
# conexion y el robot reconectaba en bucle. Con esto los prints nunca fallan.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


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


# ============ HORARIOS / PLANES PROGRAMADOS (setOrder6090) ============
# Confirmado por captura MITM (16-07-2026): la app crea/lee/borra horarios con
# limpieza por habitacion y ajustes individuales mediante tres comandos:
#   setOrder6090    -> crear/actualizar un plan (o activar/desactivar via enable)
#   getOrder6090    -> leer los planes guardados en el robot
#   deleteOrder6090 -> borrar un plan por orderid
# Codificacion confirmada:
#   day_time = minutos desde medianoche (20:12 -> 1212, 19:30 -> 1170)
#   weekday  = bitmask con domingo = bit 0
#             (dom=1 lun=2 mar=4 mie=8 jue=16 vie=32 sab=64; varios dias = suma)
WEEKDAY_BITS = {
    "dom": 1, "lun": 2, "mar": 4, "mie": 8, "jue": 16, "vie": 32, "sab": 64,
}
# room_type / material_type observados en el plan capturado de este mapa. Sirven
# para replicar el plan tal cual lo manda la app; si falta una habitacion se usan
# valores neutros (el robot ejecuta por room_id).
ROOM_TYPES = {11: 2103, 14: 2101, 16: 2104, 13: 2105, 15: 2106}
ROOM_MATERIAL = {11: 2, 14: 3, 16: 3, 13: 2, 15: 3}


def load_plans(path="plans.json"):
    """Carga los horarios definidos por el usuario (plans.json). Opcional."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        plans = data.get("plans", []) if isinstance(data, dict) else data
        print(f"[PLANS] {len(plans)} horario(s) cargados de {path}")
        return plans
    except Exception as e:
        print(f"[PLANS] error leyendo {path}: {e}")
        return []


_plans_sched = load_plans()


def _norm_day(d):
    """Normaliza un dia a su clave de 3 letras (jueves/jue/Thu... -> 'jue')."""
    d = str(d).strip().lower()
    alias = {"sun": "dom", "mon": "lun", "tue": "mar", "wed": "mie",
             "thu": "jue", "fri": "vie", "sat": "sab"}
    d = d.replace("á", "a").replace("é", "e")
    d = alias.get(d, d)
    return d[:3]


def _days_to_weekday(days):
    mask = 0
    for d in days:
        mask |= WEEKDAY_BITS.get(_norm_day(d), 0)
    return mask


def _time_to_daytime(hhmm):
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)


def _plan_orderid(plan):
    """orderid estable (para activar/desactivar/borrar SIEMPRE el mismo plan)."""
    if plan.get("orderid"):
        return int(plan["orderid"])
    seed = str(plan.get("id", plan.get("name", "")))
    h = int(hashlib.md5(seed.encode()).hexdigest()[:8], 16)
    return 1700000000 + (h % 89999999)


def _current_mapid():
    """map_head_id del mapa activo: en vivo (report_data) o desde .env."""
    mid = _state.get("map_head_id")
    if mid:
        return int(mid)
    env_mid = _env("MAP_HEAD_ID", "").strip()
    return int(env_mid) if env_mid.isdigit() else 0


def build_order(plan, enable=None):
    """Construye el objeto 'order' de setOrder6090 desde un plan de plans.json."""
    rooms = []
    for r in plan.get("rooms", []):
        rid = int(r["room"])
        rooms.append({
            "material_type": ROOM_MATERIAL.get(rid, 2),
            "room_id": rid,
            "sweep_mode": 0,
            "room_name": ROOMS.get(rid, ""),
            "waterlevel": WATER_LEVELS.get(r.get("water", "Medio"), 12),
            "windpower": FAN_LEVELS.get(r.get("fan", "Normal"), 2),
            "carpet": 0,
            "twiceclean": 1 if r.get("twice") else 0,
            "shake_shift": MOP_LEVELS.get(r.get("mop", "Estándar"), 1),
            "cleanmode": 0,
            "room_type": ROOM_TYPES.get(rid, 0),
        })
    en = plan.get("enable", True) if enable is None else enable
    return {
        "orderid": _plan_orderid(plan),
        "order_name": plan.get("name", "Plan"),
        "enable": 1 if en else 0,
        "repeat": 1,
        "weekday": _days_to_weekday(plan.get("days", [])),
        "day_time": _time_to_daytime(plan.get("time", "0:00")),
        "mapid": _current_mapid(),
        "mapName": _state.get("map_name") or "Interior",
        "is_global": 0,
        "clean_type": 0,
        "arealist": [],
        "virwallList": [],
        "roomPer": rooms,
    }


def send_set_order(plan, enable=None):
    """Envia (crea/actualiza) un plan al robot."""
    if _current_mapid() == 0:
        print("  [PLANS] aviso: aun no conozco el map_head_id (espera a que el "
              "robot reporte, o pon MAP_HEAD_ID en .env)")
    robot_send_control({"control": "setOrder6090",
                        "order": build_order(plan, enable)})


def send_delete_order(orderid):
    robot_send_control({"control": "deleteOrder6090", "orderid": int(orderid)})


def send_get_order():
    robot_send_control({"control": "getOrder6090", "userid": ROBOT_USERID})


def send_get_quiet():
    """Consulta el horario 'no molestar' / modo silencioso del robot (get_quiet)."""
    robot_send_control({"control": "get_quiet", "userid": ROBOT_USERID})


def _hhmm_to_min(hhmm):
    """'23:15' -> 1395 (minutos desde medianoche)."""
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)


def _min_to_hhmm(mins):
    """1395 -> '23:15'."""
    try:
        m = int(mins)
    except (TypeError, ValueError):
        return "00:00"
    return f"{(m // 60) % 24:02d}:{m % 60:02d}"


def send_set_quiet(is_open, begin_time, end_time):
    """Fija el horario 'no molestar' del robot (set_quiet). CONFIRMADO por captura:
    espejo de get_quiet. begin_time/end_time en minutos desde medianoche."""
    robot_send_control({"control": "set_quiet", "quiet_count": 1,
                        "quiet_list": [{"quietID": 0,
                                        "is_open": 1 if is_open else 0,
                                        "begin_time": int(begin_time),
                                        "end_time": int(end_time)}]})


def _quiet_or_default():
    """Config actual del 'no molestar' (de get_quiet) o un valor por defecto."""
    q = _state.get("quiet")
    if q:
        return q
    return {"is_open": 0, "begin_time": 1320, "end_time": 420}  # 22:00-07:00


# ============ CONTROLES ADICIONALES (captura dirigida 16-07-2026) ============
# Modos de limpieza: set_mode con value:4 selecciona el TIPO (confirmado en vivo).
CLEAN_MODES = {
    "Auto": 0, "Limpieza completa": 14, "Fregado": 2, "Bordes": 1,
    "Espiral": 5, "Espiral cuadrada": 15, "Punto": 6,
}
# Tipo de base instalada (set_preference ctrltype 17).
BASE_TYPES = {"Base de carga": 0, "Colector automático": 1}


def send_set_pref(ctrltype, value):
    """set_preference generico (succion 1 / agua 2 / x2 3 / turbo-alfombra 5 /
    mopa 15 / tipo-base 17...)."""
    robot_send_control({"control": "set_preference",
                        "ctrltype": int(ctrltype), "value": int(value)})


def send_select_mode(mode_name):
    """Selecciona un TIPO de limpieza (set_mode type=<modo>, value=4)."""
    t = CLEAN_MODES.get(mode_name)
    if t is None:
        print(f"  [!] modo de limpieza desconocido: {mode_name}")
        return
    robot_send_control({"control": "set_mode", "mapid": 0, "type": t, "value": 4})


def send_dust_action():
    """Vacia la base ahora (autovaciado manual)."""
    robot_send_control({"control": "set_dust_action", "action": 1})


def send_set_upgrade(auto_upgrade):
    """Activa/desactiva las actualizaciones automaticas (OTA)."""
    robot_send_control({"control": "set_upgrade_config",
                        "auto_upgrade": 1 if auto_upgrade else 0})


def send_set_voice(voice_mode, volume):
    """Fija voz (on/off) y volumen (0-10)."""
    robot_send_control({"control": "set_voice",
                        "voiceMode": 1 if voice_mode else 0,
                        "volume": max(0, min(10, int(volume)))})


def send_get_consumables():
    robot_send_control({"control": "get_consumables", "userid": ROBOT_USERID})


def send_get_upgrade():
    robot_send_control({"control": "get_upgrade_config", "userid": ROBOT_USERID})


def send_get_voice():
    robot_send_control({"control": "get_voice", "userid": ROBOT_USERID})


def sync_all_plans():
    """Empuja todos los planes de plans.json al robot (segun su campo enable)."""
    if not _plans_sched:
        print("[PLANS] no hay plans.json que sincronizar")
        return
    for p in _plans_sched:
        send_set_order(p)
        time.sleep(0.3)
    print(f"[PLANS] {len(_plans_sched)} plan(es) sincronizados con el robot")


# Anti-rebote: ignora comandos identicos repetidos en menos de N segundos
_last_cmd = {"cmd": None, "t": 0.0}
CMD_DEBOUNCE = 1.5

# ==================== estado compartido ====================
_robot = {"sock": None}
_robot_lock = threading.Lock()
_state = {"battery": None, "mode": None, "charge": None, "fault": None,
          "area": None, "time": None, "water": None, "room": None,
          "map_head_id": None, "map_name": None,
          "quiet": None,          # {"is_open","begin_time","end_time"} no molestar
          "voice": None,          # {"voiceMode","volume"} (de get_voice)
          "consumables": None,    # {"main_brush","side_brush","filter","dishcloth"}
          "auto_upgrade": None}   # 0/1 (de get_upgrade_config)
# Ajustes de solo-escritura (no hay lectura fiable): se muestran en HA con estos
# valores y se actualizan al cambiarlos. base_type: la mayoria trae colector auto.
_extra = {"turbo_carpet": False, "base_type": "Colector automático", "mode": "Auto"}
_mqttc = None
# Consultas tras conectar: se reintentan en los primeros reportes hasta respuesta
# (el robot a veces ignora la primera get_* al reconectar).
_diag = {"quiet_tries": 0, "info_tries": 0}


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

    # --- Horarios programados (setOrder6090), uno por plan de plans.json ---
    for p in _plans_sched:
        pid = p["id"]
        swcfg = {
            "name": f"Horario {p.get('name', pid)}",
            "unique_id": f"{UID}_plan_{pid}",
            "command_topic": f"conga/{NODE}/plan/{pid}/set",
            "state_topic": f"conga/{NODE}/plan/{pid}",
            "payload_on": "on", "payload_off": "off",
            "icon": "mdi:calendar-clock",
            "availability_topic": T_AVAIL,
            "payload_available": "online", "payload_not_available": "offline",
            "device": device_ref,
        }
        client.publish(f"{DISC_PREFIX}/switch/{UID}_plan_{pid}/config",
                       json.dumps(swcfg), retain=True)
        client.publish(f"conga/{NODE}/plan/{pid}",
                       "on" if p.get("enable", True) else "off", retain=True)
    # Botones de gestion de horarios (solo si hay planes definidos)
    if _plans_sched:
        for bid, bname, icon in (("sync", "Sincronizar horarios", "mdi:calendar-sync"),
                                 ("get", "Consultar horarios", "mdi:calendar-search")):
            bcfg = {
                "name": f"Conga {bname}", "unique_id": f"{UID}_plans_{bid}",
                "command_topic": f"conga/{NODE}/plans/{bid}",
                "payload_press": "1", "icon": icon,
                "availability_topic": T_AVAIL,
                "payload_available": "online", "payload_not_available": "offline",
                "device": device_ref,
            }
            client.publish(f"{DISC_PREFIX}/button/{UID}_plans_{bid}/config",
                           json.dumps(bcfg), retain=True)
        print(f"[MQTT] {len(_plans_sched)} horario(s) programado(s) publicados")

    # --- No molestar (set_quiet): interruptor ON/OFF + horas de inicio/fin ---
    qsw = {
        "name": "Conga No molestar", "unique_id": f"{UID}_quiet",
        "command_topic": f"conga/{NODE}/quiet/enabled/set",
        "state_topic": f"conga/{NODE}/quiet/enabled",
        "payload_on": "on", "payload_off": "off", "icon": "mdi:sleep",
        "availability_topic": T_AVAIL,
        "payload_available": "online", "payload_not_available": "offline",
        "device": device_ref,
    }
    client.publish(f"{DISC_PREFIX}/switch/{UID}_quiet/config",
                   json.dumps(qsw), retain=True)
    for part, pname, icon in (("begin", "No molestar inicio", "mdi:weather-night"),
                              ("end", "No molestar fin", "mdi:weather-sunny")):
        tcfg = {
            "name": f"Conga {pname}", "unique_id": f"{UID}_quiet_{part}",
            "command_topic": f"conga/{NODE}/quiet/{part}/set",
            "state_topic": f"conga/{NODE}/quiet/{part}",
            "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$", "icon": icon,
            "availability_topic": T_AVAIL,
            "payload_available": "online", "payload_not_available": "offline",
            "device": device_ref,
        }
        client.publish(f"{DISC_PREFIX}/text/{UID}_quiet_{part}/config",
                       json.dumps(tcfg), retain=True)
    publish_quiet_state(client)  # estado inicial si ya lo conocemos
    print("[MQTT] control de 'no molestar' publicado")

    # --- Controles adicionales (captura dirigida 16-07-2026) ---
    # Boton: vaciar base (autovaciado manual)
    client.publish(f"{DISC_PREFIX}/button/{UID}_dust/config", json.dumps({
        "name": "Conga Vaciar base", "unique_id": f"{UID}_dust",
        "command_topic": f"conga/{NODE}/dust_action", "payload_press": "1",
        "icon": "mdi:delete-empty", "availability_topic": T_AVAIL,
        "payload_available": "online", "payload_not_available": "offline",
        "device": device_ref}), retain=True)

    # Interruptores on/off: turbo alfombras, OTA, voz
    for uid, name, cmd, state, icon, init in (
        ("turbo_carpet", "Turbo en alfombras", f"conga/{NODE}/turbo_carpet/set",
         f"conga/{NODE}/turbo_carpet", "mdi:rug",
         "on" if _extra["turbo_carpet"] else "off"),
        ("ota", "Actualizaciones automáticas", f"conga/{NODE}/ota/set",
         f"conga/{NODE}/ota", "mdi:cloud-download", None),
        ("voice", "Voz", f"conga/{NODE}/voice/enabled/set",
         f"conga/{NODE}/voice/enabled", "mdi:account-voice", None),
    ):
        client.publish(f"{DISC_PREFIX}/switch/{UID}_{uid}/config", json.dumps({
            "name": f"Conga {name}", "unique_id": f"{UID}_{uid}",
            "command_topic": cmd, "state_topic": state,
            "payload_on": "on", "payload_off": "off", "icon": icon,
            "availability_topic": T_AVAIL,
            "payload_available": "online", "payload_not_available": "offline",
            "device": device_ref}), retain=True)
        if init is not None:
            client.publish(state, init, retain=True)

    # Number: volumen de voz (0-10)
    client.publish(f"{DISC_PREFIX}/number/{UID}_volume/config", json.dumps({
        "name": "Conga Volumen voz", "unique_id": f"{UID}_volume",
        "command_topic": f"conga/{NODE}/voice/volume/set",
        "state_topic": f"conga/{NODE}/voice/volume",
        "min": 0, "max": 10, "step": 1, "icon": "mdi:volume-high",
        "availability_topic": T_AVAIL,
        "payload_available": "online", "payload_not_available": "offline",
        "device": device_ref}), retain=True)

    # Selectores: tipo de base y modo de limpieza
    for uid, name, cmd, state, options, init, icon in (
        ("base_type", "Tipo de base", f"conga/{NODE}/base_type/set",
         f"conga/{NODE}/base_type", list(BASE_TYPES.keys()),
         _extra["base_type"], "mdi:home-import-outline"),
        ("mode", "Modo de limpieza", f"conga/{NODE}/mode/set",
         f"conga/{NODE}/mode", list(CLEAN_MODES.keys()),
         _extra["mode"], "mdi:broom"),
    ):
        client.publish(f"{DISC_PREFIX}/select/{UID}_{uid}/config", json.dumps({
            "name": f"Conga {name}", "unique_id": f"{UID}_{uid}",
            "command_topic": cmd, "state_topic": state, "options": options,
            "icon": icon, "availability_topic": T_AVAIL,
            "payload_available": "online", "payload_not_available": "offline",
            "device": device_ref}), retain=True)
        client.publish(state, init, retain=True)

    # Sensores de consumibles (vida de cepillos/filtro/mopa)
    for key, name, icon in (
        ("main_brush", "Cepillo central", "mdi:broom"),
        ("side_brush", "Cepillo lateral", "mdi:broom"),
        ("filter", "Filtro", "mdi:air-filter"),
        ("dishcloth", "Mopa", "mdi:water"),
    ):
        client.publish(f"{DISC_PREFIX}/sensor/{UID}_cons_{key}/config", json.dumps({
            "name": f"Conga {name}", "unique_id": f"{UID}_cons_{key}",
            "state_topic": f"conga/{NODE}/consumable/{key}", "icon": icon,
            "availability_topic": T_AVAIL,
            "payload_available": "online", "payload_not_available": "offline",
            "device": device_ref}), retain=True)

    # Estados iniciales conocidos (si el robot ya respondio a las get_*)
    publish_voice_state(client)
    publish_consumables(client)
    if _state.get("auto_upgrade") is not None:
        client.publish(f"conga/{NODE}/ota",
                       "on" if _state["auto_upgrade"] else "off", retain=True)
    print("[MQTT] controles adicionales publicados (base, voz, OTA, modo, consumibles)")


def _is_error_fault(fault):
    """True si el faultCode es un error real, no un aviso de estacion (21xx) ni
    de consumible (5xx), que se ignoran."""
    try:
        f = int(fault)
    except (TypeError, ValueError):
        return False
    return f != 0 and not (2100 <= f <= 2199) and not (500 <= f <= 599)


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
    if _is_error_fault(fault):
        ha_state = "error"

    bat = _state["battery"]
    bat_pct = int(bat / 2) if isinstance(bat, int) else None  # 0-200 -> 0-100

    payload = {"state": ha_state}
    if bat_pct is not None:
        payload["battery_level"] = bat_pct
    client.publish(T_STATE, json.dumps(payload), retain=True)


def publish_quiet_state(client):
    """Refleja el 'no molestar' (interruptor + horas de inicio/fin) en HA."""
    q = _state.get("quiet")
    if not q:
        return
    client.publish(f"conga/{NODE}/quiet/enabled",
                   "on" if q.get("is_open") else "off", retain=True)
    client.publish(f"conga/{NODE}/quiet/begin",
                   _min_to_hhmm(q.get("begin_time")), retain=True)
    client.publish(f"conga/{NODE}/quiet/end",
                   _min_to_hhmm(q.get("end_time")), retain=True)


def publish_voice_state(client):
    """Refleja voz (on/off) y volumen en HA."""
    v = _state.get("voice")
    if not v:
        return
    client.publish(f"conga/{NODE}/voice/enabled",
                   "on" if v.get("voiceMode") else "off", retain=True)
    client.publish(f"conga/{NODE}/voice/volume", str(v.get("volume", 10)),
                   retain=True)


def publish_consumables(client):
    """Publica la vida de los consumibles como sensores."""
    c = _state.get("consumables")
    if not c:
        return
    for key in ("main_brush", "side_brush", "filter", "dishcloth"):
        if c.get(key) is not None:
            client.publish(f"conga/{NODE}/consumable/{key}", str(c[key]),
                           retain=True)


def on_mqtt_connect(client, userdata, flags, reason_code, properties=None):
    # paho v2 pasa un ReasonCode (con .value); v1 pasa un int. Normalizamos.
    rc = getattr(reason_code, "value", reason_code)
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
    client.subscribe(f"conga/{NODE}/plan/+/set")
    client.subscribe(f"conga/{NODE}/plans/sync")
    client.subscribe(f"conga/{NODE}/plans/get")
    client.subscribe(f"conga/{NODE}/quiet/+/set")
    client.subscribe(f"conga/{NODE}/dust_action")
    client.subscribe(f"conga/{NODE}/turbo_carpet/set")
    client.subscribe(f"conga/{NODE}/ota/set")
    client.subscribe(f"conga/{NODE}/voice/+/set")
    client.subscribe(f"conga/{NODE}/base_type/set")
    client.subscribe(f"conga/{NODE}/mode/set")
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
            send_set_pref(3, 1 if _prefs["twice"] else 0)  # x2 real (ctrltype 3)
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
    # Activar/desactivar un horario programado (switch por plan)
    if topic.startswith(f"conga/{NODE}/plan/") and topic.endswith("/set"):
        pid = topic.split("/")[-2]
        plan = next((p for p in _plans_sched if str(p["id"]) == pid), None)
        if plan is None:
            print(f"  [!] horario desconocido: {pid}")
            return
        enable = (payload == "on")
        send_set_order(plan, enable=enable)
        client.publish(f"conga/{NODE}/plan/{pid}", payload, retain=True)
        print(f"[MQTT] horario '{pid}' -> {'activado' if enable else 'desactivado'}")
        return
    # Botones de gestion de horarios
    if topic == f"conga/{NODE}/plans/sync":
        print("[MQTT] sincronizando horarios con el robot")
        sync_all_plans()
        return
    if topic == f"conga/{NODE}/plans/get":
        print("[MQTT] consultando horarios guardados en el robot")
        send_get_order()
        return
    # Control del "no molestar" (set_quiet): interruptor y horas de inicio/fin
    if topic.startswith(f"conga/{NODE}/quiet/") and topic.endswith("/set"):
        part = topic.split("/")[-2]  # enabled, begin o end
        q = dict(_quiet_or_default())
        try:
            if part == "enabled":
                q["is_open"] = 1 if payload == "on" else 0
            elif part == "begin":
                q["begin_time"] = _hhmm_to_min(payload)
            elif part == "end":
                q["end_time"] = _hhmm_to_min(payload)
            else:
                return
        except Exception as e:
            print(f"  [!] valor de no molestar invalido: {payload} ({e})")
            return
        send_set_quiet(q["is_open"], q["begin_time"], q["end_time"])
        _state["quiet"] = q
        publish_quiet_state(client)
        send_get_quiet()  # confirmar desde el robot
        print(f"[MQTT] no molestar -> {'ON' if q['is_open'] else 'off'} "
              f"{_min_to_hhmm(q['begin_time'])}-{_min_to_hhmm(q['end_time'])}")
        return
    # Vaciar base (boton)
    if topic == f"conga/{NODE}/dust_action":
        print("[MQTT] vaciar base (autovaciado manual)")
        send_dust_action()
        return
    # Turbo en alfombras (ctrltype 5)
    if topic == f"conga/{NODE}/turbo_carpet/set":
        _extra["turbo_carpet"] = (payload == "on")
        send_set_pref(5, 1 if _extra["turbo_carpet"] else 0)
        client.publish(f"conga/{NODE}/turbo_carpet", payload, retain=True)
        print(f"[MQTT] turbo alfombras -> {payload}")
        return
    # Actualizaciones automaticas / OTA (set_upgrade_config)
    if topic == f"conga/{NODE}/ota/set":
        on = (payload == "on")
        send_set_upgrade(on)
        _state["auto_upgrade"] = 1 if on else 0
        client.publish(f"conga/{NODE}/ota", payload, retain=True)
        print(f"[MQTT] OTA auto_upgrade -> {payload}")
        return
    # Voz (on/off) y volumen (0-10) -> set_voice
    if topic.startswith(f"conga/{NODE}/voice/") and topic.endswith("/set"):
        part = topic.split("/")[-2]  # enabled o volume
        v = dict(_state.get("voice") or {"voiceMode": 1, "volume": 10})
        if part == "enabled":
            v["voiceMode"] = 1 if payload == "on" else 0
        elif part == "volume":
            try:
                v["volume"] = max(0, min(10, int(float(payload))))
            except ValueError:
                return
        else:
            return
        send_set_voice(v["voiceMode"], v["volume"])
        _state["voice"] = v
        publish_voice_state(client)
        print(f"[MQTT] voz -> voiceMode={v['voiceMode']} volume={v['volume']}")
        return
    # Tipo de base (ctrltype 17)
    if topic == f"conga/{NODE}/base_type/set":
        val = BASE_TYPES.get(payload)
        if val is None:
            print(f"  [!] tipo de base desconocido: {payload}")
            return
        send_set_pref(17, val)
        _extra["base_type"] = payload
        client.publish(f"conga/{NODE}/base_type", payload, retain=True)
        print(f"[MQTT] tipo de base -> {payload}")
        return
    # Modo de limpieza (set_mode type/value:4)
    if topic == f"conga/{NODE}/mode/set":
        if payload not in CLEAN_MODES:
            print(f"  [!] modo desconocido: {payload}")
            return
        send_select_mode(payload)
        _extra["mode"] = payload
        client.publish(f"conga/{NODE}/mode", payload, retain=True)
        print(f"[MQTT] modo de limpieza -> {payload}")
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
    # paho-mqtt 2.x pide declarar la version de la API de callbacks. Usamos la v2
    # (on_connect recibe reason_code + properties). Con respaldo para paho 1.x.
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="conga_bridge")
    except (AttributeError, TypeError):
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
            # Registrar el codigo EXACTO cuando el robot marca error real (ej. el
            # que aparecio a las 22:00). Solo la primera vez que aparece ese codigo.
            if _is_error_fault(new_fault) and new_fault != _state["fault"]:
                print(f"  [!! FAULT] faultCode={new_fault} -> ERROR en HA "
                      f"(workMode={new_mode} charge={new_charge})")
            _state["battery"] = data.get("battary")
            _state["charge"] = new_charge
            _state["mode"] = new_mode
            _state["fault"] = new_fault
            _state["area"] = data.get("cleanSize")
            _state["time"] = data.get("cleanTime")
            _state["water"] = data.get("waterlevel")
            _state["room"] = data.get("cleaning_roomId")
            if data.get("map_head_id"):
                _state["map_head_id"] = data.get("map_head_id")
            if data.get("current_map_name"):
                _state["map_name"] = data.get("current_map_name")
            # Consultar el "no molestar" hasta obtener respuesta (reintenta unos
            # reportes; el robot a veces ignora el primer get_quiet al reconectar).
            if _state.get("quiet") is None and _diag["quiet_tries"] < 6:
                _diag["quiet_tries"] += 1
                send_get_quiet()
            # Consultar info adicional (consumibles / OTA / voz) tras conectar.
            if _diag["info_tries"] < 4 and (_state.get("consumables") is None
                    or _state.get("auto_upgrade") is None):
                _diag["info_tries"] += 1
                send_get_consumables()
                send_get_upgrade()
                send_get_voice()
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
        # Acuses de la app hacia el robot: incluyen resultados de nuestros
        # comandos de horario (getOrder6090 devuelve la lista de planes).
        try:
            data = json.loads(msg.get("content", "{}")).get("data", {})
            ctrl = data.get("control")
            if ctrl == "getOrder6090":
                orders = data.get("orders", [])
                print(f"  [robot] horarios guardados: {len(orders)}")
                for o in orders:
                    print(f"    - '{o.get('order_name')}' id={o.get('orderid')} "
                          f"{'ON' if o.get('enable') else 'off'} "
                          f"weekday={o.get('weekday')} day_time={o.get('day_time')} "
                          f"rooms={[r.get('room_id') for r in o.get('roomPer', [])]}")
                if _mqttc:
                    _mqttc.publish(f"conga/{NODE}/plans/list", json.dumps(orders))
            elif ctrl in ("setOrder6090", "deleteOrder6090"):
                print(f"  [robot] {ctrl} result={data.get('result')}")
            elif ctrl == "get_quiet":
                qlist = data.get("quiet_list") or []
                if qlist:
                    _state["quiet"] = {
                        "is_open": qlist[0].get("is_open", 0),
                        "begin_time": qlist[0].get("begin_time", 1320),
                        "end_time": qlist[0].get("end_time", 420)}
                    print(f"  [robot] no molestar: "
                          f"{'ON' if _state['quiet']['is_open'] else 'off'} "
                          f"{_min_to_hhmm(_state['quiet']['begin_time'])}-"
                          f"{_min_to_hhmm(_state['quiet']['end_time'])}")
                    if _mqttc:
                        publish_quiet_state(_mqttc)
            elif ctrl == "get_consumables":
                _state["consumables"] = {
                    "main_brush": data.get("main_brush"),
                    "side_brush": data.get("side_brush"),
                    "filter": data.get("filter"),
                    "dishcloth": data.get("dishcloth")}
                print(f"  [robot] consumibles: {_state['consumables']}")
                if _mqttc:
                    publish_consumables(_mqttc)
            elif ctrl == "get_upgrade_config":
                _state["auto_upgrade"] = 1 if data.get("auto_upgrade") else 0
                print(f"  [robot] auto_upgrade = {_state['auto_upgrade']}")
                if _mqttc:
                    _mqttc.publish(f"conga/{NODE}/ota",
                                   "on" if _state["auto_upgrade"] else "off",
                                   retain=True)
            elif ctrl == "get_voice":
                _state["voice"] = {"voiceMode": data.get("voiceMode", 1),
                                   "volume": data.get("volume", 10)}
                print(f"  [robot] voz: voiceMode={_state['voice']['voiceMode']} "
                      f"volume={_state['voice']['volume']}")
                if _mqttc:
                    publish_voice_state(_mqttc)
            elif ctrl in ("set_quiet", "set_voice", "set_upgrade_config",
                          "set_dust_action"):
                print(f"  [robot] {ctrl} result={data.get('result')}")
            elif ctrl and ctrl != "status":
                # cualquier otra respuesta/acuse (para descubrir comandos nuevos)
                print(f"  [robot] respuesta '{ctrl}': {json.dumps(data, ensure_ascii=False)}")
        except Exception:
            pass
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
