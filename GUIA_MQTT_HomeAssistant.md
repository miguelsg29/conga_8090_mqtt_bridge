# Guía: Conectar el Conga 8090 a Home Assistant vía MQTT

Puente `conga_mqtt_bridge.py`. Convierte todo el trabajo de ingeniería inversa en
una entidad `vacuum` real dentro de Home Assistant, con botones de
limpiar/pausar/parar/volver a base, nivel de batería y estado.

## Cómo funciona

El puente tiene dos caras que corren a la vez:

- **Cara robot**: un servidor TLS+WebSocket+JSON que suplanta la nube de Cecotec.
  El Conga se conecta a él (por el DNS rewrite), hace login, manda heart-beat y
  su estado (`report_data`).
- **Cara HA**: un cliente MQTT que publica un mensaje de *autodiscovery* (para que
  HA cree la entidad `vacuum` sola), publica el estado del robot, y escucha los
  comandos que envías desde HA para reenviárselos al robot.

```
  Conga 8090  --TLS/WS/JSON-->  [ conga_mqtt_bridge.py ]  --MQTT-->  Home Assistant
                                                          <--MQTT--   (comandos)
```

## Requisitos previos

1. **Certificados**: `cert.pem` y `key.pem` (los autofirmados que ya generaste)
   junto al script.
2. **DNS rewrite** en AdGuard: `tcp-cecotec.3irobotix.net` -> IP de la máquina
   que corre el puente. (Si lo corres en el mismo PC de siempre, `192.168.31.5`).
3. **Puerto 9090** abierto en el firewall de esa máquina.
4. **paho-mqtt**: `pip install paho-mqtt`
5. **Credenciales MQTT** de tu broker Mosquitto (el add-on de HA).

## Configuración del script

Las credenciales y ajustes van en un fichero **`.env`** junto al script (así no
quedan en el código). Copia `.env.example` como `.env` y rellena:

```
MQTT_HOST=192.168.31.2
MQTT_PORT=1883
MQTT_USER=tu_usuario_mqtt
MQTT_PASS=tu_password_mqtt

# Config de limpieza por defecto (para limpiar por habitacion)
DEFAULT_FAN=Normal        # Off / Eco / Normal / Turbo
DEFAULT_WATER=Medio       # Off / Bajo / Medio / Alto
DEFAULT_MOP=Estándar      # Off / Estándar / Fuerte / Potente
DEFAULT_TWICE=off         # on / off (doble pasada)
```

Para las credenciales MQTT: si usas el add-on Mosquitto de HA, normalmente se crea
un usuario de HA para MQTT, o defines uno en la config del add-on. Si no tienes uno,
crea un usuario en **Ajustes -> Personas -> Usuarios** y úsalo, o define
`logins` en la configuración del add-on Mosquitto.

El `.env` no debe subirse a git (añádelo a `.gitignore`).

## Lanzar

```
python conga_mqtt_bridge.py
```

Salida esperada:

```
=== Puente Conga 8090 <-> Home Assistant (MQTT) ===
[ROBOT] servidor escuchando en 0.0.0.0:9090
[MQTT] conectado (rc=0)
[MQTT] discovery publicado en homeassistant/vacuum/conga8090/config
[robot] conexion desde 192.168.31.15
  [robot] conectado ✓
  [robot] LOGIN
```

En cuanto conecte el MQTT, **en Home Assistant aparece solo** un dispositivo
"Conga 8090" (Ajustes -> Dispositivos y servicios -> MQTT). La entidad
`vacuum.conga_8090` tendrá los controles de aspiradora.

## Uso en Home Assistant

Aparece un dispositivo "Conga 8090" con estas entidades:

**Aspiradora** (`vacuum.conga_8090`): iniciar, pausar, parar, volver a base,
localizar (pita). Estado y batería se actualizan solos.

**Sensores**: batería (%), área limpiada (m²), tiempo de limpieza (min).

**Botones "Limpiar <habitación>"**: uno por cada habitación (Cocina, Salón,
Baño...). Al pulsarlo, el robot limpia SOLO esa habitación.

**Selectores de configuración**: Potencia de succión, Nivel de agua, Vibración de
la mopa. Y un switch de Doble pasada (x2).

### Configuración de la limpieza por habitación

Cuando pulsas un botón "Limpiar <habitación>", el bridge primero aplica la
configuración actual de los selectores (potencia, agua, mopa) y el switch x2, y
luego lanza la limpieza de esa habitación. Es decir: ajusta los selectores como
quieras y luego pulsa la habitación. Los valores iniciales salen del `.env`
(DEFAULT_FAN, etc.) y puedes cambiarlos en caliente desde HA.

Los cambios en los selectores también se aplican al robot al instante (útil
durante una limpieza en curso).

Mapeo de comandos HA -> robot (lógica inteligente según el modo actual):

| Botón HA | Si limpiando (36) | Si pausado (37) | Si volviendo (5) |
|---|---|---|---|
| Start | inicia (set_mode 0/1) | reanuda (2/1) | reanuda (2/1) |
| Pause | pausa (2/2) | — | cancela retorno (3/0) |
| Stop | pausa (2/2) | pausa (2/2) | cancela retorno (3/0) |
| Return to base | va a base (3/1) | va a base (3/1) | va a base (3/1) |
| Locate | get_status (placeholder, no hace sonido) |

La clave: cada modo del robot acepta comandos distintos. El puente detecta el
`workMode` actual y envía el `set_mode` correcto. Por eso Start reanuda si está
pausado (en vez de reiniciar), y Stop cancela el retorno si va hacia la base.

## Notas y siguientes pasos

- **Persistencia**: mientras el puente corra, el robot vive contra él. Si paras el
  script, el robot se queda sin nube (ni la de Cecotec ni la tuya). Para uso
  permanente, conviene correrlo como servicio (systemd en un Linux, o Tarea
  Programada en Windows), o mejor, empaquetarlo como add-on de HAOS.
- **Batería**: se reporta en escala 0-200; el puente ya la convierte a 0-100%.
- **Sin stop puro**: el 8090 no expone un "stop" distinto de "pause". Se mapea Stop
  a pausa. Volver a base sí es un comando propio.
- **Pendiente (mejoras)**:
  - Exponer el **mapa** como entidad `camera` (usando `decodificar_mapa.py` para
    renderizar el PNG y publicarlo por MQTT).
  - **Limpieza por habitaciones** usando los ids del mapa (10-16) y el comando de
    segmentos.
  - Sensores extra: consumibles (cepillos/filtro), tiempo y área de limpieza.
  - Empaquetar como **add-on de HAOS** con el DNS rewrite integrado para que
    funcione sin depender de un PC.

## Solución de problemas

- **No aparece en HA**: revisa que MQTT esté bien configurado en HA (integración
  MQTT activa y apuntando a tu Mosquitto). Mira en el broker si llega el mensaje a
  `homeassistant/vacuum/conga8090/config`.
- **`[MQTT] conectado (rc=5)`**: credenciales incorrectas.
- **El robot no conecta**: confirma el DNS rewrite (`tcp-cecotec` -> IP del puente)
  y que el puerto 9090 esté abierto. Reinicia el robot con corte de energía real.
- **Aparece pero sin estado**: normal hasta el primer `report_data` (unos segundos).
