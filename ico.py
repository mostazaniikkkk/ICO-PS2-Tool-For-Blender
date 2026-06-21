# -*- coding: utf-8 -*-
"""Codec del icono 3D ``.ico`` de PlayStation 2 (FR-14).

Pese a la extension, un ``.ico`` de PS2 **no es un icono de Windows**: es un
modelo 3D animado que el navegador de la consola renderiza girando. La animacion
es *vertex animation / morph targets* (no skinning: no hay huesos ni pesos).

Este modulo es **stdlib puro** y **autocontenido** (no importa otros modulos de
``ps2mc``) a proposito: es la fuente unica del codec y se **vendoriza** tal cual
en el add-on de Blender (``blender/io_import_ps2_ico/ico.py``), cuyo interprete
no puede ``pip install`` la libreria. Si editas el formato aqui, vuelve a copiar
este archivo al add-on.

Uso::

    import ps2mc
    model = ps2mc.ico.parse_file("icon0.ico")
    print(model.num_vertices, model.animation_shapes)
    for (x, y, z) in model.shapes[0]:   # forma base
        ...

Referencia de formato: ``docs/04-ico-3d-icon-format.md``. Las partes que la
documentacion marcaba como NO VERIFICADAS (layout de la animacion y el esquema
RLE de la textura) se reverseo y verifico contra los archivos reales de
``demo/`` antes de escribir este codec:

* Cabecera 20 B: ``magic, animation_shapes, texture_type, scale(f32), num_vertices``.
* Vertice: ``animation_shapes`` posiciones s16[4] + normal s16[4] + UV s16[2] +
  color u8[4]. Los s16 se dividen por 4096.0 (punto fijo).
* Animacion: cabecera de 20 B (id, frame_length, anim_speed f32, play_offset,
  frame_count) seguida de ``frame_count`` descriptores
  ``(shape_id u32, nb_keys u32, nb_keys * (time f32, value f32))``.
* Textura: ``u32 compressed_size`` + flujo RLE de u16. Por cada token ``rle``:
  si ``rle & 0x8000`` -> run **literal** de ``0x10000 - rle`` pixeles (cada uno
  un u16); si no -> run **repetido** del siguiente u16 ``rle`` veces. Se expande
  hasta 128*128 = 16384 pixeles. El bit ``0x08`` de ``texture_type`` indica RLE:
  15 y 14 lo tienen (comprimidas); 7 y 6 no -> **crudas** (32768 B de u16
  RGBA5551, verificado en ``ps2.ico`` tipo 7 y ``Crash.ico`` tipo 6). NOTA: la
  doc 04 describia mal el RLE como pares ``(count, color)`` sin prefijo y daba
  solo el 7 como crudo; ambas cosas son incorrectas.
"""

import struct

__all__ = ["IcoError", "Texture", "IcoModel", "parse", "parse_file",
           "build", "write_file",
           "FIXED_POINT", "TEX_W", "TEX_H", "TEX_PIXELS", "MAGIC"]

FIXED_POINT = 4096.0          # divisor de las coordenadas en punto fijo (s16)
TEX_W = 128                   # ancho de textura (fijo en PS2)
TEX_H = 128                   # alto de textura (fijo en PS2)
TEX_PIXELS = TEX_W * TEX_H    # 16384
MAGIC = 0x00010000

# Bit de 'texture_type' que indica compresion RLE de la textura (verificado):
#   15 (0b1111) y 14 (0b1110) -> RLE ;  7 (0b0111) y 6 (0b0110) -> sin comprimir.
RLE_FLAG = 0x08


class IcoError(Exception):
    """Error de parseo de un ``.ico`` (datos truncados o no es un icono PS2)."""


class Texture(object):
    """Textura 128x128 ya decodificada a RGBA float (0..1), orden raster top-down.

    ``rgba`` es una lista plana de ``TEX_PIXELS * 4`` floats, fila 0 = arriba.
    """

    __slots__ = ("width", "height", "rgba", "type", "compressed")

    def __init__(self, width, height, rgba, tex_type, compressed):
        self.width = width
        self.height = height
        self.rgba = rgba
        self.type = tex_type
        self.compressed = compressed


