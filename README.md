# Conga 8090 Ultra → Home Assistant (control local)

Proyecto de ingeniería inversa para controlar el robot aspirador **Cecotec Conga
8090 Ultra** en local desde Home Assistant, sin depender de la nube de Cecotec.

El 8090 **no es compatible con Congatudo/Valetudo** (usan protocolo binario de los
modelos viejos). Este robot usa una pila distinta: **TLS 1.2 → WebSocket → JSON**,
con el mapa en **Protobuf comprimido con zlib**. Todo esto se reconstruyó desde
cero capturando el tráfico.

## ¿Qué repo es este? (hay dos)

Este proyecto tiene dos repositorios complementarios:

- **Este repo (documentación + protocolo):** la especificación completa del
  protocolo, las herramientas de captura (MITM), el decodificador de mapa y los
  servidores de cada fase. Para **entender cómo funciona por dentro**, adaptar
  otro modelo, o contribuir.
- **Add-on de Home Assistant (listo para usar):**
  [github.com/miguelsg29/conga-8090-home-assistant](https://github.com/miguelsg29/conga-8090-home-assistant)
  — empaqueta el puente como **add-on nativo de Home Assistant OS**, con formulario
  de configuración visual, para que funcione **sin depender de un PC**. Si solo
  quieres instalarlo y usarlo en HA, ve directo al add-on.

Ambos comparten el mismo puente (`conga_mqtt_bridge.py`); este repo lo explica y
documenta, el add-on lo empaqueta para instalación en un clic.

## Estado del proyecto: FUNCIONAL ✅

- ✅ Control local total: limpiar, pausar, reanudar, parar, volver a base
- ✅ Movimiento manual (adelante/atrás/izquierda/derecha)
- ✅ Estado en tiempo real: base / limpiando / pausado / volviendo / inactivo
- ✅ Nivel de batería
- ✅ Entidad `vacuum` nativa en Home Assistant (vía MQTT autodiscovery)
- ✅ Mapa decodificado con las 7 habitaciones etiquetadas
- ⬜ (opcional) Mapa como cámara en HA, limpieza por habitaciones, servicio permanente

## Ficheros del proyecto

| Fichero | Qué es |
|---|---|
| `Conga8090_Protocolo.md` | **Especificación completa** del protocolo (transporte, JSON, comandos, estados, mapa). El documento de referencia. |
| `conga_mqtt_bridge.py` | **El puente final.** Conecta el robot con Home Assistant vía MQTT. Es lo que se ejecuta en producción. |
| `GUIA_MQTT_HomeAssistant.md` | Guía de instalación y uso del puente MQTT. |
| `decodificar_mapa.py` | Decodificador del mapa (zlib+Protobuf → PNG + habitaciones). |
| `mapa_ejemplo.png` | Render de EJEMPLO (casa ficticia) que muestra qué produce el decodificador. |
| `servidor_conga.py` | Servidor mínimo (Fase 1). Solo mantiene la conexión. Histórico. |
| `servidor_conga_control.py` | Servidor con control por teclado (Fase 2). Útil para pruebas/experimentar con comandos crudos. |
| `mitm_dirigido.py` | Proxy MITM contra la nube real, para capturar comandos nuevos desde la app oficial. Herramienta de descubrimiento. |
| `test_jwt_sintetico.py` | Prueba si el robot acepta un JWT sintético (sin caducidad). Si funciona, elimina la dependencia del token capturado. |

## Puesta en marcha rápida (el puente)

Requisitos: Python 3, `paho-mqtt`, certificados `cert.pem`/`key.pem`
autofirmados, y un broker MQTT (Mosquitto de HA).

1. **Certificados** (una vez), con OpenSSL:
   ```
   openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem \
     -days 3650 -nodes -subj "/CN=tcp-cecotec.3irobotix.net"
   ```
2. **DNS rewrite** en AdGuard/tu DNS: `tcp-cecotec.3irobotix.net` → IP de la
   máquina que corre el puente.
3. **Firewall**: abre el puerto 9090 en esa máquina.
4. **Config**: edita `MQTT_HOST/USER/PASS` en `conga_mqtt_bridge.py`.
5. **Ejecuta**: `pip install paho-mqtt` y luego `python conga_mqtt_bridge.py`.
6. **Reinicia el robot** (corte de energía real). Aparecerá solo en HA como
   dispositivo "Conga 8090".

Detalle completo en `GUIA_MQTT_HomeAssistant.md`.

## Datos de tu robot (cómo obtenerlos)

Cada robot tiene identificadores propios. **Los tuyos** salen del login que captura
el MITM (ver `Conga8090_Protocolo.md` seccion 4.1 y 6). Van en tu fichero `.env`
(privado). El codigo trae valores de EJEMPLO que debes sustituir:

- `ROBOT_DID` (device id) — de `result.id` del login
- `ROBOT_USERID` (app) — de `BIND_LIST` del login
- `ROBOT_SN` — de `sn` del login
- `ROBOT_MAC` — de `mac` del login
- `AUTH_JWT` — de `result.data.AUTH` del login

IP nube real: `nslookup tcp-cecotec.3irobotix.net 8.8.8.8` (dato publico de Cecotec).

## Configuración: cómo rellenar tu `.env`

El proyecto se configura con un fichero `.env` (privado, **no se sube a git**).
Copia `.env.example` a `.env` y rellénalo. Aquí va de dónde sale cada dato:

### Credenciales MQTT (lo mínimo para empezar)

- `MQTT_HOST`: la IP de tu Home Assistant / broker Mosquitto (ej. `192.168.1.10`).
- `MQTT_PORT`: normalmente `1883`.
- `MQTT_USER` / `MQTT_PASS`: usuario y contraseña de tu broker. Si usas el add-on
  Mosquitto de HA y no tienes uno, créalo en Ajustes → Personas → Usuarios, o
  configúralo en las opciones del add-on Mosquitto.

### Datos de tu robot (se obtienen capturando el login)

Estos identifican a TU Conga. Se sacan capturando el mensaje de login del robot
con la herramienta MITM (ver `Conga8090_Protocolo.md`, secciones 4.1 y 8):

1. Configura el DNS para redirigir `tcp-cecotec.3irobotix.net` a tu PC.
2. Genera los certificados (ver abajo) y ejecuta el MITM.
3. En el fichero `cap_mitm_full.log`, busca `auth/login` y saca:
   - `ROBOT_DID` ← campo `id` de la respuesta.
   - `ROBOT_USERID` ← número dentro de `BIND_LIST` de la respuesta.
   - `ROBOT_SN` ← campo `sn` de la petición.
   - `ROBOT_MAC` ← campo `mac` de la petición.
   - `AUTH_JWT` ← **ya no hace falta** (el puente genera uno sintético). Déjalo
     vacío en el `.env`.

Si no pones estos datos, el script usa valores de ejemplo (que NO funcionarán con
tu robot real, solo evitan que el código lleve datos personales).

### Sobre el `AUTH_JWT`: ya no hace falta capturarlo

**Confirmado experimentalmente:** el robot **no valida el JWT** (acepta el login con
`code:0` y una respuesta bien formada, sin comprobar firma ni caducidad). Por eso el
puente **genera un JWT sintético sin caducidad automáticamente**.

Recomendación: deja `AUTH_JWT=` **vacío** en tu `.env`. El puente generará el token
solo, y no tendrás que capturarlo ni renovarlo nunca. Al arrancar, el log muestra
`[JWT] modo: SINTÉTICO (generado, sin caducidad)`.

Si prefieres usar el token capturado de tu robot, ponlo en `AUTH_JWT=<token>`. Y
`USE_SYNTHETIC_JWT=on` fuerza el sintético aunque haya un token configurado.

Puedes verificar que tu unidad acepta el sintético con `test_jwt_sintetico.py`
(ver tabla de ficheros).

### Certificados TLS (una vez)

```bash
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem \
  -days 3650 -nodes -subj "/CN=tcp-cecotec.3irobotix.net"
```

Deja `cert.pem` y `key.pem` junto a los scripts. (Tampoco se suben a git.)

### Ajustes de limpieza (opcionales)

`DEFAULT_FAN`, `DEFAULT_WATER`, `DEFAULT_MOP`, `DEFAULT_TWICE` definen la
configuración por defecto para la limpieza por habitación. Todos tienen un valor
sensato por defecto.

## Subir a GitHub (paso a paso, sin experiencia previa)

**IMPORTANTE antes de nada:** el `.gitignore` ya está preparado para que NUNCA se
suban tu `.env`, los certificados (`.pem`), las capturas (`cap_*.log`) ni el mapa
real. Aun así, revisa una vez que tu `.env` real no aparece en la lista antes de
publicar.

## Resumen técnico (para quien continúe)

- **Transporte**: el robot conecta al puerto **9090** con **TLS 1.2** (acepta cert
  autofirmado, sin pinning), hace **handshake WebSocket**, y habla **JSON**.
- **Login**: no valida el JWT; basta `code:0` + respuesta bien formada.
- **Comandos**: van como `{"tag":"sweeper-transmit/to_bind","content":"{...control...}"}`.
  Los principales: `set_mode` (type/value) y `set_direct` (direction).
- **set_mode**: 0/1=limpiar, 2/2=pausar, 2/1=reanudar, 3/1=ir a base, 3/0=cancelar base.
- **Estados** (workMode): 0+charge=base, 0=parado, 5=volviendo, 36=limpiando,
  37=pausado, 2=manual.
- **Mapa**: mensaje `syn_no_cache`, binario con cabecera + zlib (firma 78 9c) +
  Protobuf. Rejilla 800x800 (0=desconocido, 255=pared, 10-16=habitaciones).

## Contribución a la comunidad

Este trabajo documenta que la **serie Conga 8000/Ultra** usa TLS+WebSocket+JSON+
Protobuf, algo no documentado en el proyecto Congatudo/agnoc. La especificación
(`Conga8090_Protocolo.md`) sería una buena base para el repo de 
`github.com/congatudo/agnoc` y que abra soporte para esta generación de robots.
