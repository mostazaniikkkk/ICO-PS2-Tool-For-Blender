# -*- coding: utf-8 -*-
"""Importador de iconos 3D ``.ico`` de PlayStation 2 para Blender.

Anade  File > Import > PS2 Icon (.ico)  y construye una malla a partir del
modelo: triangulos planos, normales/UVs/colores por vertice, shape keys para los
morph targets (animacion por vertices) y un material con la textura decodificada.

El parseo binario vive en :mod:`ico` (copia vendorizada de ``ps2mc/ico.py``, sin
dependencia de Blender). Este
modulo solo traduce ese resultado a datos de Blender.

Compatibilidad: probado el parser contra archivos reales; la construccion en
Blender usa APIs disponibles en 3.x y 4.x (con guardas para los cambios de 4.1
en normales personalizadas y atributos de color).
"""

bl_info = {
    "name": "PS2 Icon (.ico) importer",
    "author": "mostazaniikkkk",
    "version": (1, 0, 0),
    "blender": (2, 93, 0),
    "location": "File > Import/Export > PS2 Icon (.ico)",
    "description": "Importa y exporta modelos 3D de iconos de PlayStation 2 (.ico): "
                   "malla, colores de vertice, UVs, textura y morph targets (shape keys).",
    "warning": "La seccion de animacion y algunos tipos de textura del formato "
               ".ico estan solo parcialmente verificados.",
    "category": "Import-Export",
}

import os

import bpy
from bpy.props import BoolProperty, FloatProperty, StringProperty
from bpy.types import Operator
from bpy_extras.io_utils import ExportHelper, ImportHelper

# Import del parser tanto si el add-on esta instalado como paquete ("from .")
# como si se ejecuta el archivo suelto dentro de Blender (sin paquete).
try:
    from . import ico
except ImportError:  # ejecucion como script suelto
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ico


# --------------------------------------------------------------------------- #
#  Construccion de la malla en Blender                                        #
# --------------------------------------------------------------------------- #
def _axis_convert(co, convert, scale):
    """PS2 usa **Y hacia abajo**; Blender usa Z arriba. Rota -90 sobre X si
    ``convert`` (deja el icono derecho, no boca abajo). Es una rotacion propia
    (det +1): no invierte normales ni el sentido de las caras."""
    x, y, z = co
    if convert:
        return (x * scale, z * scale, -y * scale)
    return (x * scale, y * scale, z * scale)


def _build_mesh(icon, name, opts):
    """Crea y devuelve el objeto malla a partir de un :class:`ico.IcoModel`."""
    nv = icon.num_vertices
    base_shape = icon.shapes[0]

    verts = [_axis_convert(co, opts["convert_axes"], opts["scale"]) for co in base_shape]
    faces = [(i, i + 1, i + 2) for i in range(0, nv - nv % 3, 3)]

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update(calc_edges=True)

    if opts["import_uvs"]:
        _apply_uvs(mesh, icon, opts["flip_v"])
    if opts["import_colors"]:
        _apply_colors(mesh, icon)

    mesh.validate(clean_customdata=False)

    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)

    # Normales personalizadas: cada vertice es unico (lista plana), asi que una
    # normal por vertice equivale a una por loop.
    if opts["import_normals"]:
        _apply_normals(mesh, icon, opts["convert_axes"])

    return obj


def _apply_uvs(mesh, icon, flip_v):
    uv_layer = mesh.uv_layers.new(name="UVMap")
    uvs = icon.uvs
    data = uv_layer.data
    for loop in mesh.loops:
        u, v = uvs[loop.vertex_index]
        data[loop.index].uv = (u, 1.0 - v if flip_v else v)