class IcoModel(object):
    """Modelo ``.ico`` parseado a estructuras Python planas.

    Atributos:
        magic, animation_shapes, texture_type, scale, num_vertices: cabecera.
        shapes:  lista de ``animation_shapes`` formas; cada forma es una lista
                 de ``num_vertices`` tuplas ``(x, y, z)`` ya en float (/4096).
        normals: lista de ``num_vertices`` tuplas ``(nx, ny, nz)`` (/4096).
        uvs:     lista de ``num_vertices`` tuplas ``(u, v)`` (/4096).
        colors:  lista de ``num_vertices`` tuplas ``(r, g, b, a)`` en 0..1.
        animation: dict con la cabecera de animacion y sus frames.
        texture:  :class:`Texture` o ``None`` si no se pudo decodificar.
        warnings: lista de avisos (no fatales) acumulados durante el parseo.
    """

    def __init__(self):
        self.magic = 0
        self.animation_shapes = 1
        self.texture_type = 0
        self.scale = 1.0
        self.num_vertices = 0
        self.shapes = []
        self.normals = []
        self.uvs = []
        self.colors = []
        self.animation = {}
        self.texture = None
        self.warnings = []

    # ---- propiedades derivadas ----
    @property
    def num_triangles(self):
        return self.num_vertices // 3

    @property
    def per_vertex_size(self):
        return self.animation_shapes * 8 + 8 + 4 + 4

    @property
    def is_animated(self):
        return self.animation_shapes > 1

    def __repr__(self):
        return ("IcoModel(vertices=%d, triangles=%d, shapes=%d, tex_type=%d)"
                % (self.num_vertices, self.num_triangles,
                   self.animation_shapes, self.texture_type))


# --------------------------------------------------------------------------- #
#  API publica                                                                #
# --------------------------------------------------------------------------- #
def parse(data, opaque_alpha=True):
    """Parsea los bytes de un ``.ico`` y devuelve un :class:`IcoModel`.

    ``opaque_alpha``: si es ``True`` (recomendado) la textura se decodifica con
    alfa=1 en todos los pixeles; el alfa de 1 bit del formato suele estar a 0 y
    tratarlo como transparencia haria invisible la textura. Si es ``False`` se
    respeta el bit de alfa original.

    Lanza :class:`IcoError` solo ante fallos fatales (cabecera invalida o bloque
    de vertices truncado). Los problemas de la animacion o la textura se degradan
    a avisos en ``model.warnings`` y la geometria se devuelve igual.
    """
    data = bytes(data)
    if len(data) < 20:
        raise IcoError("archivo demasiado corto para la cabecera (%d B)" % len(data))

    model = IcoModel()
    (model.magic, model.animation_shapes, model.texture_type,
     model.scale, model.num_vertices) = struct.unpack_from("<IIIfI", data, 0)

    if model.magic != MAGIC:
        raise IcoError(
            "magic 0x%08X != 0x%08X: no parece un icono 3D de PS2" % (model.magic, MAGIC))
    if model.animation_shapes < 1:
        raise IcoError("animation_shapes invalido (%d)" % model.animation_shapes)
    if model.num_vertices <= 0 or model.num_vertices % 3 != 0:
        model.warnings.append(
            "num_vertices=%d no es multiplo de 3 (lista de triangulos dudosa)"
            % model.num_vertices)

    vtx_end = _parse_vertices(data, model)
    anim_end = _parse_animation(data, vtx_end, model)
    model.texture = _parse_texture(data, anim_end, model, opaque_alpha)
    return model


def parse_file(path, opaque_alpha=True):
    with open(path, "rb") as f:
        return parse(f.read(), opaque_alpha)


