# Conga 8090 Ultra — Especificación del protocolo local

Documento de referencia del trabajo de ingeniería inversa sobre la comunicación
del robot Cecotec Conga 8090 Ultra con la nube de 3irobotix/Cecotec.

Objetivo final: control local del robot desde Home Assistant sin depender de la
nube, suplantando el servidor de Cecotec con un servidor propio.

---

## 1. Resumen ejecutivo

El Conga 8090 **no es compatible con Congatudo/agnoc**. Los modelos soportados por
ese proyecto (3090–6090) hablan un protocolo binario propio de 3irobotix sobre
TCP en claro. El 8090 pertenece a una generación tecnológica posterior y usa una
pila completamente distinta:

```
TCP  ->  TLS 1.2  ->  WebSocket  ->  mensajes JSON
```

Consecuencia práctica: la vía Congatudo está descartada por diseño (su
`PacketSocket` intenta leer el JSON como binario y falla con "Max length
exceeded"). La buena noticia es que el protocolo del 8090 es JSON legible, mucho
más fácil de integrar que un binario ofuscado.

Puntos clave confirmados experimentalmente:

- El robot conecta al puerto **9090** (no al 4010 de los modelos viejos).
- La conexión va cifrada con **TLS 1.2**.
- El robot **acepta certificados TLS autofirmados** (no hay pinning ni validación
  de CA). Esto es lo que hace viable la suplantación local.
- Sobre TLS, el robot hace un **handshake WebSocket** estándar (`GET / HTTP/1.1`
  con `Upgrade: websocket`).
- Dentro de los frames WebSocket viajan **mensajes JSON** (API tipo REST).

---

## 2. Capas del transporte

### 2.1 DNS
El robot resuelve estos dominios (vía el DNS de la red). Para control local, se
redirige `tcp-cecotec` al servidor propio mediante DNS rewrite:

| Dominio | Función |
|---|---|
| `tcp-cecotec.3irobotix.net` | Canal de comandos/datos (TLS+WS, puerto 9090) — **el importante** |
| `web-eu.3irobotix.net` | API web/aprovisionamiento |
| `eu-ota.3irobotix.net`, `cecotec-ota.3irobotix.net` | Actualizaciones de firmware (conviene NO redirigir aquí para evitar OTAs) |
| `eu-log.3irobotics.net` | Telemetría/logs |

Nota: existen dos TLD distintos, `3irobotix.net` (con x) y `3irobotics.net`
(con cs). El robot usa ambos.

### 2.2 TLS
- Versión: TLS 1.2.
- El robot ofrece cipher suites modernas (ECDHE + AES-GCM / ChaCha20).
- **No valida el certificado del servidor**: un cert autofirmado con
  `CN=tcp-cecotec.3irobotix.net` es aceptado sin problema.

### 2.3 WebSocket
Handshake estándar RFC 6455. El robot envía:

```
GET / HTTP/1.1
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Key: <base64>
Sec-WebSocket-Version: 13
Host: tcp-cecotec.3irobotix.net:9090
```

El servidor debe responder `101 Switching Protocols` con el `Sec-WebSocket-Accept`
calculado (SHA1 de la key + magic GUID `258EAFA5-E914-47DA-95CA-C5AB0DC85B11`,
en base64).

Detalle: los frames del **robot van enmascarados** (es cliente WS); los del
**servidor van sin máscara** (es servidor WS). Aparece también algún frame con el
texto `libuwsc` (la librería WebSocket del firmware, uWSC) que puede tratarse
como keep-alive.

---

## 3. Protocolo de aplicación (JSON sobre WebSocket)

### 3.1 Formato de las peticiones del robot -> servidor

```json
{
  "traceId": 1783795174,
  "method": "POST",
  "service": "sweeper-robot-center/auth/login",
  "content": "{...json escapado...}"
}
```

- `traceId`: entero incremental, identifica la petición para casar la respuesta.
- `method`: "POST" o "Get".
- `service`: la "ruta" del servicio.
- `content`: string JSON escapado con los datos.

### 3.2 Formato de las respuestas del servidor -> robot

```json
{
  "code": 0,
  "traceId": "1783795174",
  "service": "sweeper-robot-center/auth/login",
  "result": { ... } 
}
```

- `code`: 0 = OK.
- `traceId`: **como string** en la respuesta (en la petición es número).
- `result`: objeto, booleano (`true`), o valor según el servicio.

### 3.3 Formato de los comandos servidor -> robot (push)

Los comandos que la app/nube empuja al robot usan otro formato, con `tag`:

```json
{
  "tag": "sweeper-transmit/to_bind",
  "content": "{\"control\":\"set_mode\",\"mapid\":0,\"type\":3,\"value\":1}"
}
```

El robot, al recibirlos y actuar, reenvía un acuse por
`sweeper-transmit/transmit/to_bind` con `"targets":[<userId>]` y su `did`.

---

## 4. Servicios observados (robot -> servidor)

| service | Frecuencia | Función |
|---|---|---|
| `sweeper-robot-center/auth/login` | al conectar | Autenticación. Envía credenciales del robot. |
| `heart-beat` | cada ~10 s | Latido. El servidor responde con timestamp. |
| `sweeper-robot-center/device/report_data` | frecuente | Estado del robot (batería, modo, mapa activo…). |
| `sweeper-transmit/transmit/to_bind` | por acción | Acuses/datos hacia la app vinculada. |
| `sweeper-map/robot/syn_no_cache` | al pedir mapa | Sincronización de mapa. |
| `sweeper-robot-center/info_report/status/2` y `/3` | eventos | Reportes de estado puntuales. |
| `sweeper-app-user/robot/get_notice_config` | al arrancar | Config de notificaciones (lista larga, en chino). |
| `sweeper-app-user/robot/get_pets` | ocasional | Detección de mascotas. |
| `sweeper-robot-center/stuff/config?modeType=CRL30V` | al arrancar | Config de consumibles/piezas. |

### 4.1 Login (crítico para suplantar)

Petición del robot (`content` desescapado). Los valores mostrados son EJEMPLO;
los de tu robot los ves en tu propia captura:

```json
{
  "factoryId": 1003,
  "mac": "12:34:56:78:9A:BC",
  "keyt": "XXXXXXXXXXXXXXXX",
  "packageVersions": [
    {"packageType":"ramdisk","version":20,"versionName":"S3.4.20","ctrlVersion":"V5.0"},
    {"packageType":"target","version":20,"versionName":"S3.4.20","ctrlVersion":"V5.0"}
  ],
  "projectType": "CECOTECCRL350-1001",
  "sn": "500400000000"
}
```

Respuesta de la nube (`result.data`):

```json
{
  "AUTH": "<JWT>",
  "FACTORY_ID": "1003",
  "USERNAME": "500400000000",
  "CONNECTION_TYPE": "sweeper",
  "PROJECT_TYPE": "CECOTECCRL350-1001",
  "ROBOT_TYPE": "sweeper",
  "SN": "500400000000",
  "MAC": "12:34:56:78:9A:BC",
  "BIND_LIST": "[\"654321\"]"
}
```

Más `"clientType":"ROBOT"`, `"id":"123456"`, `"resetCode":0` al nivel de `result`.

**Sobre la validación del `AUTH` (JWT) — CONFIRMADO: el robot NO valida el JWT.**

Probado empiricamente con `test_jwt_sintetico.py`: el robot acepta un JWT con
firma literalmente falsa (`SYNTHETIC0SIGNATURE0NO0VALIDATION0NEEDED...`) y sin
campo `timestamp`, y se mantiene estable (1 solo login, heart-beats y report_data
fluyendo sin reconexiones). Conclusion: el robot solo necesita `code:0` y un
`result` bien formado; la firma y la caducidad del token son irrelevantes.

Implicacion practica (aplicada en el puente): se genera un **JWT sintético sin
caducidad** en el propio `conga_mqtt_bridge.py`. Ya no hace falta capturar ni
renovar el token. En el `.env`, dejar `AUTH_JWT` vacio activa este modo; poner un
token lo usa tal cual; `USE_SYNTHETIC_JWT=on` fuerza el sintético.

Estructura del JWT sintético: header `{"typ":"JWT","alg":"HS256"}` + payload con
`{"value": "{...data,clientType:ROBOT,id,resetCode...}", "timestamp": null}` +
firma de relleno. Todo en base64url.

### 4.2 report_data (estado del robot)

`content.data` incluye, entre otros: `battary` (batería, valor 200 = 100%?),
`chargeStatus`, `workMode`, `faultCode`, `cleanTime`, `cleanSize`, `waterlevel`,
`dustBox_type`, `mop_type`, `house_name`, `current_map_name`, `map_head_id`,
`sweep_mode`, y `control:"status"`. Es la fuente para exponer sensores en HA.

---

## 5. Diccionario de comandos (control del robot)

Los comandos van dentro de `content` con la clave `control`. Observados:

| control | Parámetros | Significado |
|---|---|---|
| `set_direct` | `direction` (2,4,5…), `angle` | Movimiento manual (joystick app). |
| `set_mode` | `mapid`, `type`, `value` | Iniciar/parar limpieza, volver a base. |
| `get_status` | — | Pide estado. |
| `get_map` / `getMapAll` | `mapid`, `type`, `mask` | Pide el mapa. |
| `get_consumables` | — | Vida de cepillos/filtro/mopa. |
| `get_device_info` | — | Info de red y versión. |
| `getOrder` | — | Programaciones/horarios. |
| `set_mode` | ver abajo | (ver combinaciones) |
| `setVoiceType` | `Voice` | Tipo/idioma de voz. |
| `set_time` | `timezone`, `time` | Ajuste de hora. |
| `lock_device` | `userid` | Bloqueo del dispositivo. |
| `device_remind` | — | Recordatorios. |

### 5.1 set_mode — combinaciones CONFIRMADAS (captura dirigida)

`set_mode` es el comando central de limpieza. Formato del push:

```json
{"control":"set_mode","mapid":0,"type":<T>,"value":<V>}
```

Acuse del robot: `{"value":<V>,"ctrltype":<T>,"result":0,"control":"set_mode","did":123456}`

Combinaciones confirmadas pulsando cada botón en la app oficial:

| Acción | type | value |
|---|---|---|
| Iniciar limpieza | 0 | 1 |
| Pausar | 2 | 2 |
| Reanudar | 2 | 1 |
| Ir a base (home) | 3 | 1 |
| Cancelar ir a base | 3 | 0 |

Interpretacion: `type` es el CONTEXTO (0=limpieza, 2=pausa/reanudar, 3=retorno a
base), `value` la accion dentro de ese contexto. Importante: para pausar hay que
usar type=2 (no type=0), y cada modo tiene su propia parada.

### 5.2 Direcciones set_direct — CONFIRMADAS (observacion en vivo)

```json
{"angle":<a>,"control":"set_direct","direction":<D>}
```

| direction | Movimiento |
|---|---|
| 1 | Adelante |
| 2 | Izquierda |
| 3 | Derecha |
| 4 | Atras |
| 0 | Soltar (parar joystick) |

### 5.3 Estados observados (workMode en report_data) — CONFIRMADOS

Confirmados observando el log en vivo del robot durante operacion real:

| workMode | charge | Estado real | Estado HA |
|---|---|---|---|
| 0 | 1 | En la base cargando | docked |
| 0 | 0 | Parado fuera de base | idle |
| 5 | - | Volviendo a la base | returning |
| 36 | 0 | Limpiando (activo) | cleaning |
| 37 | 0 | Pausado durante limpieza | paused |
| 2 | - | Control manual (joystick) | cleaning |

Clave: 36 = limpiando, 37 = pausado (son distintos aunque parezcan lo mismo).
El `chargeStatus=1` siempre significa "en base", tiene prioridad sobre workMode.

Bateria: escala 0-200 (200 = 100%). Se divide entre 2 para el porcentaje HA.

faultCode (NO son errores, son avisos y hay que ignorarlos):
- rango 21xx = avisos de estacion (2104=retornando, 2105=carga completa,
  2108/2110 = otros avisos de base)
- rango 5xx = avisos de consumibles (525 = deposito de agua)
Solo se marca "error" en HA para codigos fuera de esos rangos.

### 5.4 set_preference — AJUSTES (potencia, agua, mopa) CONFIRMADOS

Comando para ajustes de configuración. Formato:

```json
{"control":"set_preference","ctrltype":<CT>,"value":<V>}
```

Valores CONFIRMADOS cruzando el plan programado con las capturas de la app:

| ctrltype | Ajuste | Valores |
|---|---|---|
| 1 | **Potencia de succión** | 0=Off, 1=Eco, 2=Normal, 3=Turbo |
| 2 | **Nivel de agua** | 10=Off, 11=Bajo, 12=Medio, 13=Alto |
| 15 | **Vibración de la mopa** | 0=Off, 1=Estándar, 2=Fuerte, 3=Potente |
| 3 | Toggle (on/off) | 0, 1 |
| 5 | Toggle (on/off) | 0, 1 |

Confirmacion (del plan creado en la app):
- Salón: Eco+AguaOff+Estándar → windpower:1, waterlevel:10, shake_shift:1
- Pasillo: Normal+Bajo+Potente → windpower:2, waterlevel:11, shake_shift:3
- Cocina: Turbo+Alto+Potente → windpower:3, waterlevel:13, shake_shift:3

(los campos del plan `windpower`/`waterlevel`/`shake_shift` usan la MISMA escala
que set_preference ctrltype 1/2/15 respectivamente)

### 5.5 Otros comandos descubiertos (via app oficial)

| control | params | Funcion |
|---|---|---|
| `set_dust_action` | `{action:1}` | Vaciado de la base de autovaciado |
| `setVoiceType` | `{Voice:N}` | Idioma/tipo de voz |
| `selectMapPlan` | `{mapid,planid,type}` | Seleccionar plan de mapa |
| `getOrder6090` | `{userid}` | Consultar programaciones/horarios |
| `getMapAll` | `{maplist:[{map_id}]}` | Listar/obtener todos los mapas |
| `get_map` | `{mapid,type,mask}` | Obtener mapa (mask 1/3/4/5/7 = capas) |
| `get_consumables` | `{userid}` | Vida de consumibles |
| `get_voice` / `get_quiet` | `{userid}` | Consultar voz / modo silencioso |
| `get_systemConfig` | `{type,value}` | Config del sistema |
| `get_preference` | `{ctrltype,value}` | Consultar un ajuste |
| `lock_device` | `{userid}` | Keepalive/lock (la app lo manda cada 60s) |
| `device_ctrl` | `{ctrltype,operation}` | Control generico |
| `set_time` | `{timezone,time}` | Ajustar hora |

### 5.6 set_mode types adicionales (tipos de limpieza)

Ademas de los ya confirmados (limpiar/pausar/base), la app usa set_mode con mas
types, todos con value:4, que parecen seleccionar TIPO de limpieza antes de
arrancar (auto, bordes, habitacion, zona, etc.): types 1, 2, 5, 6, 14, 15.
El value:4 comun sugiere "modo seleccionado". Requiere captura dirigida
adicional para mapear cada type a su tipo concreto.

### 5.7 report_data — TODOS los campos de estado

El robot reporta un `data` muy completo. Campos utiles para HA:

| Campo | Significado |
|---|---|
| `workMode` | Estado (0=idle,5=volviendo,36=limpiando,37=pausado,2=manual) |
| `battary` | Bateria 0-200 (200=100%) |
| `chargeStatus` | 1 = en base cargando |
| `faultCode` | Aviso/error (21xx y 5xx son avisos, ignorar) |
| `cleanTime` | Tiempo de limpieza (min) |
| `cleanSize` | Area limpiada (m2) |
| `waterlevel` | Nivel de agua actual |
| `cleanPerference` | Preferencia de limpieza / potencia |
| `cleaning_roomId` | Habitacion que esta limpiando (0=ninguna) |
| `sweep_mode` | Modo de barrido |
| `mop_type` | Tipo de mopa instalada |
| `dustBox_type` | Tipo de deposito |
| `dust_action` | Estado de autovaciado |
| `repeatClean` | Doble pasada (0/1) |
| `house_name` / `current_map_name` | Nombre casa / mapa activo |
| `map_head_id` | ID del mapa activo (ej. 1702045335) |
| `roll_brush_type` / `water_tank_type` | Tipos de cepillo / tanque |

### 5.8 Estado de funciones adicionales

**Localizar / buscar robot — RESUELTO:**
```json
{"control":"device_ctrl","ctrltype":3,"operation":1}
```
El robot pita para encontrarlo. Ya implementado en el bridge (boton locate de HA).

**Limpieza INMEDIATA por habitacion — RESUELTO (`setRoomClean`):**
```json
{"control":"setRoomClean","ctrlValue":1,"roomsID":[11],"clean_type":0}
```
`roomsID` = lista de ids de habitacion a limpiar YA (ej. [13,15] = Cocina+Salón).
`ctrlValue:1` = iniciar. Los ids coinciden con el mapa (10-16). Ya implementado
en el bridge como botones "Limpiar <habitacion>" en HA.

**Zona prohibida / pared virtual — RESUELTO (`set_virwall`):**
```json
{"control":"set_virwall","VirwallCount":1,"clean_plan_id":0,
 "virwallList":[{"PointList":[{"PointX":"-1.20","PointY":"3.40"}, ... 4 puntos],
   "Type":200,"name":"","Count":4,"ID":5445404,"area_type":1}],
 "map_head_id":1702045335,"area_type":1}
```
Define un poligono (4 esquinas con coords X/Y en metros del mapa) como zona
restringida. `Type:200` = pared virtual/zona prohibida. Requiere coordenadas del
mapa, mas complejo de exponer en HA (queda documentado).

**Programación con habitaciones — CONFIRMADO Y RESUELTO (captura MITM 16-07-2026).**

Son TRES comandos que trabajan juntos:

| control | params | Funcion |
|---|---|---|
| `setOrder6090` | `{order:{...}}` | Crear/actualizar un plan (o activar/desactivar via `enable`) |
| `getOrder6090` | `{userid}` | Leer los planes guardados (el robot devuelve `orders:[...]`) |
| `deleteOrder6090` | `{orderid}` | Borrar un plan por su `orderid` |

Crear/guardar un plan (`setOrder6090`) — estructura REAL capturada:

```json
{"control":"setOrder6090","order":{
  "orderid":1700000001, "order_name":"Plan3",
  "enable":1, "repeat":1, "weekday":16, "day_time":1212,
  "mapid":1700000000, "mapName":"Interior",
  "is_global":0, "clean_type":0, "arealist":[], "virwallList":[],
  "roomPer":[
    {"material_type":2,"room_id":11,"sweep_mode":0,"room_name":"Baño privado",
     "waterlevel":13,"windpower":3,"carpet":0,"twiceclean":0,"shake_shift":3,
     "cleanmode":0,"room_type":2103},
    {"material_type":3,"room_id":14,"room_name":"Dormitorio Principal",
     "waterlevel":11,"windpower":2,"shake_shift":2,"room_type":2101, ...},
    {"material_type":3,"room_id":16,"room_name":"Pasillo","waterlevel":11,
     "windpower":3,"shake_shift":3,"room_type":2104, ...},
    {"material_type":2,"room_id":13,"room_name":"Cocina","waterlevel":13,
     "windpower":3,"shake_shift":3,"room_type":2105, ...},
    {"material_type":3,"room_id":15,"room_name":"Salón","waterlevel":11,
     "windpower":2,"shake_shift":2,"room_type":2106, ...}
  ]
}}
```

Campos clave (todo CONFIRMADO cruzando la captura con las acciones en la app):
- `day_time`: **minutos desde medianoche**. Confirmado: 20:12 → `1212`; 19:30 → `1170`.
- `weekday`: **bitmask con domingo = bit 0**. Confirmado: "jueves" → `16`; "sábado" → `64`.
  Tabla: `dom=1 lun=2 mar=4 mie=8 jue=16 vie=32 sab=64`. Varios días = suma de bits.
- `enable`: 1=activo, 0=desactivado. Para activar/desactivar se reenvía el MISMO
  `setOrder6090` (mismo `orderid`) cambiando solo `enable`.
- `orderid`: identificador del plan (la app usa un timestamp-like). Debe ser estable
  para poder actualizar/borrar el mismo plan.
- `mapid`: el `map_head_id` del mapa activo (aparece en `report_data`). Cambia si
  se re-mapea la casa.
- `roomPer[]`: una entrada por habitación con su modo propio:
  `windpower` (0-3, misma escala que set_preference ctrltype 1),
  `waterlevel` (10-13, ctrltype 2), `shake_shift` (0-3, mopa, ctrltype 15),
  `twiceclean` (0/1 doble pasada), `cleanmode` (0=Auto).
  `room_type` es un id semántico por habitación (2101=Dorm. Principal, 2103=Baño
  privado, 2104=Pasillo, 2105=Cocina, 2106=Salón); `material_type` = tipo de suelo.
  El robot ejecuta por `room_id`; `room_type`/`material_type` son metadatos.

Leer planes (`getOrder6090` `{userid}`): el robot responde con `orders:[...]`, cada
plan con los mismos campos más `room_material` (en la lectura) en vez de
`material_type` (en la escritura). Es como se listan/refrescan los horarios.

Borrar (`deleteOrder6090` `{orderid}`): elimina el plan; un `getOrder6090` posterior
ya no lo devuelve. Confirmado en la captura.

**IMPLEMENTADO en el puente**: `conga_mqtt_bridge.py` genera estos tres comandos a
partir de un fichero `plans.json` (ver `plans.example.json`). `build_order()` produce
un `setOrder6090` byte-idéntico al de la app (verificado contra la captura). En Home
Assistant aparece un interruptor por plan (activar/desactivar) y botones "Sincronizar"
y "Consultar" horarios.

**Pendiente (menor)**: limpieza inmediata con modo DISTINTO por habitación en una sola
pasada. Hoy `setRoomClean` aplica un modo global; el modo por habitación sólo está
confirmado dentro de un plan (`roomPer`). Si se quiere inmediato+per-room, requiere
otra captura dirigida (o crear un plan efímero).

**No molestar / modo silencioso (`get_quiet`) — CONFIRMADO (lectura en vivo 16-07-2026).**

Consultando `{"control":"get_quiet","userid":<userid>}`, el robot responde:

```json
{"quiet_count":1,
 "quiet_list":[{"quietID":0,"is_open":1,"begin_time":1320,"end_time":420}],
 "control":"get_quiet"}
```

- `is_open`: 1 = activado, 0 = desactivado.
- `begin_time` / `end_time`: **minutos desde medianoche** (misma escala que `day_time`).
  Confirmado: `1320` = 22:00 (inicio), `420` = 07:00 (fin).

**Importante**: durante la franja de no molestar el robot **aborta la limpieza programada,
vuelve a la base y NO hace autovaciado**. Un plan `setOrder6090` que se solape con esta
franja se corta al llegar la hora de inicio (observado: plan a las 21:52 cortado a las
22:00 con retorno a base). El puente lo detecta como estado `error` momentaneo por el
faultCode que emite el robot al entrar en silencio.

**Escritura (`set_quiet`) — CONFIRMADO (captura MITM 16-07-2026).** Es el espejo
exacto de `get_quiet`:

```json
{"control":"set_quiet","quiet_count":1,
 "quiet_list":[{"quietID":0,"is_open":1,"begin_time":1395,"end_time":570}]}
```

Acuse del robot: `{"result":0,"control":"set_quiet","did":<did>}`. Confirmado pulsando
en la app: cambiar la franja a 23:15-09:30 genero `begin_time:1395`, `end_time:570`.
Para desactivar el no molestar basta con `is_open:0` (mismos begin/end).

**IMPLEMENTADO en el puente**: `send_set_quiet()` genera este comando (verificado
byte-identico a la captura). En Home Assistant hay un interruptor **"Conga No molestar"**
y dos campos de texto **"No molestar inicio"/"fin"** (HH:MM). El puente lee el estado con
`get_quiet` al conectar y lo refleja en HA.

---

## 5.9 Catálogo completo de comandos (captura dirigida 16-07-2026)

Sesión de captura MITM pulsando cada opción en la app oficial. Todo CONFIRMADO en vivo
con acuse `result:0` del robot.

### set_mode — TIPOS de limpieza (con `value:4` = seleccionar modo)

`{"control":"set_mode","mapid":0,"type":<T>,"value":4}` selecciona el modo antes de
arrancar. Confirmado pulsando cada modo (el robot ACK con `ctrltype:<T>`):

| type | Modo |
|---|---|
| 0 | Auto |
| 14 | Limpieza completa |
| 2 | Fregado |
| 1 | Bordes |
| 5 | Espiral |
| 15 | Espiral cuadrada |
| 6 | Punto (spot; el robot manda `device_remind type:2008`) |

El modo **"Área"** no usa `set_mode`: se define con `set_area` (ver abajo). Combinaciones
de acción (`value` != 4) siguen como en §5.1 (0/1=iniciar, 2/2=pausa, 2/1=reanudar,
3/1=base, 3/0=cancelar base).

### set_preference — tabla de `ctrltype` COMPLETA

`{"control":"set_preference","ctrltype":<CT>,"value":<V>}` (lectura con `get_preference`):

| ctrltype | Ajuste | Valores |
|---|---|---|
| 1 | Potencia succión | 0=Off 1=Eco 2=Normal 3=Turbo |
| 2 | Nivel de agua | 10=Off 11=Bajo 12=Medio 13=Alto |
| 3 | **Doble pasada / x2** | 0/1 |
| 5 | **Turbo en alfombras** | 0/1 |
| 15 | Vibración mopa | 0=Off 1=Estándar 2=Fuerte 3=Potente |
| 16 | **Intervalo de autovaciado** (min) | lectura `dust_collect_interval_min` |
| 17 | **Tipo de base** | 0=Base de carga, 1=Colector de polvo automático |

### Voz y volumen

- `{"control":"set_voice","voiceMode":<0/1>,"volume":<0-10>}` — voz on/off + volumen
  (0=mudo … 10=máx). Lectura: `get_voice` → `{voiceMode, volume}`.
- `{"control":"setVoiceType","Voice":<N>}` — idioma/pack de voz (la unidad de prueba = 3).

### Autovaciado y base

- `{"control":"set_dust_action","action":1}` — **vaciar la base ahora** (autovaciado manual).
- Tipo de base y su intervalo: `set_preference` ctrltype 17 y 16 (arriba).

### Actualizaciones OTA (importante para blindar la integración local)

- `{"control":"set_upgrade_config","auto_upgrade":<0/1>}` — 0 desactiva las OTA. Lectura:
  `get_upgrade_config` → `{auto_upgrade}`. **Recomendado 0** para que el robot no se
  auto-actualice y rompa el protocolo reverseado.

### Consumibles

- `get_consumables {userid}` → `{main_brush, side_brush, filter, dishcloth}` (vida de
  cepillo central/lateral, filtro y mopa/paño). Base para sensores en HA.

### Zonas (poligonos con coords en METROS del mapa; `Count` = nº de puntos)

Estructura común: `{VirwallCount:N, clean_plan_id:0, virwallList:[{PointList:[{PointX,
PointY}...], Type, name, Count, ID, area_type}], map_head_id, area_type}`.
**Gestión = reemplazo de lista completa** (para borrar una, reenviar la lista sin ella;
lista vacía `VirwallCount:0` = borrar todas).

- `set_virwall` — **zonas restringidas**: `Type:200` = **prohibida (no-go)**;
  `Type:301` = **sin fregona (no-mop)**.
- `set_area` — **zonas de limpieza**: `Type:200` = 1 pasada; `Type:201` = 2 pasadas (x2).
  Con `area_type:1` en la zona = área temporal para limpieza puntual.

### Mapa y habitaciones

- `setPlanData6090 {mapid, mapName, roomInfo:[{roomID, roomName, roomTypeId,
  roomMaterialId, cleanStatus}]}` — guarda la config de habitaciones. Da la lista real
  de habitaciones (el puente podría leer nombres/tipos de aquí en vez de fijarlos).
  - `roomTypeId`: categoría semántica (Dormitorio=2101, Baño=2103, Pasillo=2104,
    Cocina=2105, Salón=2106, Comedor=2002…); editable.
  - `roomMaterialId` (**tipo de suelo**): **1=Suave, 2=Azulejos, 3=Madera, 4=Alfombra**
    (1 y 3 confirmados directamente; 2/4 por eliminación y contexto).
- `splitRoom {mapId, roomId, startX/Y, endX/Y}` — divide una habitación por una línea
  (el trozo nuevo recibe un id nuevo).
- `mergeRoom {mapId, roomsId:[...]}` — une los ids indicados.
- `setMapAngle {angle}` — rota el mapa (0/90/180/270).
- `selectMapPlan {mapid, planid, type}` — selecciona el plan de mapa activo.
- `set_time {timezone, time}` — pone el reloj del robot (timezone en segundos: 7200=UTC+2;
  time = epoch Unix). Útil si el reloj del robot se desajustara.

Otros observados: `clean_status`, `device_remind` (avisos del robot), `get_device_info`
(wifi/versión), `setRoomClean` (limpieza inmediata por habitación, §5.8).

Pendientes (menores):
- **Limpieza rapida** (boton "Limpieza rapida x1" de la app): probablemente
  un setRoomClean con todas las habitaciones o un set_mode. Sin confirmar.
- Coordenadas del mapa para colocar zonas prohibidas desde HA (set_virwall
  necesita puntos X/Y en metros; hay que mapear la transformacion rejilla<->metros).

---

## 5.4 EL MAPA (formato completo, DECODIFICADO)

El mapa es lo mas complejo y ya esta resuelto. El robot lo envia por el servicio
`sweeper-map/robot/syn_no_cache`. El payload **NO es JSON**: es un frame binario:

```
[cabecera binaria][ "POST" ][ ruta servicio ][ mas cabecera ][ ZLIB (78 9c) ]
```

Los datos van comprimidos con **zlib** (firma `78 9c`). Descomprimidos, son un
mensaje **Protobuf**. Un frame tipico: ~9 KB comprimidos -> ~666 KB
descomprimidos.

### Estructura Protobuf del mapa (campos de nivel superior)

| campo | tipo | contenido |
|---|---|---|
| 1 | varint | tipo/version (=3) |
| 2 | message | metadatos: id, timestamp, resolucion(float)=90.0 |
| 3 | message | parametros: origen, escala, min/max |
| 4 | bytes | **REJILLA 800x800 = 640000 celdas**, 1 byte/celda |
| 5 | message | nombre del mapa ("Interior") |
| 7 | message | pose/posicion del robot (floats) |
| 9 | message (rep) | zonas / paredes virtuales (pares de coords) |
| 12 | message (rep) | **HABITACIONES**: {id(1), nombre(2)} |
| 13 | bytes | tabla de orden/vecindad de habitaciones |
| 14 | message (rep) | celdas por habitacion (spans RLE) |
| 17 | message | jerarquia casa>mapa ("Casa" > "Interior") |

### Valores de la rejilla (campo 4)

| valor | significado |
|---|---|
| 0 | desconocido / sin explorar (la mayoria) |
| 1 | borde/pared fina |
| 255 | pared / obstaculo |
| 10-16 | celdas de cada HABITACION (segun id) |

### Habitaciones de ESTE mapa (cambian si se re-mapea)

| id | habitacion |
|---|---|
| 10 | Dormitorio |
| 11 | Bano privado |
| 12 | Bano |
| 13 | Cocina |
| 14 | Dormitorio Principal |
| 15 | Salon |
| 16 | Pasillo |

Con estos ids se puede implementar **limpieza por habitacion** desde HA.

Herramienta: `decodificar_mapa.py` extrae el zlib, parsea el Protobuf, saca la
rejilla + habitaciones y renderiza un PNG a color. Reutilizable con cualquier
frame de mapa capturado.

---

## 6. Identificadores del robot (COMO OBTENER LOS TUYOS)

Cada robot tiene sus propios identificadores. **NO uses los de otra persona.** Los
tuyos aparecen en el mensaje de login que captura el MITM (seccion 4.1). Busca en
`cap_mitm_full.log` el `auth/login` y saca estos campos:

| Campo | De donde sale | Ejemplo (ficticio) |
|---|---|---|
| `did` (device id) | `result.id` de la respuesta login | `123456` |
| `userId` (app vinculada) | `BIND_LIST` de la respuesta login | `654321` |
| MAC | `mac` de la peticion login | `12:34:56:78:9A:BC` |
| SN / USERNAME | `sn` de la peticion login | `500400000000` |
| `keyt` (login) | `keyt` de la peticion login | `XXXXXXXXXXXXXXXX` |
| `AUTH` (JWT) | `result.data.AUTH` de la respuesta | `eyJ...` |
| WiFi SSID | (aparece en otros mensajes) | `TU_WIFI_SSID` |

Estos valores van en tu fichero `.env` (privado, NO subir a git). El codigo trae
valores de ejemplo que hay que sustituir por los tuyos.

IP nube real de `tcp-cecotec.3irobotix.net`: se obtiene con
`nslookup tcp-cecotec.3irobotix.net 8.8.8.8` (es un dato publico de Cecotec).

---

## 7. Arquitectura de la integración propuesta

```
   Conga 8090
       |  (DNS: tcp-cecotec -> IP del servidor)
       v
  [ Servidor suplantador Python ]   <-- termina TLS (cert autofirmado)
       |                                 responde WS 101
       |                                 habla JSON: login, heart-beat, report_data
       |                                 envia comandos: set_mode, set_direct...
       v
  [ Home Assistant ]   <-- via MQTT (autodiscovery) o integración custom
```

Fases:

1. **Servidor mínimo** (esqueleto): acepta TLS+WS, responde `login` y `heart-beat`,
   registra `report_data`. → **COMPLETADO** (`servidor_conga.py`). El 8090 NO
   valida el JWT; basta con `code:0` y respuesta bien formada.
2. **Control interactivo**: enviar comandos por teclado. → **COMPLETADO**
   (`servidor_conga_control.py`).
3. **Lectura de estado + comandos vía HA (MQTT)**: → **COMPLETADO**
   (`conga_mqtt_bridge.py`). Entidad `vacuum` nativa con autodiscovery, estado en
   tiempo real y control completo (limpiar/pausar/reanudar/parar/base).
4. **Mapa**: → **DECODIFICADO** (`decodificar_mapa.py`). zlib + Protobuf, rejilla
   800x800, 7 habitaciones. Pendiente exponerlo como `camera` en HA.
5. **Empaquetado**: add-on/servicio permanente + DNS rewrite. → PENDIENTE (opcional).

Pendientes opcionales:
- Mapa como entidad `camera` en HA (render PNG -> MQTT).
- Limpieza por habitaciones (ids 10-16 + comando de segmentos).
- Comando "locate" real (no capturado; `get_status` es un placeholder).
- Sensores extra: consumibles, tiempo/área de limpieza.
- Puente como servicio permanente (Tarea Programada Windows / add-on HAOS).

---

## 8. Anexo — Herramientas usadas para capturar

- **DNS rewrite** en AdGuard Home para desviar `tcp-cecotec` al PC de captura.
- **tcpdump** (vía contenedor `nicolaka/netshoot` con `--net=host` en HAOS) para
  descubrir el puerto real (9090) y confirmar conexiones.
- **Proxy Python** con terminación **TLS** (cert autofirmado con OpenSSL) +
  **handshake WebSocket** + desenmascarado de frames, reenviando a la nube real
  como cliente TLS/WS (MITM) para grabar la conversación completa descifrada.

Ficheros de captura de referencia: `cap_9090.log` (TLS Client Hello),
`cap_tls_decrypted.log` (handshake WS), `cap_ws_payload.log` (primeros JSON),
`cap_mitm.log` (conversación completa robot<->nube).