def _apply_colors(mesh, icon):
    colors = icon.colors
    # Blender 3.2+: color_attributes. Anterior: vertex_colors.
    if hasattr(mesh, "color_attributes"):
        try:
            attr = mesh.color_attributes.new(name="Color", type="BYTE_COLOR",
                                             domain="CORNER")
        except Exception:  # noqa: BLE001
            attr = mesh.color_attributes.new(name="Color", type="FLOAT_COLOR",
                                             domain="CORNER")
        data = attr.data
        for loop in mesh.loops:
            data[loop.index].color = colors[loop.vertex_index]
    elif hasattr(mesh, "vertex_colors"):
        vc = mesh.vertex_colors.new(name="Color")
        data = vc.data
        for loop in mesh.loops:
            data[loop.index].color = colors[loop.vertex_index]


def _apply_normals(mesh, icon, convert):
    normals = [_axis_convert(n, convert, 1.0) for n in icon.normals]
    # Pre-4.1 requiere auto_smooth para que las normales personalizadas surtan efecto.
    if hasattr(mesh, "use_auto_smooth"):
        mesh.use_auto_smooth = True
    try:
        mesh.normals_split_custom_set_from_vertices(normals)
    except Exception:  # noqa: BLE001  (API no disponible / malla degenerada)
        pass


def _add_shape_keys(obj, icon, opts):
    """Anade los morph targets como shape keys (Basis = forma 0)."""
    if icon.animation_shapes < 2:
        return None
    obj.shape_key_add(name="Basis", from_mix=False)
    keyblocks = []
    for si in range(1, icon.animation_shapes):
        kb = obj.shape_key_add(name="shape_%d" % si, from_mix=False)
        shape = icon.shapes[si]
        for vi, co in enumerate(shape):
            kb.data[vi].co = _axis_convert(co, opts["convert_axes"], opts["scale"])
        keyblocks.append(kb)
    return keyblocks


def _animate_shape_keys(obj, icon, keyblocks):
    """Crea una animacion ciclica aproximada entre las shape keys.

    El layout exacto de tiempos en el .ico no esta verificado; esto reproduce el
    "latido" del icono recorriendo las formas de manera uniforme. Editable luego
    en el editor de acciones de Blender.
    """
    if not keyblocks:
        return
    anim = icon.animation or {}
    frame_length = int(anim.get("frame_length") or 0)
    n_shapes = icon.animation_shapes
    if frame_length < n_shapes:
        frame_length = max(n_shapes * 8, 16)   # ritmo por defecto razonable

    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = frame_length

    seg = frame_length / float(n_shapes)
    # forma activa i en frame round(i*seg); las demas a 0. Loop suave.
    all_blocks = keyblocks  # shape_1..shape_{n-1}; Basis = forma 0 implicita
    for active in range(n_shapes):
        frame = 1 + int(round(active * seg))
        for idx, kb in enumerate(all_blocks, start=1):
            kb.value = 1.0 if idx == active else 0.0
            kb.keyframe_insert(data_path="value", frame=frame)
    # cerrar el ciclo volviendo a la forma 0
    for kb in all_blocks:
        kb.value = 0.0
        kb.keyframe_insert(data_path="value", frame=frame_length + 1)


# --------------------------------------------------------------------------- #
#  Textura y material                                                          #
# --------------------------------------------------------------------------- #
def _build_image(icon, name):
    tex = icon.texture
    if tex is None:
        return None
    img = bpy.data.images.new(name=name + "_tex", width=tex.width, height=tex.height,
                              alpha=True)
    # Blender almacena pixels de abajo a arriba; la textura viene de arriba a
    # abajo. Se voltea verticalmente para que se vea derecha en el editor (y el
    # flip_v de las UV mantiene el mapeo coherente).
    w, h = tex.width, tex.height
    row = w * 4
    flipped = [0.0] * (len(tex.rgba))
    for y in range(h):
        src = (h - 1 - y) * row
        dst = y * row
        flipped[dst:dst + row] = tex.rgba[src:src + row]
    try:
        img.pixels.foreach_set(flipped)
    except AttributeError:
        img.pixels = flipped
    img.pack()
    return img