# --------------------------------------------------------------------------- #
#  Vertices  (VERIFICADO contra archivos reales)                              #
# --------------------------------------------------------------------------- #
def _parse_vertices(data, model):
    per_vertex = model.per_vertex_size
    nshapes = model.animation_shapes
    nv = model.num_vertices
    need = 20 + nv * per_vertex
    if need > len(data):
        raise IcoError(
            "bloque de vertices truncado: requiere %d B, archivo de %d B"
            % (need, len(data)))

    shapes = [[None] * nv for _ in range(nshapes)]
    normals = [None] * nv
    uvs = [None] * nv
    colors = [None] * nv

    off = 20
    for vi in range(nv):
        for si in range(nshapes):
            x, y, z, _pad = struct.unpack_from("<4h", data, off)
            off += 8
            shapes[si][vi] = (x / FIXED_POINT, y / FIXED_POINT, z / FIXED_POINT)
        nx, ny, nz, _pad = struct.unpack_from("<4h", data, off)
        off += 8
        normals[vi] = (nx / FIXED_POINT, ny / FIXED_POINT, nz / FIXED_POINT)
        u, v = struct.unpack_from("<2h", data, off)
        off += 4
        uvs[vi] = (u / FIXED_POINT, v / FIXED_POINT)
        r, g, b, a = struct.unpack_from("<4B", data, off)
        off += 4
        colors[vi] = (r / 255.0, g / 255.0, b / 255.0, a / 255.0)

    model.shapes = shapes
    model.normals = normals
    model.uvs = uvs
    model.colors = colors
    return off


# --------------------------------------------------------------------------- #
#  Animacion  (reverseado y verificado: anim_shapes 1 y 4)                     #
# --------------------------------------------------------------------------- #
def _parse_animation(data, off, model):
    """Parsea la seccion de animacion. Devuelve el offset donde empieza la textura.

    Es best-effort: ante datos inesperados acumula un aviso y devuelve el mejor
    offset estimado para la textura.
    """
    if off + 20 > len(data):
        model.warnings.append("sin seccion de animacion (archivo corto)")
        return off

    id_tag, frame_length, anim_speed, play_offset, frame_count = \
        struct.unpack_from("<IIfII", data, off)
    cur = off + 20

    frames = []
    ok = True
    if frame_count > 1024:
        model.warnings.append("frame_count=%d sospechoso; animacion ignorada" % frame_count)
        frame_count = 0
        ok = False

    for _ in range(frame_count):
        if cur + 8 > len(data):
            ok = False
            break
        shape_id, nb_keys = struct.unpack_from("<II", data, cur)
        cur += 8
        if nb_keys > 4096 or cur + nb_keys * 8 > len(data):
            ok = False
            break
        keys = []
        for _k in range(nb_keys):
            t, val = struct.unpack_from("<ff", data, cur)
            cur += 8
            keys.append((t, val))
        frames.append({"shape_id": shape_id, "keys": keys})

    model.animation = {
        "id_tag": id_tag,
        "frame_length": frame_length,
        "anim_speed": anim_speed,
        "play_offset": play_offset,
        "frame_count": frame_count,
        "frames": frames,
    }
    if not ok:
        model.warnings.append(
            "seccion de animacion incompleta o inesperada (offset de textura estimado)")
    return cur


# --------------------------------------------------------------------------- #
#  Textura  (RLE reverseado y verificado contra archivos reales)              #
# --------------------------------------------------------------------------- #
def _rgba5551(c, opaque_alpha):
    """Convierte un u16 RGBA5551 (R en bits bajos) a tupla de 4 floats 0..1."""
    r = (c & 0x1F) / 31.0
    g = ((c >> 5) & 0x1F) / 31.0
    b = ((c >> 10) & 0x1F) / 31.0
    a = 1.0 if opaque_alpha else float((c >> 15) & 1)
    return (r, g, b, a)


