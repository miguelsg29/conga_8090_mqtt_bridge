#!/usr/bin/env python3
"""
Decodificador del mapa del Conga 8090.

El robot envia el mapa como mensaje del servicio 'sweeper-map/robot/syn_no_cache'.
El payload NO es JSON: es un frame binario con esta estructura:

  [cabecera binaria][ "POST" ][ ruta del servicio ][ mas cabecera ][ ZLIB DATA ]

Los datos del mapa van comprimidos con zlib (firma 78 9c) y, una vez
descomprimidos, son un mensaje Protobuf con estos campos de nivel superior:

  campo 1  (varint)        : tipo/version (=3)
  campo 2  (message)       : metadatos { id, timestamp, resolucion(float)=90.0 ... }
  campo 3  (message)       : parametros del mapa (origen, escala, min/max...)
  campo 4  (bytes)         : REJILLA 800x800 = 640000 celdas, 1 byte por celda:
                               0   = desconocido (sin explorar)
                               1   = ? (borde/pared fina)
                               255 = pared / obstaculo
                               10-16 = celdas de cada HABITACION (por id)
  campo 5  (message)       : nombre del mapa ("Interior")
  campo 7  (message)       : pose/posicion del robot (floats)
  campo 9  (message, rep)  : zonas / paredes virtuales (pares de coords)
  campo 12 (message, rep)  : HABITACIONES { id(1), nombre(2), ... }
  campo 13 (bytes)         : tabla de vecindad/orden de habitaciones
  campo 14 (message, rep)  : celdas por habitacion (RLE de spans)
  campo 17 (message)       : jerarquia casa>mapa ("Casa" > "Interior")

Habitaciones de ESTE mapa (pueden cambiar si se re-mapea):
  10 Dormitorio | 11 Bano privado | 12 Bano | 13 Cocina
  14 Dormitorio Principal | 15 Salon | 16 Pasillo

Uso:
  python decodificar_mapa.py <fichero_con_frame_binario_o_hex>
  # o importar decode_map_payload(bytes) y render_map(...)
"""

import sys, zlib, struct
from collections import Counter

GRID_W = 800
GRID_H = 800


def read_varint(data, pos):
    result = 0
    shift = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7f) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def extract_zlib(frame):
    """Encuentra la firma zlib en el frame y descomprime."""
    pos = frame.find(b"\x78\x9c")
    if pos < 0:
        raise ValueError("No se encontro firma zlib (78 9c) en el frame")
    return zlib.decompress(frame[pos:])


def parse_protobuf_map(pb):
    """Devuelve dict con grid (bytes), rooms [(id,name)], y metadatos."""
    pos = 0
    grid = None
    rooms = []
    map_name = None
    while pos < len(pb):
        try:
            tag, pos = read_varint(pb, pos)
        except IndexError:
            break
        fn = tag >> 3
        wt = tag & 0x7
        if wt == 0:
            _, pos = read_varint(pb, pos)
        elif wt == 1:
            pos += 8
        elif wt == 5:
            pos += 4
        elif wt == 2:
            length, pos = read_varint(pb, pos)
            chunk = pb[pos:pos + length]
            pos += length
            if fn == 4:
                # campo 4: subcampo 1 = rejilla
                p = 0
                st, p = read_varint(chunk, p)
                glen, p = read_varint(chunk, p)
                grid = chunk[p:p + glen]
            elif fn == 12:
                rid = None
                name = None
                p = 0
                while p < len(chunk):
                    st, p = read_varint(chunk, p)
                    sf = st >> 3
                    sw = st & 7
                    if sw == 0:
                        v, p = read_varint(chunk, p)
                        if sf == 1:
                            rid = v
                    elif sw == 2:
                        sl, p = read_varint(chunk, p)
                        if sf == 2:
                            name = chunk[p:p + sl].decode("utf-8", "replace")
                        p += sl
                    elif sw == 5:
                        p += 4
                    elif sw == 1:
                        p += 8
                    else:
                        break
                rooms.append((rid, name))
            elif fn == 5 and map_name is None:
                # nombre del mapa esta como string dentro
                p = 0
                while p < len(chunk):
                    st, p = read_varint(chunk, p)
                    sf = st >> 3
                    sw = st & 7
                    if sw == 2:
                        sl, p = read_varint(chunk, p)
                        try:
                            map_name = chunk[p:p + sl].decode("utf-8")
                        except Exception:
                            pass
                        p += sl
                    elif sw == 0:
                        _, p = read_varint(chunk, p)
                    else:
                        break
        else:
            break
    return {"grid": grid, "rooms": rooms, "map_name": map_name}


def decode_map_payload(frame):
    """De frame binario crudo -> dict con grid, rooms, map_name."""
    pb = extract_zlib(frame)
    return parse_protobuf_map(pb)


def render_map(info, out_path="mapa_conga.png", scale=3):
    from PIL import Image
    grid = info["grid"]
    if not grid or len(grid) < GRID_W * GRID_H:
        raise ValueError("Rejilla incompleta")
    img = Image.new("RGB", (GRID_W, GRID_H), (30, 30, 40))
    px = img.load()
    room_colors = {
        10: (120, 180, 255), 11: (255, 180, 120), 12: (150, 255, 150),
        13: (255, 150, 200), 14: (255, 240, 130), 15: (180, 150, 255),
        16: (150, 230, 230), 1: (90, 90, 110),
    }
    xs = []
    ys = []
    for y in range(GRID_H):
        row = y * GRID_W
        for x in range(GRID_W):
            v = grid[row + x]
            if v == 0:
                continue
            xs.append(x)
            ys.append(y)
            if v == 255:
                px[x, y] = (240, 240, 240)
            else:
                px[x, y] = room_colors.get(v, (200, 80, 80))
    if xs:
        pad = 20
        crop = img.crop((max(0, min(xs) - pad), max(0, min(ys) - pad),
                         min(GRID_W, max(xs) + pad), min(GRID_H, max(ys) + pad)))
        crop = crop.resize((crop.width * scale, crop.height * scale), Image.NEAREST)
        crop.save(out_path)
    return out_path


def main():
    if len(sys.argv) < 2:
        print("uso: python decodificar_mapa.py <fichero>")
        print("  el fichero puede ser binario crudo o hex (con espacios)")
        return
    raw = open(sys.argv[1], "rb").read()
    # Detectar si es hex-texto o binario
    try:
        txt = raw.decode("ascii").strip()
        if all(c in "0123456789abcdefABCDEF \n" for c in txt[:200]):
            frame = bytes.fromhex(txt.replace(" ", "").replace("\n", ""))
        else:
            frame = raw
    except Exception:
        frame = raw

    info = decode_map_payload(frame)
    print(f"Mapa: {info['map_name']}")
    print(f"Rejilla: {len(info['grid'])} celdas ({GRID_W}x{GRID_H})")
    c = Counter(info["grid"])
    print(f"Valores: {dict(sorted(c.items()))}")
    print("Habitaciones:")
    for rid, name in info["rooms"]:
        print(f"  {rid}: {name}")
    out = render_map(info)
    print(f"Imagen: {out}")


if __name__ == "__main__":
    main()