def _build_material(obj, icon, image, opts):
    mat = bpy.data.materials.new(name=obj.name + "_mat")
    mat.use_nodes = True
    nt = mat.node_tree
    nodes = nt.nodes
    links = nt.links

    bsdf = nodes.get("Principled BSDF")
    if bsdf is None:
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    # menos brillos especulares: los iconos son casi planos
    _set_input(bsdf, ("Specular", "Specular IOR Level"), 0.0)
    _set_input(bsdf, ("Roughness",), 1.0)

    base_color_socket = None

    if image is not None and opts["import_texture"]:
        tex_node = nodes.new("ShaderNodeTexImage")
        tex_node.image = image
        tex_node.location = (-600, 200)
        base_color_socket = tex_node.outputs["Color"]
        # alfa de la textura (por si el usuario activo el alfa de 1 bit)
        try:
            links.new(bsdf.inputs["Alpha"], tex_node.outputs["Alpha"])
            _enable_alpha_blend(mat)
        except Exception:  # noqa: BLE001
            pass

    if opts["import_colors"]:
        vc_node = _new_vertex_color_node(nodes, "Color")
        if vc_node is not None:
            vc_node.location = (-600, -150)
            if base_color_socket is None:
                base_color_socket = vc_node.outputs["Color"]
            else:
                mix = _new_multiply_node(nodes)
                if mix is not None:
                    mix.location = (-300, 100)
                    _link_multiply(links, mix, base_color_socket, vc_node.outputs["Color"])
                    base_color_socket = _multiply_output(mix)

    if base_color_socket is not None:
        links.new(bsdf.inputs["Base Color"], base_color_socket)

    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)
    return mat


# ---- helpers de nodos tolerantes a versiones ---- #
def _set_input(node, names, value):
    for n in names:
        if n in node.inputs:
            try:
                node.inputs[n].default_value = value
            except Exception:  # noqa: BLE001
                pass
            return


def _enable_alpha_blend(mat):
    if hasattr(mat, "blend_method"):
        mat.blend_method = "HASHED"


def _new_vertex_color_node(nodes, layer):
    for bl_id in ("ShaderNodeVertexColor", "ShaderNodeAttribute"):
        try:
            node = nodes.new(bl_id)
        except RuntimeError:
            continue
        if bl_id == "ShaderNodeVertexColor":
            node.layer_name = layer
        else:
            node.attribute_name = layer
        return node
    return None


def _new_multiply_node(nodes):
    # Blender 3.4+: ShaderNodeMix (data_type RGBA). Antes: ShaderNodeMixRGB.
    try:
        node = nodes.new("ShaderNodeMix")
        node.data_type = "RGBA"
        node.blend_type = "MULTIPLY"
        if "Factor" in node.inputs:
            node.inputs["Factor"].default_value = 1.0
        return node
    except (RuntimeError, TypeError):
        pass
    try:
        node = nodes.new("ShaderNodeMixRGB")
        node.blend_type = "MULTIPLY"
        node.inputs["Fac"].default_value = 1.0
        return node
    except RuntimeError:
        return None


def _link_multiply(links, mix, sock_a, sock_b):
    if mix.bl_idname == "ShaderNodeMix":
        # OJO: el nodo Mix tiene sockets "A"/"B" repetidos (Float/Vector/RGBA)
        # con el mismo nombre; los de color son los indices 6 y 7. Hay que
        # direccionarlos por indice, no por nombre (devolveria el Float).
        links.new(mix.inputs[6], sock_a)
        links.new(mix.inputs[7], sock_b)
    else:
        links.new(mix.inputs["Color1"], sock_a)
        links.new(mix.inputs["Color2"], sock_b)


def _multiply_output(mix):
    if mix.bl_idname == "ShaderNodeMix":
        return mix.outputs[2]  # Result (RGBA)
    return mix.outputs["Color"]