def _find_compressed_texture_offset(data, anim_end):
    """Localiza de forma robusta el ``u32 compressed_size`` de la textura.

    La textura comprimida termina exactamente en EOF y va prefijada por su
    tamano: ``data[t:t+4] + (t+4) == len(data)``. Esa firma es muy fuerte, asi
    que se prefiere a confiar ciegamente en el offset que sale de la animacion
    (cuyo layout es el menos verificado). Devuelve el offset, o ``None``.
    """
    eof = len(data)
    if 0 <= anim_end <= eof - 4:
        size = struct.unpack_from("<I", data, anim_end)[0]
        if anim_end + 4 + size == eof:
            return anim_end
    start = max(0, (anim_end - 64) & ~3)
    for t in range(start, eof - 3, 4):
        size = struct.unpack_from("<I", data, t)[0]
        if 0 < size < eof and t + 4 + size == eof:
            return t
    return None


def _decode_rle(data, off, end, opaque_alpha):
    """Descomprime el flujo RLE de la textura a una lista de floats RGBA.

    Esquema (verificado): token ``rle`` u16. Si ``rle & 0x8000`` -> run literal
    de ``0x10000 - rle`` pixeles (cada uno un u16). Si no -> ``rle`` copias del
    siguiente u16. Para hasta ``TEX_PIXELS`` pixeles.
    """
    rgba = []
    p = off
    count = 0
    while p + 2 <= end and count < TEX_PIXELS:
        rle = struct.unpack_from("<H", data, p)[0]
        p += 2
        if rle & 0x8000:                       # run literal
            n = 0x10000 - rle
            for _ in range(n):
                if p + 2 > end or count >= TEX_PIXELS:
                    break
                rgba.extend(_rgba5551(struct.unpack_from("<H", data, p)[0], opaque_alpha))
                p += 2
                count += 1
        else:                                  # run repetido
            if p + 2 > end:
                break
            color = struct.unpack_from("<H", data, p)[0]
            p += 2
            px = _rgba5551(color, opaque_alpha)
            n = min(rle, TEX_PIXELS - count)
            rgba.extend(px * n)
            count += n
    return rgba, count


def _texture_raw(data, anim_end, model, opaque_alpha):
    """Textura sin comprimir: 128*128 u16 RGBA5551 = 32768 B. Devuelve Texture/None.

    ``texture_type`` 6 y 7 son crudos (verificado: ``Crash.ico`` tipo 6 deja
    exactamente 32768 B entre la animacion y EOF, y ``ps2.ico`` tipo 7 igual).
    """
    eof = len(data)
    for base in (anim_end, anim_end + 4):       # con o sin un u32 de relleno delante
        if base >= 0 and base + TEX_PIXELS * 2 <= eof:
            shorts = struct.unpack_from("<%dH" % TEX_PIXELS, data, base)
            rgba = []
            for c in shorts:
                rgba.extend(_rgba5551(c, opaque_alpha))
            return Texture(TEX_W, TEX_H, rgba, model.texture_type, False)
    return None


def _texture_rle(data, anim_end, model, opaque_alpha):
    """Textura RLE (``texture_type`` con bit ``0x08``: 14, 15). Devuelve Texture/None.

    Como la seccion de animacion es la parte menos verificada del formato, el
    offset exacto puede variar; se prueban varios candidatos y se acepta el
    primero que descomprima **exactamente** 128*128 pixeles.
    """
    eof = len(data)
    attempts = []
    if anim_end + 4 <= eof:                      # A) prefijo u32 justo tras animacion
        sz = struct.unpack_from("<I", data, anim_end)[0]
        if 0 < sz <= eof:
            attempts.append((anim_end + 4, anim_end + 4 + sz))
    sig = _find_compressed_texture_offset(data, anim_end)   # B) prefijo anclado a EOF
    if sig is not None:
        sz = struct.unpack_from("<I", data, sig)[0]
        attempts.append((sig + 4, sig + 4 + sz))
    attempts.append((anim_end, eof))            # C) sin prefijo, hasta EOF
    if anim_end + 4 <= eof:                      # D) sin prefijo, saltando un u32
        attempts.append((anim_end + 4, eof))

    seen = set()
    for off, end in attempts:
        if (off, end) in seen:
            continue
        seen.add((off, end))
        end = min(end, eof)
        if off < 0 or off >= end:
            continue
        rgba, count = _decode_rle(data, off, end, opaque_alpha)
        if count == TEX_PIXELS:
            return Texture(TEX_W, TEX_H, rgba, model.texture_type, True)
    return None


def _parse_texture(data, anim_end, model, opaque_alpha=True):
    """Decodifica la textura. Devuelve :class:`Texture` o ``None`` (con aviso).

    El bit ``0x08`` de ``texture_type`` indica compresion RLE (15 y 14 lo tienen;
    7 y 6 no -> crudos). Se intenta primero el metodo que sugiere ese bit y, por
    robustez ante tipos no vistos, el otro como reserva.
    """
    rle_first = bool(model.texture_type & RLE_FLAG)
    decoders = (_texture_rle, _texture_raw) if rle_first else (_texture_raw, _texture_rle)
    for decode in decoders:
        tex = decode(data, anim_end, model, opaque_alpha)
        if tex is not None:
            return tex
    model.warnings.append(
        "no se pudo decodificar la textura (tipo=%d fuera de lo verificado); "
        "geometria sin textura" % model.texture_type)
    return None


# =========================================================================== #
#  ENCODER  (IcoModel -> bytes .ico)                                          #
# =========================================================================== #
#
# El encoder es la inversa exacta del parser y esta validado por round-trip
# (parse -> build -> parse) contra todos los `.ico` reales de `demo/`. La
# cuantizacion no pierde informacion: cada float vino de ``entero/divisor``, asi
# que ``round(float*divisor)`` recupera el entero original.

def _f2s(v):
    """float -> s16 en punto fijo (*4096), con saturacion al rango s16."""
    n = int(round(v * FIXED_POINT))
    if n < -32768:
        return -32768
    if n > 32767:
        return 32767
    return n


def _c8(v):
    """float 0..1 -> u8 0..255 con saturacion."""
    n = int(round(v * 255.0))
    return 0 if n < 0 else 255 if n > 255 else n


def _c5(v):
    """float 0..1 -> entero 0..31 (5 bits) con saturacion."""
    n = int(round(v * 31.0))
    return 0 if n < 0 else 31 if n > 31 else n


def _floats_to_5551(rgba):
    """Lista plana de floats RGBA (top-down) -> lista de u16 RGBA5551 (R en bits bajos)."""
    shorts = []
    for i in range(0, len(rgba) - 3, 4):
        r = _c5(rgba[i]); g = _c5(rgba[i + 1]); b = _c5(rgba[i + 2])
        a = 1 if rgba[i + 3] >= 0.5 else 0
        shorts.append(r | (g << 5) | (b << 10) | (a << 15))
    return shorts


def _encode_texture_raw(shorts):
    """128*128 u16 RGBA5551 sin comprimir = 32768 B (formato de texture_type 6/7)."""
    return struct.pack("<%dH" % TEX_PIXELS, *shorts)


def _encode_texture_rle(shorts):
    """Comprime con RLE de runs repetidos (la inversa del decoder).

    Emite tokens ``(u16 count, u16 color)`` con ``count`` en 1..0x7FFF (bit alto
    siempre 0 -> el decoder los lee como run repetido). No usa runs literales:
    es algo menos compacto pero round-trip exacto y trivial de verificar. Va
    prefijado por ``u32`` con el tamano del flujo (la firma que busca el parser).
    """
    body = bytearray()
    n = len(shorts)
    i = 0
    while i < n:
        j = i + 1
        while j < n and shorts[j] == shorts[i] and (j - i) < 0x7FFF:
            j += 1
        body += struct.pack("<HH", j - i, shorts[i])
        i = j
    return struct.pack("<I", len(body)) + bytes(body)