# --------------------------------------------------------------------------- #
#  Operador de importacion                                                     #
# --------------------------------------------------------------------------- #
class IMPORT_OT_ps2_ico(Operator, ImportHelper):
    """Importa un icono 3D .ico de PlayStation 2"""
    bl_idname = "import_scene.ps2_ico"
    bl_label = "Import PS2 Icon"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".ico"
    filter_glob: StringProperty(default="*.ico", options={"HIDDEN"})

    convert_axes: BoolProperty(
        name="Corregir ejes (PS2 -> Blender)",
        description="Rota del sistema de PS2 (Y hacia abajo) al de Blender (Z "
        "arriba) para que el icono quede derecho. Desactiva para coords crudas",
        default=True)
    scale: FloatProperty(
        name="Escala", description="Factor de escala global", default=1.0,
        min=0.0001, max=1000.0)
    import_normals: BoolProperty(name="Normales", default=True)
    import_uvs: BoolProperty(name="UVs", default=True)
    flip_v: BoolProperty(
        name="Voltear V", description="Invertir la coordenada V de las UV (PS2 usa "
        "origen arriba; Blender abajo)", default=True)
    import_colors: BoolProperty(name="Colores de vertice", default=True)
    import_texture: BoolProperty(name="Textura", default=True)
    opaque_texture: BoolProperty(
        name="Textura opaca", description="Forzar alfa=1 en la textura. El alfa de "
        "1 bit del formato suele estar a 0 (haria invisible la textura)",
        default=True)
    import_shapekeys: BoolProperty(
        name="Shape keys (morph)", description="Importar los morph targets como "
        "shape keys", default=True)
    import_animation: BoolProperty(
        name="Animacion (aprox.)", description="Crear una animacion ciclica entre "
        "las shape keys (tiempos aproximados, no verificados)", default=True)

    def execute(self, context):
        try:
            icon = ico.parse_file(self.filepath, opaque_alpha=self.opaque_texture)
        except ico.IcoError as e:
            self.report({"ERROR"}, "No es un .ico de PS2 valido: %s" % e)
            return {"CANCELLED"}
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "Error leyendo el .ico: %s" % e)
            return {"CANCELLED"}

        name = os.path.splitext(os.path.basename(self.filepath))[0]
        opts = {
            "convert_axes": self.convert_axes,
            "scale": self.scale,
            "import_normals": self.import_normals,
            "import_uvs": self.import_uvs,
            "flip_v": self.flip_v,
            "import_colors": self.import_colors,
            "import_texture": self.import_texture,
        }

        obj = _build_mesh(icon, name, opts)

        keyblocks = None
        if self.import_shapekeys:
            keyblocks = _add_shape_keys(obj, icon, opts)
            if self.import_animation and keyblocks:
                _animate_shape_keys(obj, icon, keyblocks)

        image = _build_image(icon, name) if self.import_texture else None
        _build_material(obj, icon, image, opts)

        # seleccionar y activar el objeto importado
        for o in context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj

        msg = "Importado '%s': %d vertices, %d triangulos, %d shape(s)" % (
            name, icon.num_vertices, icon.num_triangles, icon.animation_shapes)
        if icon.texture is None:
            msg += " (sin textura)"
        self.report({"INFO"}, msg)
        for w in icon.warnings:
            self.report({"WARNING"}, w)
        return {"FINISHED"}

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.prop(self, "convert_axes")
        col.prop(self, "scale")
        layout.separator()
        col = layout.column(align=True)
        col.prop(self, "import_normals")
        col.prop(self, "import_uvs")
        sub = col.row()
        sub.enabled = self.import_uvs
        sub.prop(self, "flip_v")
        col.prop(self, "import_colors")
        layout.separator()
        col = layout.column(align=True)
        col.prop(self, "import_texture")
        sub = col.row()
        sub.enabled = self.import_texture
        sub.prop(self, "opaque_texture")
        layout.separator()
        col = layout.column(align=True)
        col.prop(self, "import_shapekeys")
        sub = col.row()
        sub.enabled = self.import_shapekeys
        sub.prop(self, "import_animation")


# --------------------------------------------------------------------------- #
#  Exportacion: malla de Blender -> .ico                                       #
# --------------------------------------------------------------------------- #
def _axis_unconvert(co, convert, scale):
    """Inversa de :func:`_axis_convert`: Blender (X,Y,Z) -> PS2 (X,-Z,Y)."""
    x, y, z = co[0], co[1], co[2]
    if convert:
        return (x / scale, -z / scale, y / scale)
    return (x / scale, y / scale, z / scale)


def _loop_normals(mesh):
    """Devuelve las normales divididas por loop, compatible 3.x/4.x."""
    if hasattr(mesh, "calc_normals_split"):
        try:
            mesh.calc_normals_split()          # necesario antes de 4.1
        except Exception:  # noqa: BLE001
            pass


def _active_color_layer(mesh):
    """(layer, domain) del color activo, o (None, None)."""
    if hasattr(mesh, "color_attributes") and mesh.color_attributes:
        attr = mesh.color_attributes.active_color or mesh.color_attributes[0]
        return attr, attr.domain
    if hasattr(mesh, "vertex_colors") and mesh.vertex_colors:
        vc = mesh.vertex_colors.active or mesh.vertex_colors[0]
        return vc, "CORNER"
    return None, None


def _find_image(obj):
    """Primera imagen de un nodo de textura en los materiales del objeto, o None."""
    for slot in obj.material_slots:
        mat = slot.material
        if not mat or not mat.use_nodes:
            continue
        for node in mat.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is not None:
                return node.image
    return None


def _image_to_texture(img, texture_type):
    """Imagen de Blender (128x128, bottom-up) -> ico.Texture (top-down). None si no sirve."""
    if img is None:
        return None
    w, h = img.size[0], img.size[1]
    work = img
    tmp = None
    if (w, h) != (ico.TEX_W, ico.TEX_H):
        tmp = img.copy()                       # no mutar la imagen del usuario
        tmp.scale(ico.TEX_W, ico.TEX_H)
        work = tmp
    src = list(work.pixels)                     # RGBA float, fila 0 = abajo
    row = ico.TEX_W * 4
    rgba = [0.0] * (row * ico.TEX_H)
    for r in range(ico.TEX_H):                  # r = fila top-down de salida
        bl = ico.TEX_H - 1 - r
        rgba[r * row:(r + 1) * row] = src[bl * row:(bl + 1) * row]
    if tmp is not None:
        bpy.data.images.remove(tmp)
    return ico.Texture(ico.TEX_W, ico.TEX_H, rgba, texture_type, bool(texture_type & 0x08))


def _mesh_to_model(obj, opts):
    """Construye un :class:`ico.IcoModel` desde el objeto malla activo."""
    mesh = obj.data
    mesh.calc_loop_triangles()
    _loop_normals(mesh)

    uv_layer = mesh.uv_layers.active
    color_layer, color_domain = _active_color_layer(mesh)

    shape_blocks = None
    if opts["export_shapekeys"] and mesh.shape_keys:
        shape_blocks = mesh.shape_keys.key_blocks
    nshapes = len(shape_blocks) if shape_blocks else 1

    convert = opts["convert_axes"]
    scale = opts["scale"]
    flip_v = opts["flip_v"]

    shapes = [[] for _ in range(nshapes)]
    normals, uvs, colors = [], [], []

    for tri in mesh.loop_triangles:
        for k in range(3):
            li = tri.loops[k]
            vi = tri.vertices[k]
            loop = mesh.loops[li]

            normals.append(_axis_unconvert(loop.normal, convert, 1.0))

            if uv_layer:
                u, v = uv_layer.data[li].uv
                uvs.append((u, 1.0 - v if flip_v else v))
            else:
                uvs.append((0.0, 0.0))

            if color_layer is not None:
                idx = li if color_domain == "CORNER" else vi
                c = color_layer.data[idx].color
                colors.append((c[0], c[1], c[2], c[3]))
            else:
                colors.append((1.0, 1.0, 1.0, 1.0))

            for si in range(nshapes):
                if shape_blocks:
                    co = shape_blocks[si].data[vi].co
                else:
                    co = mesh.vertices[vi].co
                shapes[si].append(_axis_unconvert(co, convert, scale))

    model = ico.IcoModel()
    model.magic = ico.MAGIC
    model.animation_shapes = nshapes
    model.texture_type = 15 if opts["compress_texture"] else 7
    model.scale = 1.0
    model.num_vertices = len(normals)
    model.shapes = shapes
    model.normals = normals
    model.uvs = uvs
    model.colors = colors
    model.animation = {}                        # el codec sintetiza una valida

    if opts["export_texture"]:
        model.texture = _image_to_texture(_find_image(obj), model.texture_type)
    return model