def _build_animation(model):
    """Serializa la seccion de animacion.

    Si ``model.animation`` trae frames coherentes (p. ej. de un archivo parseado)
    los reescribe **byte a byte**; si no, sintetiza una seccion minima valida
    (la que tienen los iconos estaticos reales: 1 frame, 1 key).
    """
    anim = model.animation or {}
    frames = anim.get("frames")
    frame_count = anim.get("frame_count")
    coherent = (isinstance(frames, list) and frame_count is not None
                and len(frames) == frame_count)

    if not coherent:
        # sintetizar: cabecera + una entrada por forma (best-effort para animados)
        n = model.animation_shapes
        out = struct.pack("<IIfII", 1, max(1, n), 1.0, 0, n)
        for i in range(n):
            out += struct.pack("<II", i, 1) + struct.pack("<ff", 1.0, 1.0)
        return out

    out = struct.pack("<IIfII",
                      int(anim.get("id_tag", 1)),
                      int(anim.get("frame_length", 1)),
                      float(anim.get("anim_speed", 1.0)),
                      int(anim.get("play_offset", 0)),
                      int(frame_count))
    for fr in frames:
        keys = fr.get("keys", [])
        out += struct.pack("<II", int(fr.get("shape_id", 0)), len(keys))
        for (t, val) in keys:
            out += struct.pack("<ff", float(t), float(val))
    return out


def _build_texture(model):
    tex = model.texture
    if tex is None:
        return _encode_texture_raw([0] * TEX_PIXELS)
    shorts = _floats_to_5551(tex.rgba)
    if len(shorts) != TEX_PIXELS:                 # normalizar a 128*128
        shorts = (shorts + [0] * TEX_PIXELS)[:TEX_PIXELS]
    if model.texture_type & RLE_FLAG:
        return _encode_texture_rle(shorts)
    return _encode_texture_raw(shorts)


def build(model):
    """Serializa un :class:`IcoModel` a los bytes de un ``.ico``. Inversa de :func:`parse`.

    Lanza :class:`IcoError` si el modelo es incoherente (longitudes que no cuadran).
    """
    nshapes = model.animation_shapes
    nv = model.num_vertices
    if nshapes < 1:
        raise IcoError("animation_shapes invalido (%d)" % nshapes)
    if nv < 0:
        raise IcoError("num_vertices invalido (%d)" % nv)
    if len(model.shapes) != nshapes:
        raise IcoError("hay %d formas, animation_shapes=%d" % (len(model.shapes), nshapes))
    for si, shape in enumerate(model.shapes):
        if len(shape) != nv:
            raise IcoError("la forma %d tiene %d vertices, esperaba %d"
                           % (si, len(shape), nv))
    if not (len(model.normals) == len(model.uvs) == len(model.colors) == nv):
        raise IcoError("normals/uvs/colors deben tener %d entradas" % nv)

    out = bytearray()
    out += struct.pack("<IIIfI", MAGIC, nshapes, model.texture_type,
                       float(model.scale), nv)
    for vi in range(nv):
        for si in range(nshapes):
            x, y, z = model.shapes[si][vi]
            out += struct.pack("<4h", _f2s(x), _f2s(y), _f2s(z), 0)
        nx, ny, nz = model.normals[vi]
        out += struct.pack("<4h", _f2s(nx), _f2s(ny), _f2s(nz), 0)
        u, v = model.uvs[vi]
        out += struct.pack("<2h", _f2s(u), _f2s(v))
        r, g, b, a = model.colors[vi]
        out += struct.pack("<4B", _c8(r), _c8(g), _c8(b), _c8(a))

    out += _build_animation(model)
    out += _build_texture(model)
    return bytes(out)


def write_file(model, path):
    """Escribe ``model`` como ``.ico`` en ``path``. Devuelve el numero de bytes."""
    data = build(model)
    with open(path, "wb") as f:
        f.write(data)
    return len(data)