class EXPORT_OT_ps2_ico(Operator, ExportHelper):
    """Exporta el objeto malla activo a un icono 3D .ico de PlayStation 2"""
    bl_idname = "export_scene.ps2_ico"
    bl_label = "Export PS2 Icon"
    bl_options = {"REGISTER"}

    filename_ext = ".ico"
    filter_glob: StringProperty(default="*.ico", options={"HIDDEN"})

    convert_axes: BoolProperty(
        name="Corregir ejes (Blender -> PS2)",
        description="Invierte la rotacion del import (Blender Z arriba -> PS2 Y "
        "abajo). Deja activado si importaste con esta misma opcion", default=True)
    scale: FloatProperty(
        name="Escala", description="Divisor de escala (usa el mismo valor que al "
        "importar)", default=1.0, min=0.0001, max=1000.0)
    flip_v: BoolProperty(
        name="Voltear V", description="Invertir V de las UV (igual que al importar)",
        default=True)
    export_texture: BoolProperty(
        name="Textura", description="Incluir la textura desde el material del objeto",
        default=True)
    compress_texture: BoolProperty(
        name="Comprimir textura (RLE)",
        description="texture_type 15 (RLE) en vez de 7 (cruda 32 KB). Cruda es lo "
        "mas seguro/compatible; RLE da archivos mas pequenos", default=False)
    export_shapekeys: BoolProperty(
        name="Shape keys (morph)", description="Exportar las shape keys como morph "
        "targets (animacion por vertices)", default=True)

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == "MESH"

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.type != "MESH":
            self.report({"ERROR"}, "Selecciona un objeto de malla para exportar")
            return {"CANCELLED"}

        opts = {
            "convert_axes": self.convert_axes,
            "scale": self.scale,
            "flip_v": self.flip_v,
            "export_texture": self.export_texture,
            "compress_texture": self.compress_texture,
            "export_shapekeys": self.export_shapekeys,
        }
        try:
            model = _mesh_to_model(obj, opts)
            size = ico.write_file(model, self.filepath)
        except ico.IcoError as e:
            self.report({"ERROR"}, "No se pudo construir el .ico: %s" % e)
            return {"CANCELLED"}
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "Error escribiendo el .ico: %s" % e)
            return {"CANCELLED"}

        self.report({"INFO"}, "Exportado '%s': %d vertices, %d triangulos, %d shape(s), %d bytes"
                    % (os.path.basename(self.filepath), model.num_vertices,
                       model.num_triangles, model.animation_shapes, size))
        if model.num_vertices % 3 != 0:
            self.report({"WARNING"}, "num_vertices no es multiplo de 3; revisa la malla")
        return {"FINISHED"}

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.prop(self, "convert_axes")
        col.prop(self, "scale")
        col.prop(self, "flip_v")
        layout.separator()
        col = layout.column(align=True)
        col.prop(self, "export_texture")
        sub = col.row()
        sub.enabled = self.export_texture
        sub.prop(self, "compress_texture")
        col.prop(self, "export_shapekeys")


def menu_func_import(self, context):
    self.layout.operator(IMPORT_OT_ps2_ico.bl_idname, text="PS2 Icon (.ico)")


def menu_func_export(self, context):
    self.layout.operator(EXPORT_OT_ps2_ico.bl_idname, text="PS2 Icon (.ico)")


_CLASSES = (IMPORT_OT_ps2_ico, EXPORT_OT_ps2_ico)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
