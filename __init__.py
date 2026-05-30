bl_info = {
    "name": "Nukxon VR Exporter",
    "description": "Render VR cubemap tours and export .nukxon packages",
    "author": "Nukxon, LLC",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Side Panel > Nukxon",
    "support": "COMMUNITY",
    "category": "Import-Export",
}

addon_version = (1, 0, 0)

import os
import json
import math
import zipfile
import datetime
from math import radians

import bpy
from mathutils import Vector


# ============================================================================
#  Utility functions
# ============================================================================

def get_scene_name():
    filepath = bpy.context.blend_data.filepath
    return "NukxonScene" if filepath == "" else bpy.path.display_name_from_filepath(filepath)


def abs_file_path(path):
    item = bpy.path.abspath(path)
    item = os.path.realpath(item)
    item = os.path.normpath(item)
    return item.replace('\\', '/')


def _safe_name(name):
    """Make a Blender object name safe for use in filenames."""
    safe = name.replace(" ", "_").replace(".", "_").replace("/", "_").replace("\\", "_")
    safe = "".join(c for c in safe if c.isalnum() or c in "_-")
    return safe or "unnamed"


def _is_nukxon_camera(obj):
    """Check if an object is a Nukxon camera marker (Empty with NukxonCameraMarker tag)."""
    return bool(obj.get('NukxonCameraMarker', False))


class NUKXON_OT_messagebox(bpy.types.Operator):
    bl_idname = "nukxon.messagebox"
    bl_label = ""
    message: bpy.props.StringProperty(name="message", default='')  # type: ignore

    def execute(self, context):
        self.report({'INFO'}, self.message)
        return {'FINISHED'}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        self.layout.label(text=self.message)


# ============================================================================
#  Teleport Points
# ============================================================================

class NukxonTeleportPoint(bpy.types.PropertyGroup):
    """A named teleport destination in the VR tour."""
    label: bpy.props.StringProperty(
        name="Label",
        description="Display name shown to clients (e.g. 'Master Suite', 'Kitchen')",
        default="Untitled",
    )  # type: ignore

    camera: bpy.props.PointerProperty(
        name="Camera",
        description="Camera position for this teleport point",
        type=bpy.types.Object,
        poll=lambda self, obj: _is_nukxon_camera(obj),
    )  # type: ignore



class NUKXON_UL_teleport_list(bpy.types.UIList):
    """UIList for teleport points."""
    bl_idname = "NUKXON_UL_teleport_list"

    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "label", text="", emboss=False)
            # Right column shows the bound NukxonCam name; falls back to an
            # "Assign camera" prompt when no camera is bound yet.
            if item.camera:
                row.label(text=item.camera.name, icon='CAMERA_DATA')
            else:
                row.label(text="Assign camera", icon='ERROR')

        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text=item.label)


class NUKXON_UL_link_list(bpy.types.UIList):
    """UIList for Project Link markers. Filters scene.objects to those tagged
    NukxonHotspotProp so we can display them in a scrollable list with the
    same visual treatment as the teleport list."""
    bl_idname = "NUKXON_UL_link_list"

    def draw_item(self, context, layout, data, item, icon, active_data, active_property, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "name", text="", emboss=False, icon='EMPTY_SINGLE_ARROW')
        elif self.layout_type == 'GRID':
            layout.alignment = 'CENTER'
            layout.label(text=item.name, icon='EMPTY_SINGLE_ARROW')

    def filter_items(self, context, data, propname):
        objs = getattr(data, propname)
        flt_flags = [
            self.bitflag_filter_item if obj.get("NukxonHotspotProp", False) else 0
            for obj in objs
        ]
        return flt_flags, []


class NUKXON_OT_teleport_add(bpy.types.Operator):
    """Add a new teleport point"""
    bl_idname = "nukxon.teleport_add"
    bl_label = "Add Teleport"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        tp = scene.nukxon_teleports.add()
        tp.label = f"Room {len(scene.nukxon_teleports)}"
        scene.nukxon_teleport_index = len(scene.nukxon_teleports) - 1
        return {'FINISHED'}


class NUKXON_OT_teleport_remove(bpy.types.Operator):
    """Remove the selected teleport point"""
    bl_idname = "nukxon.teleport_remove"
    bl_label = "Remove Teleport"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(context.scene.nukxon_teleports) > 0

    def execute(self, context):
        scene = context.scene
        scene.nukxon_teleports.remove(scene.nukxon_teleport_index)
        # Clamp to 0 floor — min(idx, len-1) yields -1 when the list becomes
        # empty, a latent off-by-one for any code that indexes the list.
        scene.nukxon_teleport_index = max(0, min(
            scene.nukxon_teleport_index, len(scene.nukxon_teleports) - 1))
        return {'FINISHED'}


class NUKXON_OT_teleport_move(bpy.types.Operator):
    """Reorder teleport points"""
    bl_idname = "nukxon.teleport_move"
    bl_label = "Move Teleport"
    bl_options = {'REGISTER', 'UNDO'}

    direction: bpy.props.EnumProperty(
        items=(('UP', "Up", ""), ('DOWN', "Down", "")),
    )  # type: ignore

    @classmethod
    def poll(cls, context):
        return len(context.scene.nukxon_teleports) > 1

    def execute(self, context):
        scene = context.scene
        idx = scene.nukxon_teleport_index
        new_idx = idx + (-1 if self.direction == 'UP' else 1)
        if 0 <= new_idx < len(scene.nukxon_teleports):
            scene.nukxon_teleports.move(idx, new_idx)
            scene.nukxon_teleport_index = new_idx
        return {'FINISHED'}


# ============================================================================
#  Scene Properties
# ============================================================================

def _apply_denoiser_toggle(self, context):
    """use_denoiser update callback — rewires the compositor live so the
    user sees the change immediately."""
    scene = context.scene
    engine = scene.render.engine
    if not scene.use_nodes or not scene.node_tree:
        return

    node_tree = scene.node_tree
    nodes     = node_tree.nodes
    rlayers_list   = [n for n in nodes if n.bl_idname == 'CompositorNodeRLayers']
    composite_list = [n for n in nodes if n.bl_idname == 'CompositorNodeComposite']
    if not rlayers_list or not composite_list:
        return

    rlayers   = rlayers_list[0]
    composite = composite_list[0]

    for n in [n for n in nodes if n.name.startswith(_DENOISER_NODE_PREFIX)]:
        nodes.remove(n)
    stale_grp = bpy.data.node_groups.get(_NUKXON_GROUP_NAME)
    if stale_grp:
        bpy.data.node_groups.remove(stale_grp)

    for lnk in list(node_tree.links):
        if lnk.to_node == composite and lnk.to_socket.name == 'Image':
            node_tree.links.remove(lnk)

    if self.use_denoiser and engine == 'CYCLES':
        enable_denoiser_passes(context.view_layer)
        denoised_out = build_denoiser_compositor(node_tree, rlayers)
        node_tree.links.new(denoised_out, composite.inputs['Image'])
        print("[Nukxon] Denoiser enabled")
    else:
        node_tree.links.new(rlayers.outputs['Image'], composite.inputs['Image'])
        print("[Nukxon] Denoiser disabled")


# Shim for the floormap slice-height slider's update callback. The real
# function lives near the floor-plan code further down the file; this
# stub forwards the call once that function is defined at module-load
# time. Allows the FloatProperty to reference an update target without
# forward-declaration headaches.
def _floorplan_slice_update_shim(self, context):
    fn = globals().get('_update_floorplan_preview_cam')
    if fn is None:
        return
    try:
        fn(context.scene, self)
    except Exception:
        pass  # never let a viewport hiccup break the slider


# Persistent holder for dynamic EnumProperty items lists. Blender does not
# keep a reference to the strings an items-callback returns, so without a
# Python-side holder the GC can free them mid-use → mojibake labels / crash.
_ENUM_ITEMS_CACHE = {}


class NukxonProperties(bpy.types.PropertyGroup):

    def get_collections(self, context):
        items = [("Scene Collection", "Scene Collection", "")]
        for col in bpy.data.collections:
            items.append((col.name, col.name, ""))
        # Blender does NOT retain a reference to the strings an EnumProperty
        # items-callback returns; if Python GCs them the dropdown shows
        # mojibake or crashes. Stash in a module-level holder so they
        # outlive the return.
        _ENUM_ITEMS_CACHE['collections'] = items
        return items

    def get_cameras(self, context):
        cams = []
        seen = set()
        selected = self.enum_collection if self.enum_collection else "Scene Collection"
        for col in bpy.data.collections:
            if selected not in {"Scene Collection", col.name}:
                continue
            for obj in col.objects:
                if _is_nukxon_camera(obj) and obj.name not in seen:
                    seen.add(obj.name)
                    cams.append((obj.name, obj.name, "Use as default"))
        # Cameras linked only to the scene master collection don't appear in
        # bpy.data.collections; mirror the export operator's scene-objects
        # fallback so the dropdown lists them too. Dedup via `seen` also kills
        # the multi-collection double-count.
        if selected == "Scene Collection" and context is not None and context.scene:
            for obj in context.scene.objects:
                if _is_nukxon_camera(obj) and obj.name not in seen:
                    seen.add(obj.name)
                    cams.append((obj.name, obj.name, "Use as default"))
        _ENUM_ITEMS_CACHE['cameras'] = cams  # GC reference holder
        return cams

    def get_camera_count(self, context):
        return len(self.get_cameras(context))

    def get_mesh_collections(self, context):
        """All collections available for mesh export + a full scene option."""
        items = [("FULL_SCENE", "Full scene (all visible)", "Export all visible objects in scene")]
        for col in bpy.data.collections:
            items.append((col.name, col.name, f"Export only objects in '{col.name}'"))
        _ENUM_ITEMS_CACHE['mesh_collections'] = items  # GC reference holder
        return items

    enum_collection: bpy.props.EnumProperty(
        name="Cameras from",
        description="Collection containing VR camera positions",
        items=get_collections,
    )  # type: ignore

    enum_default_cam: bpy.props.EnumProperty(
        name="Default cam",
        description="Starting camera for the VR tour",
        items=get_cameras,
    )  # type: ignore

    export_path: bpy.props.StringProperty(
        name="Output Path",
        description="Folder where exported files will be saved",
        default=os.path.expanduser("~/Documents/NukxonExport/"),
        maxlen=1024,
        subtype='DIR_PATH',
    )  # type: ignore

    mesh_collection: bpy.props.EnumProperty(
        name="Mesh",
        description="Collection to export as mesh.glb — choose a collection or export the full scene",
        items=get_mesh_collections,
    )  # type: ignore

    face_resolution: bpy.props.EnumProperty(
        name="Resolution",
        description="Cubemap face resolution per camera",
        items=(
            ('1024', "Draft  (1024x1024)",    "Fast export — 6 faces at 1024px, good for iterations"),
            ('2048', "Standard  (2048x2048)", "Production ready — 6 faces at 2048px, recommended"),
        ),
        default='2048',
    )  # type: ignore

    use_denoiser: bpy.props.BoolProperty(
        name="Nukxon Denoiser",
        description=(
            "Component-wise OIDN denoising (Cycles only). Denoise per "
            "light-path separately before recombining — clean output at "
            "50-100 samples."
        ),
        default=False,
        update=_apply_denoiser_toggle,
    )  # type: ignore

    show_cam_topology: bpy.props.BoolProperty(
        name="Show Cam Spacing Graph",
        description=(
            "Draw lines between each cam and its 3 nearest neighbours, "
            "colour-coded by distance. GREEN = good archviz spacing; "
            "YELLOW = slightly tight or sparse; RED = clustered or gap"
        ),
        default=True,
    )  # type: ignore

    floormap_slice_above_m: bpy.props.FloatProperty(
        name="Slice Above Cameras",
        description=(
            "Floor-plan slice height: how far ABOVE the average camera "
            "elevation the top-down view gets clipped. Lower = walls "
            "only / less clutter; higher = more interior detail but "
            "risks showing ceilings. Typical archviz: 0.3-1.2 m. "
            "If Live Preview is on, dragging this updates the viewport "
            "live with no render needed"
        ),
        default=0.5,
        min=-2.0,
        max=5.0,
        step=10,        # 0.1 m per drag tick
        precision=2,
        unit='LENGTH',
        update=_floorplan_slice_update_shim,
    )  # type: ignore

    # ── User-set framing override ──────────────────────────────────────
    # When `floormap_user_framed` is True, the render path uses these
    # stored values for center + ortho scale + aspect instead of the
    # auto-computed bounds. Captured by the "Set Framing" button while
    # Live Preview is active. Slice height stays driven by the slider.
    floormap_user_framed: bpy.props.BoolProperty(
        name="Floor Plan Framing Locked",
        default=False,
        options={'HIDDEN'},
    )  # type: ignore
    floormap_user_center_x: bpy.props.FloatProperty(
        name="Framing Center X", default=0.0, options={'HIDDEN'},
    )  # type: ignore
    floormap_user_center_y: bpy.props.FloatProperty(
        name="Framing Center Y", default=0.0, options={'HIDDEN'},
    )  # type: ignore
    floormap_user_ortho_scale: bpy.props.FloatProperty(
        name="Framing Ortho Scale", default=1.0, min=0.01, options={'HIDDEN'},
    )  # type: ignore
    floormap_user_aspect: bpy.props.FloatProperty(
        name="Framing Aspect (W/H)", default=1.0, min=0.01, options={'HIDDEN'},
    )  # type: ignore


# ============================================================================
#  Post-render data storage
# ============================================================================

class NukxonPostData:
    """Stores state needed for post-render processing."""
    output_path = ""
    scene_name = ""
    cameras = []
    camera_positions = {}
    render_engine = ""
    status        = ""  # shown in UI panel
    rendering_active = False  # true while render pipeline is running
    photographer_handlers = []  # re-enabled after render

    # Per-frame accumulation
    pano_filenames = {}
    frames_processed = 0

    floor_plan_meta = None      # dict from _render_floor_plan, or None

    # Saved scene settings
    old_gamma = 1.0
    old_use_nodes = True
    old_use_compositing = True
    old_use_extension = True
    old_res_x = 1920
    old_res_y = 1080
    old_res_percent = 100
    old_file_format = 'PNG'
    old_quality = 90
    old_filepath = ""
    old_frame_start = 0
    old_frame_end = 250
    scene_ref = None

    def reset(self):
        self.pano_filenames = {}
        self.frames_processed = 0
        self.camera_positions = {}
        self.status       = ""
        self.rendering_active = False
        self.photographer_handlers = []
        self.floor_plan_meta = None


post_data = NukxonPostData()


def _publish_status(text):
    """Update post_data.status, defer Blender UI redraw to main thread.
    Touching status_text_set/tag_redraw from render_post crashes Blender
    (NULL deref in WM_window_status_area_find). Skip UI entirely while
    rendering_active — between-frame timers also hit a half-init context."""
    post_data.status = text

    if post_data.rendering_active:
        return

    def _apply():
        try:
            wm = bpy.context.window_manager
            for window in wm.windows:
                window.workspace.status_text_set(
                    f"Nukxon: {text}" if text else None)
                for area in window.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
        except Exception:
            pass
        return None  # one-shot

    try:
        bpy.app.timers.register(_apply, first_interval=0.0)
    except Exception:
        pass


# ============================================================================
#  Cubemap face definitions
# ============================================================================

CUBEMAP_ROTATIONS = [
    (radians(90),   0,            0),              # front  (+Y)
    (radians(-180), 0,            0),              # top    (+Z)
    (radians(90),   0,            radians(-90)),   # right  (+X)
    (radians(90),   0,            radians(-180)),  # back   (-Y)
    (0,             0,            0),              # bottom (-Z)
    (radians(90),   0,            radians(90)),    # left   (-X)
]

CUBEMAP_FACE_NAMES = ["front", "top", "right", "back", "bottom", "left"]


_CUBEMAP_WEBP_QUALITY = 90
_IMAGE_EXTENSIONS = ['.jpg', '.jpeg', '.webp', '.png', '.tiff', '.tif', '.bmp']


def ensure_webp(output_path, base_name, quality=_CUBEMAP_WEBP_QUALITY):
    """Find the rendered frame (any format) and convert to WebP if needed."""
    webp_name = base_name + ".webp"
    webp_path = os.path.join(output_path, webp_name)
    if os.path.exists(webp_path):
        return webp_name
    for ext in _IMAGE_EXTENSIONS:
        if ext == '.webp':
            continue
        src_path = os.path.join(output_path, base_name + ext)
        if not os.path.exists(src_path):
            continue
        img = None
        scene = bpy.context.scene
        old_fmt = scene.render.image_settings.file_format
        old_q = scene.render.image_settings.quality
        try:
            img = bpy.data.images.load(src_path, check_existing=False)
            scene.render.image_settings.file_format = 'WEBP'
            scene.render.image_settings.quality = quality
            img.save_render(webp_path, scene=scene)
            img.buffers_free()
            bpy.data.images.remove(img)
            img = None
            os.remove(src_path)
            return webp_name
        except Exception as e:
            print(f"[Nukxon] WARNING: Failed to convert {base_name}{ext} to WebP: {e}")
            return None
        finally:
            # Always restore render image settings + free a leaked image even
            # if save_render threw mid-conversion.
            try:
                scene.render.image_settings.file_format = old_fmt
                scene.render.image_settings.quality = old_q
            except Exception:
                pass
            try:
                if img is not None and img.name in bpy.data.images:
                    bpy.data.images.remove(img)
            except Exception:
                pass
    return None


# ============================================================================
#  Core scene setup
# ============================================================================

def create_nukxon_camera(cameras, scene):
    """Create a single camera with keyframed positions/rotations for cubemap rendering."""
    # Always delete and recreate — ensures clean Cycles property attachment
    existing = bpy.data.objects.get("NukxonCamera")
    if existing is not None:
        old_data = existing.data
        bpy.data.objects.remove(existing, do_unlink=True)
        bpy.data.cameras.remove(old_data)

    cam_data = bpy.data.cameras.new("NukxonCamData")
    cam_obj  = bpy.data.objects.new("NukxonCamera", cam_data)
    scene.collection.objects.link(cam_obj)
    scene.camera = cam_obj

    # Cubemap — 6 faces per camera, 90 deg FOV each
    cam_obj.data.type  = 'PERSP'
    cam_obj.data.lens_unit = 'FOV'
    cam_obj.data.angle = radians(90)
    frame = 0.0
    for cam in cameras:
        for rot in CUBEMAP_ROTATIONS:
            cam_obj.location = cam.location
            cam_obj.keyframe_insert(data_path="location", frame=frame)
            cam_obj.rotation_euler = rot
            cam_obj.keyframe_insert(data_path="rotation_euler", frame=frame)
            frame += 1.0


def enable_denoiser_passes(view_layer):
    """Enable Cycles light-path passes required by the Super Denoiser group."""
    view_layer.use_pass_combined = True
    view_layer.use_pass_diffuse_direct = True
    view_layer.use_pass_diffuse_indirect = True
    view_layer.use_pass_diffuse_color = True
    view_layer.use_pass_glossy_direct = True
    view_layer.use_pass_glossy_indirect = True
    view_layer.use_pass_glossy_color = True
    view_layer.use_pass_transmission_direct = True
    view_layer.use_pass_transmission_indirect = True
    view_layer.use_pass_transmission_color = True
    view_layer.cycles.use_pass_volume_direct = True
    view_layer.cycles.use_pass_volume_indirect = True
    view_layer.use_pass_emit = True
    view_layer.use_pass_environment = True
    view_layer.cycles.denoising_store_passes = True   # Normal + Albedo guides


_DENOISER_NODE_PREFIX  = "NUKXON_DN_"
_NUKXON_GROUP_NAME     = ".Nukxon Super Denoiser"


def _build_nukxon_super_group():
    """Build the .Nukxon Super Denoiser compositor node group.
    Per-component OIDN: denoise Direct + Indirect per light path, ADD them,
    MULTIPLY by Color pass, then ADD all components into final Image."""
    old = bpy.data.node_groups.get(_NUKXON_GROUP_NAME)
    if old:
        bpy.data.node_groups.remove(old)

    grp = bpy.data.node_groups.new(type="CompositorNodeTree", name=_NUKXON_GROUP_NAME)
    G_in  = grp.nodes.new("NodeGroupInput")
    G_out = grp.nodes.new("NodeGroupOutput")
    G_in.location  = (-900,   0)
    G_out.location = ( 1300, -400)

    lk = grp.links

    def link(src, dst):
        lk.new(src, dst)

    grp.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")

    def add_panel(name, inp_names, out_names, inp_types=None, out_types=None):
        p = grp.interface.new_panel(name, default_closed=False)
        for i, n in enumerate(inp_names):
            t = (inp_types[i] if inp_types else None) or "NodeSocketColor"
            grp.interface.new_socket(n, in_out="INPUT",  socket_type=t, parent=p)
        for i, n in enumerate(out_names):
            t = (out_types[i] if out_types else None) or "NodeSocketColor"
            grp.interface.new_socket(n, in_out="OUTPUT", socket_type=t, parent=p)
        return p

    add_panel("Diffuse",
        inp_names=["DiffDir",  "DiffInd",  "DiffCol"],
        out_names=["DiffDir",  "DiffInd",  "Diffuse"])

    add_panel("Glossy",
        inp_names=["GlossDir", "GlossInd", "GlossCol"],
        out_names=["GlossDir", "GlossInd", "Glossy"])

    add_panel("Transmission",
        inp_names=["TransDir", "TransInd", "TransCol"],
        out_names=["TransDir", "TransInd", "Transmission"])

    add_panel("Volume",
        inp_names=["VolumeDir", "VolumeInd"],
        out_names=["VolumeDir", "VolumeInd", "Volume"])

    add_panel("Emit",
        inp_names=["Emit"],
        out_names=["Emit"])

    add_panel("Env",
        inp_names=["Env"],
        out_names=["Env"])

    add_panel("Technical",
        inp_names=["Denoising Normal", "Denoising Albedo"],
        out_names=[],
        inp_types=["NodeSocketVector", "NodeSocketColor"])

    # Shorthand: guide passes from group input
    dn_normal = G_in.outputs["Denoising Normal"]
    dn_albedo = G_in.outputs["Denoising Albedo"]

    # ── Internal node helpers ─────────────────────────────────────────────────
    def oidn(label, x, y):
        n = grp.nodes.new("CompositorNodeDenoise")
        n.label     = label
        n.name      = f"NUKXON_{label.replace(' ', '_')}"
        n.prefilter = "ACCURATE"
        n.use_hdr   = True
        n.location  = (x, y)
        return n

    def mix(blend_type, x, y):
        n = grp.nodes.new("CompositorNodeMixRGB")
        n.blend_type = blend_type
        n.inputs[0].default_value = 1.0
        n.location = (x, y)
        return n

    def _wire_passes(node, image_in):
        """Wire (Image, Normal, Albedo) into an OIDN Denoise node."""
        link(image_in, node.inputs[0])
        link(dn_normal, node.inputs[1])
        link(dn_albedo, node.inputs[2])

    def build_component(dir_in, ind_in, col_in, y):
        dn_d = oidn("Denoise Direct",   200, y)
        dn_i = oidn("Denoise Indirect", 200, y - 200)
        add  = mix("ADD",               550, y - 100)
        mul  = mix("MULTIPLY",          780, y - 100)
        _wire_passes(dn_d, dir_in)
        _wire_passes(dn_i, ind_in)
        link(dn_d.outputs[0], add.inputs[1])
        link(dn_i.outputs[0], add.inputs[2])
        link(add.outputs[0],  mul.inputs[1])
        link(col_in,          mul.inputs[2])
        return dn_d.outputs[0], dn_i.outputs[0], mul.outputs[0]

    # Volume skips the Color-multiply step (no volume color pass exists).
    def build_volume(dir_in, ind_in, y):
        dn_d = oidn("Volume Direct",   200, y)
        dn_i = oidn("Volume Indirect", 200, y - 200)
        add  = mix("ADD",              550, y - 100)
        _wire_passes(dn_d, dir_in)
        _wire_passes(dn_i, ind_in)
        link(dn_d.outputs[0], add.inputs[1])
        link(dn_i.outputs[0], add.inputs[2])
        return dn_d.outputs[0], dn_i.outputs[0], add.outputs[0]

    I = G_in.outputs
    diff_d,  diff_i,  diff_img  = build_component(I["DiffDir"],  I["DiffInd"],  I["DiffCol"],  y=500)
    gloss_d, gloss_i, gloss_img = build_component(I["GlossDir"], I["GlossInd"], I["GlossCol"], y=100)
    trans_d, trans_i, trans_img = build_component(I["TransDir"], I["TransInd"], I["TransCol"], y=-300)
    vol_d,   vol_i,   vol_img   = build_volume(I["VolumeDir"], I["VolumeInd"], y=-700)

    dn_emit = oidn("Emit",        200, -1000)
    dn_env  = oidn("Environment", 200, -1200)
    _wire_passes(dn_emit, I["Emit"])
    _wire_passes(dn_env,  I["Env"])

    def _add_pair(y, in_a, in_b):
        n = mix("ADD", 1050, y)
        link(in_a, n.inputs[1])
        link(in_b, n.inputs[2])
        return n.outputs[0]

    a1 = _add_pair( 300, diff_img,        gloss_img)
    a2 = _add_pair(   0, a1,              trans_img)
    a3 = _add_pair(-300, a2,              vol_img)
    a4 = _add_pair(-600, a3,              dn_emit.outputs[0])
    a5 = _add_pair(-900, a4,              dn_env.outputs[0])

    O = G_out.inputs
    link(a5, O["Image"])
    link(diff_d,  O["DiffDir"]);   link(diff_i,  O["DiffInd"]);   link(diff_img,  O["Diffuse"])    # noqa: E702
    link(gloss_d, O["GlossDir"]);  link(gloss_i, O["GlossInd"]);  link(gloss_img, O["Glossy"])     # noqa: E702
    link(trans_d, O["TransDir"]);  link(trans_i, O["TransInd"]);  link(trans_img, O["Transmission"])  # noqa: E702
    link(vol_d,   O["VolumeDir"]); link(vol_i,   O["VolumeInd"]); link(vol_img,   O["Volume"])     # noqa: E702
    link(dn_emit.outputs[0], O["Emit"])
    link(dn_env.outputs[0],  O["Env"])

    return grp


def build_denoiser_compositor(node_tree, rlayers):
    """Add the Super Denoiser group to the compositor and wire all passes."""
    grp_data = _build_nukxon_super_group()

    grp_node = node_tree.nodes.new("CompositorNodeGroup")
    grp_node.name      = f"{_DENOISER_NODE_PREFIX}Group"
    grp_node.label     = ".Nukxon Super Denoiser"
    grp_node.node_tree = grp_data
    grp_node.location  = (rlayers.location[0] + 380, rlayers.location[1])
    grp_node.width     = 240

    pass_names = (
        "DiffDir",  "DiffInd",  "DiffCol",
        "GlossDir", "GlossInd", "GlossCol",
        "TransDir", "TransInd", "TransCol",
        "VolumeDir","VolumeInd",
        "Emit", "Env",
        "Denoising Normal", "Denoising Albedo",
    )
    for p in pass_names:
        if p in rlayers.outputs and p in grp_node.inputs:
            node_tree.links.new(rlayers.outputs[p], grp_node.inputs[p])

    return grp_node.outputs["Image"]


def prepare_compositor(context, scene, remove_mode="NONE"):
    """Enable compositing + denoiser passes if denoiser is active."""
    engine = scene.render.engine
    context.view_layer.use_pass_combined = True
    props = scene.nukxon_props
    if getattr(props, 'use_denoiser', False) and engine == 'CYCLES':
        enable_denoiser_passes(context.view_layer)
    scene.render.use_compositing = True


def create_output_nodes(scene, output_path):
    """Splice the Super Denoiser group in front of Composite. The user's
    existing compositor (grading, V-Ray/LuxCore nodes, etc.) is preserved —
    whatever reaches the Composite node is what Nukxon captures."""
    node_tree = scene.node_tree
    nodes     = node_tree.nodes
    engine    = scene.render.engine
    props = scene.nukxon_props
    use_denoiser = getattr(props, 'use_denoiser', False) and engine == 'CYCLES'

    if use_denoiser:
        rlayers_list = [n for n in nodes if n.bl_idname == 'CompositorNodeRLayers']
        if rlayers_list:
            rlayers   = rlayers_list[0]
            comp_list = [n for n in nodes if n.bl_idname == 'CompositorNodeComposite']
            composite = comp_list[0] if comp_list else nodes.new("CompositorNodeComposite")
            if not composite.inputs['Image'].links:
                denoised_out = build_denoiser_compositor(node_tree, rlayers)
                node_tree.links.new(denoised_out, composite.inputs['Image'])
        else:
            print("[Nukxon] WARNING: No Render Layers node found — denoiser skipped")



# ============================================================================
#  Mesh GLB export
# ============================================================================

def export_mesh_glb(output_path, collection_name=""):
    """Export scene geometry as Y-up GLB + Draco. Must run BEFORE render
    starts (full Blender context needed). When collection_name names a real
    collection (not FULL_SCENE/empty), export ONLY that collection's objects
    via use_selection — otherwise the whole visible scene ships and the
    user's Mesh-collection pick is silently ignored."""
    import time as _time
    t0 = _time.time()
    print(f"[Nukxon] Exporting mesh to {output_path}...")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Decide selection vs visible mode from the collection pick.
    use_col = bool(collection_name and collection_name != "FULL_SCENE"
                   and bpy.data.collections.get(collection_name))
    _saved_selection = None
    _saved_active = None
    if use_col:
        try:
            _saved_selection = [o for o in bpy.context.selected_objects]
            _saved_active = bpy.context.view_layer.objects.active
            if bpy.context.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            bpy.ops.object.select_all(action='DESELECT')
            sel_count = 0
            for o in bpy.data.collections[collection_name].all_objects:
                try:
                    o.select_set(True)
                    sel_count += 1
                except Exception:
                    pass
            print(f"[Nukxon] mesh.glb: exporting {sel_count} objects from "
                  f"collection '{collection_name}'")
        except Exception as e:
            # If selection fails for any reason, fall back to whole-scene
            # export rather than aborting the whole pipeline.
            print(f"[Nukxon] WARNING: collection select failed ({e}); "
                  f"exporting full visible scene")
            use_col = False

    bpy.ops.export_scene.gltf(
        filepath                             = output_path,
        export_format                        = 'GLB',
        use_selection                        = use_col,
        use_visible                          = (not use_col),
        use_renderable                       = False,
        export_yup                           = True,
        export_apply                         = True,
        export_normals                       = True,
        export_texcoords                     = False,
        export_materials                     = 'NONE',
        export_cameras                       = False,
        export_lights                        = False,
        export_extras                        = False,
        export_animations                    = False,
        export_skins                         = False,
        export_draco_mesh_compression_enable = True,
        export_draco_mesh_compression_level  = 6,
        export_draco_position_quantization   = 14,
        export_draco_normal_quantization     = 10,
        export_draco_texcoord_quantization   = 0,
        export_draco_color_quantization      = 0,
        export_draco_generic_quantization    = 0,
    )

    # Restore the user's prior selection/active object if we changed it.
    if _saved_selection is not None:
        try:
            bpy.ops.object.select_all(action='DESELECT')
            for o in _saved_selection:
                try:
                    o.select_set(True)
                except Exception:
                    pass
            if _saved_active is not None:
                bpy.context.view_layer.objects.active = _saved_active
        except Exception:
            pass

    if os.path.exists(output_path):
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"[Nukxon] mesh.glb: {size_mb:.1f} MB in {_time.time()-t0:.1f}s")
        return True
    else:
        print("[Nukxon] WARNING: mesh.glb not created")
        return False


# ============================================================================
#  Per-frame handler
# ============================================================================

def nukxon_frame_complete(scene):
    _handle_cubemap_frame(scene)


def _handle_cubemap_frame(scene):
    """Process a cubemap face render — rename to explicit CameraName_face.webp."""
    output_path = post_data.output_path
    frame_num = scene.frame_current
    cam_idx = frame_num // 6
    face_idx = frame_num % 6

    if cam_idx not in post_data.pano_filenames:
        post_data.pano_filenames[cam_idx] = []

    if cam_idx not in post_data.camera_positions and cam_idx < len(post_data.cameras):
        cam = post_data.cameras[cam_idx]
        post_data.camera_positions[cam_idx] = [
            cam.location.x, cam.location.y, cam.location.z]

    if cam_idx < len(post_data.cameras):
        # Prefix the camera INDEX so distinct cameras whose names collapse to
        # the same _safe_name (e.g. 'Cam.001' and 'Cam_001' both -> 'Cam_001')
        # can never share a per-face filename and overwrite each other.
        cam_name = f"{_safe_name(post_data.cameras[cam_idx].name)}_{cam_idx:02d}"
    else:
        cam_name = f"cam{cam_idx:02d}"
    face_name = CUBEMAP_FACE_NAMES[face_idx]
    new_name = f"{cam_name}_{face_name}.webp"

    base_name = f"{post_data.scene_name}{frame_num:04d}"
    src_name = ensure_webp(output_path, base_name, quality=_CUBEMAP_WEBP_QUALITY)

    if src_name:
        old_path = os.path.join(output_path, src_name)
        new_path = os.path.join(output_path, new_name)
        if os.path.exists(new_path) and new_path != old_path:
            os.remove(new_path)
        os.rename(old_path, new_path)
        post_data.pano_filenames[cam_idx].append(new_name)
    else:
        # Do NOT append a face that wasn't written — otherwise the manifest
        # advertises a 'cameras/<face>' path that's absent from the .nukxon
        # zip (viewer 404 / phantom face).
        print(f"[Nukxon] Frame {frame_num}: WARNING - Missing {new_name} (omitted from manifest)")

    post_data.frames_processed += 1
    total_frames = len(post_data.cameras) * 6
    _publish_status(f"Rendering cubemaps... {post_data.frames_processed}/{total_frames}")

    # Cursor-side progress widget (Blender's wm.progress_update).
    try:
        bpy.context.window_manager.progress_update(post_data.frames_processed)
    except Exception:
        pass


# ============================================================================
#  Render complete handler
# ============================================================================

def nukxon_render_complete(dummy):
    """Called once when all frames finish. Hands off to _deferred_restore."""
    if nukxon_frame_complete in bpy.app.handlers.render_post:
        bpy.app.handlers.render_post.remove(nukxon_frame_complete)
    if nukxon_render_complete in bpy.app.handlers.render_complete:
        bpy.app.handlers.render_complete.remove(nukxon_render_complete)
    print("[Nukxon] All renders complete — packaging...")
    _publish_status("Packaging .nukxon...")
    bpy.app.timers.register(_deferred_restore, first_interval=2.0)


def _deferred_package():
    """Build the .nukxon zip + clean up loose files. Runs after manifest is
    written by _deferred_restore_inner."""
    try:
        output_path = post_data.output_path
        if not output_path:
            return None

        m = _build_mesh_package(output_path)
        glb_path = os.path.join(output_path, "mesh.glb")
        manifest_path = os.path.join(output_path, "manifest.json")
        _cleanup_loose_files(output_path, manifest_path, glb_path)

        if m:
            pkg_path, pkg_size = m
            print(f"\n[Nukxon] EXPORT COMPLETE: {os.path.basename(pkg_path)} "
                  f"({pkg_size / (1024*1024):.1f} MB, {len(post_data.cameras)} cams)\n")
            _publish_status("Export complete")
        else:
            print("[Nukxon] WARNING: packaging failed")
            _publish_status("Export failed — check console")
    except Exception as e:
        print(f"[Nukxon] ERROR in packaging: {e}")
        import traceback
        traceback.print_exc()
    return None


def _deferred_restore():
    """Post-render: manifest, packaging, scene restore, cleanup. Probes for
    ID-write permission first; returns 0.5 to retry while still locked."""
    scene = post_data.scene_ref
    if scene is not None:
        try:
            scene.frame_current = scene.frame_current  # probe write
        except (AttributeError, RuntimeError) as _e:
            _m = str(_e)
            if "Writing to ID classes" in _m or "blend data" in _m:
                return 0.5  # context still locked, retry in 500ms
    try:
        return _deferred_restore_inner()
    except Exception as e:
        print(f"[Nukxon] ERROR in deferred restore: {e}")
        import traceback
        traceback.print_exc()
        return None


def _restore_photographer_handlers():
    """Re-append any Photographer-addon frame_change_post handlers we stripped
    before the render. Idempotent (guards against double-append). Called from
    the normal restore path AND the _start_render give-up branch so the other
    addon's handlers are never lost when our render fails to start."""
    for h in post_data.photographer_handlers:
        if h not in bpy.app.handlers.frame_change_post:
            bpy.app.handlers.frame_change_post.append(h)
    post_data.photographer_handlers = []


def _deferred_restore_inner():
    scene = post_data.scene_ref
    post_data.rendering_active = False

    try:
        bpy.context.window_manager.progress_end()
    except Exception:
        pass

    if scene is not None:
        try:
            scene.view_settings.gamma = post_data.old_gamma
            scene.use_nodes = post_data.old_use_nodes
            scene.render.use_compositing = post_data.old_use_compositing
            scene.render.use_file_extension = post_data.old_use_extension
            scene.render.resolution_x = post_data.old_res_x
            scene.render.resolution_y = post_data.old_res_y
            scene.render.resolution_percentage = post_data.old_res_percent
            scene.render.image_settings.file_format = post_data.old_file_format
            scene.render.image_settings.quality = post_data.old_quality
            scene.render.filepath = post_data.old_filepath
            scene.frame_start = post_data.old_frame_start
            scene.frame_end = post_data.old_frame_end
            print("[Nukxon] Scene settings restored")
        except AttributeError as e:
            if "Writing to ID classes in this context is not allowed" in str(e):
                print("[Nukxon] Context restricted — skipping scene restore, continuing with export...")
            else:
                print(f"[Nukxon] Scene restore warning: {e}")

        # Remove the denoiser compositor node + group we spliced in for the
        # render, so 'restore' truly returns the compositor to its prior
        # state instead of leaving the group wired into a user's tree.
        try:
            if scene.node_tree:
                _nt = scene.node_tree
                for _n in [n for n in _nt.nodes if n.name.startswith(_DENOISER_NODE_PREFIX)]:
                    _nt.nodes.remove(_n)
            _grp = bpy.data.node_groups.get(_NUKXON_GROUP_NAME)
            if _grp and _grp.users == 0:
                bpy.data.node_groups.remove(_grp)
        except Exception as e:
            print(f"[Nukxon] Denoiser node cleanup warning: {e}")

    output_path = post_data.output_path
    if output_path:
        post_data.floor_plan_meta = _render_floor_plan(output_path)
        _write_manifests(output_path)
        bpy.app.timers.register(_deferred_package, first_interval=1.0)

    _restore_photographer_handlers()

    cam = bpy.data.objects.get("NukxonCamera")
    if cam is not None:
        # Remove the object AND its camera datablock — objects.remove() does
        # not cascade-delete .data, so the NukxonCamData block would orphan
        # and auto-suffix (.001, .002) one per export otherwise.
        cam_data = cam.data
        bpy.data.objects.remove(cam, do_unlink=True)
        try:
            if cam_data and cam_data.users == 0 and cam_data.name in bpy.data.cameras:
                bpy.data.cameras.remove(cam_data)
        except Exception:
            pass

    # Orphan-image safety net — SCOPED to images whose file lives inside the
    # export output folder (i.e. the addon's own temp frames from ensure_webp).
    # The old unscoped sweep deleted EVERY zero-user image in the file,
    # silently destroying the user's unrelated loaded-but-unassigned images.
    out_dir = post_data.output_path or ""
    out_dir_abs = os.path.normcase(os.path.abspath(out_dir)) if out_dir else ""
    if out_dir_abs:
        for img in [im for im in bpy.data.images if not im.users]:
            try:
                fp = img.filepath_from_user() if hasattr(img, 'filepath_from_user') else img.filepath
                if not fp:
                    continue
                fp_abs = os.path.normcase(os.path.abspath(bpy.path.abspath(fp)))
                if fp_abs.startswith(out_dir_abs):
                    bpy.data.images.remove(img)
            except Exception:
                pass

    return None



# ============================================================================
#  Manifest helpers — visibility
# ============================================================================

# COORDINATE FRAME: the manifest ships raw Blender Z-up RH meters — camera
# matrices emitted row-major as-is and tagged coords="blender_zup_meters",
# with the mesh exported Y-up (export_yup=True). The Nukxon platform reads
# the coords tag and handles any alignment on its side, so the exporter does
# no coordinate conversion here. (An earlier in-addon conversion introduced a
# 90° pitch error and was removed; let the platform own frame alignment.)


def _is_visible(scene, src, dst, margin=0.05):
    """True if dst is unoccluded from src. Margin shrinks the ray so it
    doesn't self-intersect at the target."""
    direction = dst - src
    distance  = direction.length
    if distance < 0.001:
        return True
    hit, _, _, _, _, _ = scene.ray_cast(
        bpy.context.view_layer.depsgraph,
        src,
        direction.normalized(),
        distance=distance - margin,
    )
    return not hit


def _get_visible_cameras(scene, source_cam, all_cameras, max_distance=20.0):
    src = Vector(source_cam.location)
    visible = []
    for cam in all_cameras:
        if cam.name == source_cam.name:
            continue
        dst = Vector(cam.location)
        if (dst - src).length > max_distance:
            continue
        if _is_visible(scene, src, dst):
            visible.append(cam.name)
    return visible


def _get_visible_hotspots(scene, source_cam, hotspot_objs, max_distance=15.0):
    src = Vector(source_cam.location)
    visible = []
    for hs in hotspot_objs:
        dst = Vector(hs.location)
        if (dst - src).length > max_distance:
            continue
        if _is_visible(scene, src, dst):
            visible.append(hs.name.replace(" ", "_").lower())
    return visible


# ============================================================================
#  Camera spacing topology overlay
#  ----------------------------------------------------------------------------
#  Colour-coded lines between each cam and its K nearest neighbours. Bands
#  tuned for archviz (room corners 2-3m apart still read as green; only
#  flag actual clustering or actual coverage gaps).
#    < 0.6m  → RED    (clustered — likely redundant captures)
#    ≤ 3.5m  → GREEN  (normal archviz range)
#    ≤ 5.0m  → YELLOW (sparse — consider an intermediate cam)
#    > 5.0m  → RED    (coverage gap)
# ============================================================================

_TOPOLOGY_K_NEAREST = 3
_LOS_HIT_MARGIN_M = 0.10   # wall-thickness tolerance for raycast LOS check
_TOPOLOGY_GREEN_LO = 0.6
_TOPOLOGY_GREEN_HI = 3.5
_TOPOLOGY_RED_HI   = 5.0

_cam_topology_handle = None
_cam_topology_cache = {
    "sig": None,
    "lines_by_color": {"green": [], "yellow": [], "red": []},
    "cams_by_state":  {"green": [], "yellow": [], "red": []},
    "edge_count": 0,
}


def _has_line_of_sight(scene, depsgraph, pos_a, pos_b, margin=_LOS_HIT_MARGIN_M):
    """True if pos_a can see pos_b unoccluded. Margin tolerates raycast
    precision wobble near the target."""
    if depsgraph is None:
        return True
    diff = pos_b - pos_a
    dist = diff.length
    if dist < 1e-3:
        return True
    direction = diff / dist
    try:
        hit, hit_loc, _, _, _, _ = scene.ray_cast(
            depsgraph, pos_a, direction, distance=dist + margin)
    except Exception:
        return True
    if not hit:
        return True
    return (Vector(hit_loc) - pos_a).length >= dist - margin


def _compute_cam_topology(markers, k_neighbors=_TOPOLOGY_K_NEAREST,
                          scene=None, depsgraph=None):
    """For each marker, find K nearest neighbours and emit colour-coded edges.
    Walls block edges (LOS raycast). Each cam inherits the WORST state of its
    incident edges, so a green cam means all its visible neighbours are
    well-spaced. Distance bands are absolute metres (archviz-tuned)."""
    positions = [(m.name, Vector(m.location)) for m in markers]
    n = len(positions)
    if n < 2:
        return ({"green": [], "yellow": [], "red": []}, {}, [], 0)

    priority = {"green": 0, "yellow": 1, "red": 2}
    cam_state = {name: "green" for name, _ in positions}

    edges_seen = set()
    buckets = {"green": [], "yellow": [], "red": []}
    for i in range(n):
        name_i, pos_i = positions[i]
        dists = sorted(
            ((positions[j][1] - pos_i).length, j) for j in range(n) if j != i
        )
        for d, j in dists[:k_neighbors]:
            key = tuple(sorted((i, j)))
            if key in edges_seen:
                continue
            edges_seen.add(key)
            pos_j = positions[j][1]
            if scene is not None and depsgraph is not None:
                if not _has_line_of_sight(scene, depsgraph, pos_i, pos_j):
                    continue
            if d < _TOPOLOGY_GREEN_LO or d > _TOPOLOGY_RED_HI:
                bucket = "red"
            elif d <= _TOPOLOGY_GREEN_HI:
                bucket = "green"
            else:
                bucket = "yellow"
            buckets[bucket].append(
                ((pos_i.x, pos_i.y, pos_i.z),
                 (pos_j.x, pos_j.y, pos_j.z))
            )
            name_j = positions[j][0]
            for nm in (name_i, name_j):
                if priority[bucket] > priority[cam_state[nm]]:
                    cam_state[nm] = bucket

    cam_positions = [(name, (pos.x, pos.y, pos.z), cam_state[name])
                     for name, pos in positions]
    return buckets, cam_state, cam_positions, sum(len(v) for v in buckets.values())


def _draw_cam_topology():
    """Persistent viewport overlay: laser-glow edges + halo markers."""
    global _cam_topology_cache
    try:
        scene = bpy.context.scene
        props = scene.nukxon_props
    except Exception:
        return
    if not props.show_cam_topology:
        _cam_topology_cache["sig"] = None
        return

    # Hide the overlay while the user is looking through the "Preview Start"
    # camera — the topology lines would clutter the entry-direction preview.
    # Per-viewport check: only suppress in the viewport that's actively in
    # camera view through the preview cam.
    try:
        space = bpy.context.space_data
        cam = scene.camera
        if (space and space.type == 'VIEW_3D'
                and space.region_3d.view_perspective == 'CAMERA'
                and cam is not None
                and cam.get('NukxonPreviewCam', False)):
            return
    except Exception:
        pass

    markers = [obj for obj in scene.objects if _is_nukxon_camera(obj)]
    if len(markers) < 2:
        return

    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    except Exception:
        depsgraph = None
    sig_parts = [(m.name,
                  round(m.location.x, 4),
                  round(m.location.y, 4),
                  round(m.location.z, 4)) for m in markers]
    sig = (tuple(sig_parts), _TOPOLOGY_K_NEAREST, depsgraph is not None)
    if _cam_topology_cache["sig"] != sig:
        buckets, _state, cam_positions, edge_count = _compute_cam_topology(
            markers, k_neighbors=_TOPOLOGY_K_NEAREST,
            scene=scene, depsgraph=depsgraph)
        cams_by_state = {"green": [], "yellow": [], "red": []}
        for _name, xyz, st in cam_positions:
            cams_by_state[st].append(xyz)
        _cam_topology_cache["sig"] = sig
        _cam_topology_cache["lines_by_color"] = buckets
        _cam_topology_cache["cams_by_state"] = cams_by_state
        _cam_topology_cache["edge_count"] = edge_count

    buckets = _cam_topology_cache["lines_by_color"]
    cams_by_state = _cam_topology_cache["cams_by_state"]
    if _cam_topology_cache["edge_count"] == 0:
        return

    import gpu
    from gpu_extras.batch import batch_for_shader
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')

    core = {
        "green":  (0.40, 1.00, 0.40, 0.95),
        "yellow": (1.00, 0.85, 0.25, 0.95),
        "red":    (1.00, 0.30, 0.30, 0.95),
    }
    glow = {
        "green":  (0.20, 0.95, 0.20, 0.35),
        "yellow": (0.95, 0.70, 0.15, 0.30),
        "red":    (1.00, 0.20, 0.20, 0.40),
    }
    halo = {
        "green":  (0.20, 0.95, 0.20, 0.12),
        "yellow": (0.95, 0.70, 0.15, 0.12),
        "red":    (1.00, 0.20, 0.20, 0.18),
    }

    # Lines: 3-pass laser glow. Depth-test NONE so visible edges aren't
    # clipped — LOS raycasts already filtered wall-crossings out.
    gpu.state.depth_test_set('NONE')
    for width, color_dict in ((8.0, halo), (4.0, glow), (2.0, core)):
        gpu.state.line_width_set(width)
        for bucket in ("green", "yellow", "red"):
            segs = buckets.get(bucket, [])
            if not segs:
                continue
            flat = []
            for s, e in segs:
                flat.append(s)
                flat.append(e)
            batch = batch_for_shader(shader, 'LINES', {"pos": flat})
            shader.uniform_float("color", color_dict[bucket])
            batch.draw(shader)

    # Octahedral cam markers — depth-tested so they hide behind walls.
    # (gpu.state.point_size_set is clamped to 1px on some drivers, so we
    # draw real 3D geometry instead.)
    gpu.state.depth_test_set('LESS_EQUAL')
    cam_outer_r = 0.16
    cam_inner_r = 0.085
    oct_v = [
        ( 1.0,  0.0,  0.0), (-1.0,  0.0,  0.0),
        ( 0.0,  1.0,  0.0), ( 0.0, -1.0,  0.0),
        ( 0.0,  0.0,  1.0), ( 0.0,  0.0, -1.0),
    ]
    oct_t = [
        (4, 0, 2), (4, 2, 1), (4, 1, 3), (4, 3, 0),
        (5, 2, 0), (5, 1, 2), (5, 3, 1), (5, 0, 3),
    ]
    for radius_m, color_dict in ((cam_outer_r, halo), (cam_inner_r, core)):
        for state in ("green", "yellow", "red"):
            pts = cams_by_state.get(state, [])
            if not pts:
                continue
            tri_verts = []
            for cx, cy, cz in pts:
                vw = [(cx + vx * radius_m, cy + vy * radius_m, cz + vz * radius_m)
                      for (vx, vy, vz) in oct_v]
                for a, b, c in oct_t:
                    tri_verts.append(vw[a])
                    tri_verts.append(vw[b])
                    tri_verts.append(vw[c])
            batch = batch_for_shader(shader, 'TRIS', {"pos": tri_verts})
            shader.uniform_float("color", color_dict[state])
            batch.draw(shader)

    # Starting-camera highlight — recolor the DEFAULT camera's octahedron
    # MAGENTA (instead of its spacing-state green/yellow/red) so the entry
    # point is unmistakable. Drawn on top at a hair larger radius so it fully
    # covers the underlying state octahedron without z-fighting.
    try:
        default_cam_name = props.enum_default_cam
    except Exception:
        default_cam_name = ""
    if default_cam_name:
        dm = next((m for m in markers if m.name == default_cam_name), None)
        if dm is not None:
            mag_halo = (1.0, 0.12, 0.95, 0.22)
            mag_core = (1.0, 0.12, 0.95, 0.98)
            cx, cy, cz = dm.location.x, dm.location.y, dm.location.z
            for radius_m, mcol in ((cam_outer_r * 1.08, mag_halo),
                                   (cam_inner_r * 1.08, mag_core)):
                vw = [(cx + vx * radius_m, cy + vy * radius_m, cz + vz * radius_m)
                      for (vx, vy, vz) in oct_v]
                tri_verts = []
                for a, b, c in oct_t:
                    tri_verts.append(vw[a])
                    tri_verts.append(vw[b])
                    tri_verts.append(vw[c])
                batch = batch_for_shader(shader, 'TRIS', {"pos": tri_verts})
                shader.uniform_float("color", mcol)
                batch.draw(shader)

    gpu.state.point_size_set(1.0)
    gpu.state.line_width_set(1.0)
    gpu.state.blend_set('NONE')
    gpu.state.depth_test_set('LESS_EQUAL')


# ============================================================================
#  Floor Plan (top-down orthographic render)
# ============================================================================

_FLOOR_PLAN_RES = 2048
_FLOOR_PLAN_CLIP_ABOVE = 0.5
_FLOOR_PLAN_MARGIN = 0.5
# Reject mesh bbox corners more than this far from any camera, so distant
# skydomes/terrain proxies can't blow the frame out to km-scale.
_FLOOR_PLAN_CAM_RADIUS = 30.0


_NUKXON_FLOORPLAN_PREVIEW_CAM_NAME = "_NukxonFloorPlanPreview"

# Tracks the single viewport space the live-preview toggle flipped to
# lock_camera=True, plus that space's prior lock_camera value, so Hide
# restores ONLY that space to ONLY its prior value — instead of blanket-
# clearing lock_camera on every viewport (which clobbered a user's own
# Lock-Camera-to-View setting in unrelated viewports).
_FLOORPLAN_LIVE_LOCKSTATE = None  # dict {space, prior_lock} or None


def _floorplan_collect_cameras(scene, props):
    """Reproduce the export operator's camera collection (line 1607-1617)
    so live preview uses exactly the same source set."""
    cameras = []
    for col in bpy.data.collections:
        if props.enum_collection not in {"Scene Collection", col.name}:
            continue
        for obj in col.objects:
            # `obj not in cameras` dedups markers linked to >1 collection
            if _is_nukxon_camera(obj) and obj not in cameras:
                cameras.append(obj)
    if props.enum_collection == "Scene Collection":
        for obj in scene.objects:
            if _is_nukxon_camera(obj) and obj not in cameras:
                cameras.append(obj)
    return cameras


def _floorplan_compute_geometry(scene, props, cameras):
    """Return dict {center, ortho_scale, clip_start, clip_end} for the
    floor-plan ortho cam, using the SAME math as _render_floor_plan.
    Returns None if no usable geometry. Shared by render path + live
    preview cam updater so they stay in lockstep."""
    if not cameras:
        return None
    collection_name = props.mesh_collection
    use_col = (collection_name and collection_name != "FULL_SCENE"
               and bpy.data.collections.get(collection_name))
    objects = (bpy.data.collections[collection_name].all_objects
               if use_col else scene.objects)
    x_vals, y_vals, z_vals = [], [], []
    for obj in objects:
        if obj.type == 'MESH' and obj.visible_get():
            for c in obj.bound_box:
                w = obj.matrix_world @ Vector(c)
                x_vals.append(w.x)
                y_vals.append(w.y)
                z_vals.append(w.z)
    if not x_vals:
        return None

    cam_xs = [c.location.x for c in cameras]
    cam_ys = [c.location.y for c in cameras]
    clip_x_min = min(cam_xs) - _FLOOR_PLAN_CAM_RADIUS
    clip_x_max = max(cam_xs) + _FLOOR_PLAN_CAM_RADIUS
    clip_y_min = min(cam_ys) - _FLOOR_PLAN_CAM_RADIUS
    clip_y_max = max(cam_ys) + _FLOOR_PLAN_CAM_RADIUS
    kept = [(x, y, z) for x, y, z in zip(x_vals, y_vals, z_vals)
            if clip_x_min <= x <= clip_x_max and clip_y_min <= y <= clip_y_max]
    if kept:
        xk = [t[0] for t in kept]
        yk = [t[1] for t in kept]
        zk = [t[2] for t in kept]
        x_min, x_max = min(xk) - _FLOOR_PLAN_MARGIN, max(xk) + _FLOOR_PLAN_MARGIN
        y_min, y_max = min(yk) - _FLOOR_PLAN_MARGIN, max(yk) + _FLOOR_PLAN_MARGIN
        z_vals = zk
    else:
        x_min, x_max = clip_x_min, clip_x_max
        y_min, y_max = clip_y_min, clip_y_max

    avg_cam_z = sum(c.location.z for c in cameras) / len(cameras)
    slice_above = getattr(props, 'floormap_slice_above_m', _FLOOR_PLAN_CLIP_ABOVE)
    clip_z = avg_cam_z + slice_above
    far_dist = (clip_z - min(z_vals)) + 51.0
    span_x = x_max - x_min
    span_y = y_max - y_min
    # clamp ortho_scale to a sensible floor — a degenerate scene
    # (single vertical plane of mesh, or all geometry coplanar in XY)
    # gives span=0 and Blender would either clamp ortho_scale to 0.01
    # or reject the value outright.
    ortho_scale = max(0.1, max(span_x, span_y))
    center_x = (x_min + x_max) / 2
    center_y = (y_min + y_max) / 2
    cam_z = clip_z + 1.0
    return {
        'center':      (center_x, center_y, cam_z),
        'ortho_scale': ortho_scale,
        'clip_start':  1.0,
        'clip_end':    far_dist,
    }


def _update_floorplan_preview_cam(scene, props):
    """Bound to the slice slider's update callback. If the live-preview
    cam exists, slide it vertically to the new slice height — but
    PRESERVE the user's X/Y pan and ortho_scale zoom so dragging the
    slice doesn't undo their framing. Initial placement happens in the
    toggle operator's show path."""
    cam_obj = scene.objects.get(_NUKXON_FLOORPLAN_PREVIEW_CAM_NAME)
    if cam_obj is None or cam_obj.type != 'CAMERA':
        return
    cameras = _floorplan_collect_cameras(scene, props)
    if len(cameras) < 2:
        return
    geom = _floorplan_compute_geometry(scene, props, cameras)
    if geom is None:
        return
    # Only Z (slice height) + far clip change with the slider; X/Y/zoom
    # belong to the user's framing.
    cur_x, cur_y, _ = cam_obj.location
    _, _, new_z = geom['center']
    cam_obj.location = (cur_x, cur_y, new_z)
    cam_obj.data.clip_end = geom['clip_end']


def _render_floor_plan(output_path):
    """Top-down orthographic floor plan with the ceiling clipped off.
    Manifest carries bounds + ortho scale so the viewer can map world → px."""
    scene = bpy.context.scene
    props = scene.nukxon_props
    cameras_list = post_data.cameras
    if not cameras_list:
        return None

    try:
        collection_name = props.mesh_collection
        use_col = (collection_name and collection_name != "FULL_SCENE"
                   and bpy.data.collections.get(collection_name))
        objects = (bpy.data.collections[collection_name].all_objects
                   if use_col else scene.objects)
        x_vals, y_vals, z_vals = [], [], []
        for obj in objects:
            if obj.type == 'MESH' and obj.visible_get():
                for c in obj.bound_box:
                    w = obj.matrix_world @ Vector(c)
                    x_vals.append(w.x)
                    y_vals.append(w.y)
                    z_vals.append(w.z)
        if not x_vals:
            return None

        cam_xs = [c.location.x for c in cameras_list]
        cam_ys = [c.location.y for c in cameras_list]
        clip_x_min = min(cam_xs) - _FLOOR_PLAN_CAM_RADIUS
        clip_x_max = max(cam_xs) + _FLOOR_PLAN_CAM_RADIUS
        clip_y_min = min(cam_ys) - _FLOOR_PLAN_CAM_RADIUS
        clip_y_max = max(cam_ys) + _FLOOR_PLAN_CAM_RADIUS
        kept = [(x, y, z) for x, y, z in zip(x_vals, y_vals, z_vals)
                if clip_x_min <= x <= clip_x_max and clip_y_min <= y <= clip_y_max]
        if kept:
            xk = [t[0] for t in kept]
            yk = [t[1] for t in kept]
            zk = [t[2] for t in kept]
            x_min, x_max = min(xk) - _FLOOR_PLAN_MARGIN, max(xk) + _FLOOR_PLAN_MARGIN
            y_min, y_max = min(yk) - _FLOOR_PLAN_MARGIN, max(yk) + _FLOOR_PLAN_MARGIN
            z_vals = zk
        else:
            x_min, x_max = clip_x_min, clip_x_max
            y_min, y_max = clip_y_min, clip_y_max

        avg_cam_z = sum(c.location.z for c in cameras_list) / len(cameras_list)
        # User-tunable slice depth; fall back to module default if the
        # property hasn't been registered (older saved files).
        slice_above = getattr(props, 'floormap_slice_above_m', _FLOOR_PLAN_CLIP_ABOVE)
        clip_z = avg_cam_z + slice_above
        # If the slice was dragged below ALL kept geometry, the floor would
        # sit above the near plane and the plan renders black. Clamp the cut
        # to just above the lowest vertex so there's always something to
        # render.
        _z_floor = min(z_vals)
        if clip_z < _z_floor:
            clip_z = _z_floor + 0.1
        far_dist = (clip_z - _z_floor) + 51.0

        # Default: framing comes from auto-computed bounds. If the user
        # locked their own framing via "Set Framing" while Live Preview
        # was active, override center + ortho_scale + aspect with the
        # stored values. Slice height stays auto (driven by the slider).
        if getattr(props, 'floormap_user_framed', False):
            center_x    = props.floormap_user_center_x
            center_y    = props.floormap_user_center_y
            ortho_scale = props.floormap_user_ortho_scale
            aspect      = props.floormap_user_aspect
            # Derive span_x/span_y from ortho_scale (= larger axis) + aspect
            if aspect >= 1.0:
                span_x = ortho_scale
                span_y = ortho_scale / aspect
            else:
                span_y = ortho_scale
                span_x = ortho_scale * aspect
        else:
            span_x = x_max - x_min
            span_y = y_max - y_min
            # floor — see _floorplan_compute_geometry for full rationale
            ortho_scale = max(0.1, max(span_x, span_y))
            center_x = (x_min + x_max) / 2
            center_y = (y_min + y_max) / 2

        # guard against div-by-zero on degenerate spans (single
        # vertical plane mesh gives span_x=0; etc). Floor both spans
        # before the ratio so img dimensions are well-defined.
        span_x_safe = max(span_x, 1e-6)
        span_y_safe = max(span_y, 1e-6)
        if span_x_safe >= span_y_safe:
            img_w = _FLOOR_PLAN_RES
            img_h = max(64, int(_FLOOR_PLAN_RES * span_y_safe / span_x_safe))
        else:
            img_h = _FLOOR_PLAN_RES
            img_w = max(64, int(_FLOOR_PLAN_RES * span_x_safe / span_y_safe))

        cam_z = clip_z + 1.0

        fp_cam_data = bpy.data.cameras.new("NukxonFloorPlanCam")
        fp_cam_data.type = 'ORTHO'
        fp_cam_data.ortho_scale = ortho_scale
        fp_cam_data.clip_start = 1.0
        fp_cam_data.clip_end   = far_dist
        fp_cam = bpy.data.objects.new("NukxonFloorPlanCam", fp_cam_data)
        scene.collection.objects.link(fp_cam)
        fp_cam.location = (center_x, center_y, cam_z)
        fp_cam.rotation_euler = (0, 0, 0)

        # Save EVERYTHING we're about to mutate up front.
        old_camera = scene.camera
        old_res_x = scene.render.resolution_x
        old_res_y = scene.render.resolution_y
        old_res_pct = scene.render.resolution_percentage
        old_fmt = scene.render.image_settings.file_format
        old_quality = scene.render.image_settings.quality
        old_filepath = scene.render.filepath
        # film_transparent so world HDRI doesn't reach the JPG (→ black on encode).
        old_film_transparent = scene.render.film_transparent
        old_samples = None
        if scene.render.engine == 'CYCLES':
            old_samples = scene.cycles.samples

        fp_path = os.path.join(output_path, "floor_plan.jpg")

        # the render + every setting mutation lives in try/FINALLY so that
        # if bpy.ops.render.render() raises (GPU OOM, read-only path, device
        # lost), the scene's render settings are STILL restored and the temp
        # camera + datablock are STILL removed. Previously a throw here left
        # the scene at floor-plan resolution/JPEG/film_transparent + an orphan
        # camera, re-corrupting a scene the main export had already cleaned.
        try:
            if old_samples is not None:
                scene.cycles.samples = 32
            scene.render.film_transparent = True
            scene.camera = fp_cam
            scene.render.resolution_x = img_w
            scene.render.resolution_y = img_h
            scene.render.resolution_percentage = 100
            scene.render.image_settings.file_format = 'JPEG'
            scene.render.image_settings.quality = 90
            scene.render.filepath = fp_path

            bpy.ops.render.render(write_still=True)
        finally:
            def _r(label, fn):
                try:
                    fn()
                except Exception as e:
                    print(f"[Nukxon] Floor plan restore — {label} failed: {e}")
            _r('camera',           lambda: setattr(scene, 'camera', old_camera))
            _r('res_x',            lambda: setattr(scene.render, 'resolution_x', old_res_x))
            _r('res_y',            lambda: setattr(scene.render, 'resolution_y', old_res_y))
            _r('res_pct',          lambda: setattr(scene.render, 'resolution_percentage', old_res_pct))
            _r('fmt',              lambda: setattr(scene.render.image_settings, 'file_format', old_fmt))
            _r('quality',          lambda: setattr(scene.render.image_settings, 'quality', old_quality))
            _r('filepath',         lambda: setattr(scene.render, 'filepath', old_filepath))
            _r('film_transparent', lambda: setattr(scene.render, 'film_transparent', old_film_transparent))
            if old_samples is not None:
                _r('cycles_samples', lambda: setattr(scene.cycles, 'samples', old_samples))
            # Remove temp camera + its datablock (null-guarded).
            try:
                if fp_cam and fp_cam.name in bpy.data.objects:
                    bpy.data.objects.remove(fp_cam, do_unlink=True)
            except Exception:
                pass
            try:
                if fp_cam_data and fp_cam_data.name in bpy.data.cameras:
                    bpy.data.cameras.remove(fp_cam_data)
            except Exception:
                pass

        if not os.path.exists(fp_path):
            return None

        return {
            "file":       "floor_plan.jpg",
            "w":          img_w,
            "h":          img_h,
            "bounds_min": [round(x_min, 4), round(y_min, 4)],
            "bounds_max": [round(x_max, 4), round(y_max, 4)],
            "clip_z":     round(clip_z, 4),
            "ortho_scale": round(ortho_scale, 4),
            "center":     [round(center_x, 4), round(center_y, 4)],
        }

    except Exception as e:
        print(f"[Nukxon] Floor plan failed: {e}")
        import traceback
        traceback.print_exc()
        return None


# ============================================================================
#  JSON Manifest
# ============================================================================

def _write_manifests(output_path):
    """Write `manifest.json` for the .nukxon package.

    Emits raw Blender Z-up RH meters, tagged via the `coords` field, with the
    mesh exported Y-up. The Nukxon platform reads the coords tag and handles
    alignment on its side — the exporter performs no coordinate conversion.
    """
    scene   = bpy.context.scene
    cameras = post_data.cameras
    _face_res = int(scene.nukxon_props.face_resolution)
    _ts = int(datetime.datetime.utcnow().timestamp())

    # NOTE: the scene property is still called "NukxonHotspotProp" for
    # backwards compatibility with existing .blend files. Externally (manifest
    # + UI) these are now called "anchors" — spatial markers the platform uses
    # to wire up cross-project links.
    anchor_objs = [o for o in scene.objects if o.get("NukxonHotspotProp", False)]

    print("[Nukxon] Baking camera visibility...")
    camera_entries = []
    for i, cam in enumerate(cameras):
        raw_names = post_data.pano_filenames.get(i, [])
        pkg_names = [f"cameras/{n}" for n in raw_names]
        init_yaw = round(math.degrees(cam.rotation_euler[2]), 2)

        entry = {
            "id":   i,
            "n":    cam.name,
            "p":    [round(cam.location.x, 4), round(cam.location.y, 4), round(cam.location.z, 4)],
            "yaw":  init_yaw,
            "m":    [round(cam.matrix_world[r][c], 6) for r in range(4) for c in range(4)],
            "vc":   _get_visible_cameras(scene, cam, cameras),
            "vh":   _get_visible_hotspots(scene, cam, anchor_objs),
            "img":  pkg_names,
        }
        camera_entries.append(entry)

    # Anchors (canonical name — was 'hotspots' in older manifests).
    anchors = []
    for obj in anchor_objs:
        anchors.append({
            "id": obj.name.replace(" ", "_").lower(),
            "n":  obj.name,
            "p":  [round(obj.location.x, 4), round(obj.location.y, 4), round(obj.location.z, 4)],
        })

    # Teleport hints (panorama-relative yaw/pitch in degrees; not world coords).
    teleports = []
    for tp in scene.nukxon_teleports:
        if tp.camera:
            cam_idx = next(
                (i for i, c in enumerate(cameras) if c.name == tp.camera.name), None)
            if cam_idx is not None:
                rot = tp.camera.rotation_euler
                teleports.append({
                    "l": tp.label,
                    "c": cam_idx,
                    "y": round(math.degrees(-(rot[2])), 2),
                    "p": round(math.degrees(-(rot[0] - math.radians(90))), 2),
                })

    default_cam_name = scene.nukxon_props.enum_default_cam
    _default_cam_idx = 0
    for i, c in enumerate(camera_entries):
        if c["n"] == default_cam_name:
            _default_cam_idx = i
            break

    _addon_ver = ".".join(str(v) for v in addon_version)
    _host_ver  = ".".join(str(v) for v in bpy.app.version)

    manifest = {
        # Coordinate frame is tagged here; the platform aligns on its side.
        # See _write_manifests docstring.
        "coords":       "blender_zup_meters",

        "nk":           "3.0",
        "type":         "mesh",
        "ts":           _ts,
        "app":          "blender",
        "app_version":  _addon_ver,
        "host_version": _host_ver,
        "engine":       scene.render.engine,
        "proj":         "cubemap",
        "face_order":   CUBEMAP_FACE_NAMES,
        "res":          _face_res,
        "pano_w":       _face_res,
        "pano_h":       _face_res,
        "img_format":   "webp",
        "default_cam":  _default_cam_idx,
        "total_cams":   len(camera_entries),
        "total_faces":  sum(len(c["img"]) for c in camera_entries),
        "total_tp":     len(teleports),
        "total_anchors": len(anchors),
        "mesh":         "mesh.glb",
        "cameras":      camera_entries,
        "teleports":    teleports,
        "anchors":      anchors,
    }

    if post_data.floor_plan_meta:
        manifest["floor_plan"] = post_data.floor_plan_meta

    manifest_path = os.path.join(output_path, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False, separators=(',', ':'))
    print(f"[Nukxon] Manifest: {manifest_path}")


# ============================================================================
#  Package Builder — .nukxon (cubemaps + manifest + mesh + floormap)
# ============================================================================

def _build_mesh_package(output_path):
    """Build the .nukxon zip: manifest.json + mesh.glb + cameras/*.webp +
    floor_plan.jpg. Returns (path, size) on success, None if manifest.json
    hasn't been written yet. Idempotent — overwrites any existing
    {scene_name}.nukxon at the target path."""
    scene_name = get_scene_name()
    glb_path = os.path.join(output_path, "mesh.glb")
    manifest_path = os.path.join(output_path, "manifest.json")
    if not os.path.exists(manifest_path):
        return None

    pkg_path = os.path.join(output_path, f"{scene_name}.nukxon")
    if os.path.exists(pkg_path):
        try:
            os.remove(pkg_path)
        except Exception:
            pass
    print(f"\n[Nukxon] Building {scene_name}.nukxon...")

    with zipfile.ZipFile(pkg_path, 'w', compression=zipfile.ZIP_STORED) as zf:
        zf.write(manifest_path, "manifest.json")
        if os.path.exists(glb_path):
            zf.write(glb_path, "mesh.glb")
        face_count = 0
        for cam_idx, filenames in sorted(post_data.pano_filenames.items()):
            for fname in filenames:
                fpath = os.path.join(output_path, fname)
                if os.path.exists(fpath):
                    zf.write(fpath, f"cameras/{fname}")
                    face_count += 1
        fp_path = os.path.join(output_path, "floor_plan.jpg")
        if os.path.exists(fp_path):
            zf.write(fp_path, "floor_plan.jpg")

    pkg_size = os.path.getsize(pkg_path)
    print(f"[Nukxon]   Cameras: {len(post_data.pano_filenames)}, "
          f"Faces: {face_count}, Size: {pkg_size / (1024*1024):.1f} MB")
    return (pkg_path, pkg_size)


def _build_packages(output_path):
    """Build the .nukxon package + run cleanup. Used by manual reprocess
    flows (NUKXON_OT_process). The normal export path calls
    `_build_mesh_package` directly."""
    glb_path = os.path.join(output_path, "mesh.glb")
    manifest_path = os.path.join(output_path, "manifest.json")

    results = []
    m = _build_mesh_package(output_path)
    if m:
        results.append(("mesh", *m))

    _cleanup_loose_files(output_path, manifest_path, glb_path)
    return results


def _cleanup_loose_files(output_path, manifest_path, glb_path):
    """Remove loose files after successful packaging."""
    try:
        fp_path = os.path.join(output_path, "floor_plan.jpg")
        for p in (manifest_path, glb_path, fp_path):
            if os.path.exists(p):
                os.remove(p)
        for filenames in post_data.pano_filenames.values():
            for fname in filenames:
                fpath = os.path.join(output_path, fname)
                if os.path.exists(fpath):
                    os.remove(fpath)
        print("[Nukxon] Cleaned up loose files")
    except Exception as e:
        print(f"[Nukxon] Warning: cleanup failed: {e}")


# ============================================================================
#  Main Export Operator
# ============================================================================

class NUKXON_OT_export(bpy.types.Operator):
    """Export VR tour renders for Nukxon platform"""
    bl_label  = "Export Nukxon VR"
    bl_idname = "nukxon.export_vr"

    # File dialog properties
    directory: bpy.props.StringProperty(subtype='DIR_PATH')  # type: ignore
    filter_folder: bpy.props.BoolProperty(default=True, options={'HIDDEN'})  # type: ignore

    def invoke(self, context, event):
        props = context.scene.nukxon_props
        if props.export_path:
            self.directory = abs_file_path(props.export_path)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        scene = context.scene
        props = scene.nukxon_props

        cameras = []
        for col in bpy.data.collections:
            if props.enum_collection not in {"Scene Collection", col.name}:
                continue
            for obj in col.objects:
                # dedup markers linked to >1 collection
                if _is_nukxon_camera(obj) and obj not in cameras:
                    cameras.append(obj)
        if props.enum_collection == "Scene Collection":
            for obj in bpy.context.scene.objects:
                if _is_nukxon_camera(obj) and obj not in cameras:
                    cameras.append(obj)

        if len(cameras) < 2:
            bpy.ops.nukxon.messagebox('INVOKE_DEFAULT',
                message=f"ERROR: Need at least 2 cameras — found {len(cameras)} in '{props.enum_collection}'")
            return {'CANCELLED'}

        output_path = self.directory
        if not output_path.endswith(os.sep):
            output_path += os.sep
        props.export_path = output_path
        os.makedirs(output_path, exist_ok=True)

        # Kill stale timers from a cancelled previous run so they don't fire
        # mid-way through this export and write a partial manifest.
        for _stale in (_deferred_restore, _deferred_package):
            try:
                if bpy.app.timers.is_registered(_stale):
                    bpy.app.timers.unregister(_stale)
            except Exception:
                pass

        post_data.reset()
        post_data.output_path   = output_path
        post_data.scene_name    = get_scene_name()
        post_data.cameras       = cameras
        post_data.render_engine = scene.render.engine
        post_data.scene_ref     = scene
        post_data.old_gamma = scene.view_settings.gamma
        post_data.old_use_nodes = scene.use_nodes
        post_data.old_use_compositing = scene.render.use_compositing
        post_data.old_use_extension = scene.render.use_file_extension
        post_data.old_res_x = scene.render.resolution_x
        post_data.old_res_y = scene.render.resolution_y
        post_data.old_res_percent = scene.render.resolution_percentage
        post_data.old_file_format = scene.render.image_settings.file_format
        post_data.old_quality = scene.render.image_settings.quality
        post_data.old_filepath = scene.render.filepath
        post_data.old_frame_start = scene.frame_start
        post_data.old_frame_end = scene.frame_end

        scene.use_nodes = True
        scene.render.use_compositing = True
        scene.render.use_file_extension = True
        scene.render.resolution_percentage = 100
        _res = int(props.face_resolution)
        scene.render.resolution_x = _res
        scene.render.resolution_y = _res
        scene.render.image_settings.file_format = 'WEBP'
        scene.render.image_settings.quality = _CUBEMAP_WEBP_QUALITY

        create_nukxon_camera(cameras, scene)
        scene.frame_set(0)
        scene.frame_start = 0
        scene.frame_end = len(cameras) * 6 - 1
        scene.render.filepath = output_path + get_scene_name()

        prepare_compositor(context, scene)
        create_output_nodes(scene, output_path)

        # GLB export must happen here (full context); render starts via timer.
        glb_path = os.path.join(output_path, "mesh.glb")
        export_mesh_glb(glb_path, props.mesh_collection)

        post_data.frames_processed = 0
        post_data.pano_filenames = {}
        # rendering_active gates _publish_status UI calls — touching window
        # manager between frames emits 140+ context-state-bug warnings.
        post_data.rendering_active = True
        _publish_status("Rendering cubemaps...")

        # Cursor-side progress widget that ticks per cubemap face.
        try:
            context.window_manager.progress_begin(0, len(cameras) * 6)
        except Exception:
            pass

        while nukxon_frame_complete in bpy.app.handlers.render_post:
            bpy.app.handlers.render_post.remove(nukxon_frame_complete)
        while nukxon_render_complete in bpy.app.handlers.render_complete:
            bpy.app.handlers.render_complete.remove(nukxon_render_complete)

        # Photographer addon's frame_change_post handlers fight ours.
        _disabled = []
        for h in list(bpy.app.handlers.frame_change_post):
            if 'photographer' in str(getattr(h, '__module__', '')).lower():
                try:
                    bpy.app.handlers.frame_change_post.remove(h)
                    _disabled.append(h)
                except Exception:
                    pass
        post_data.photographer_handlers = _disabled

        bpy.app.handlers.render_post.append(nukxon_frame_complete)
        bpy.app.handlers.render_complete.append(nukxon_render_complete)

        print(f"\n[Nukxon] Starting export — {len(cameras)} cams × 6 faces "
              f"@ {_res}², engine {scene.render.engine}\n")

        # Retry the render kickoff against the "blend data in use" error —
        # left over when the user cancelled a previous render.
        _attempts = [0]
        def _start_render():
            _attempts[0] += 1
            try:
                bpy.ops.render.render('INVOKE_DEFAULT', animation=True, write_still=True)
                return None
            except RuntimeError as e:
                if "blend data" in str(e) and _attempts[0] < 15:
                    return 0.5
                # Give up: the render never started, so neither render_complete
                # nor render_cancel will ever fire — _deferred_restore won't run.
                # Recover the session ourselves: pull our render handlers, give
                # the Photographer addon its handlers back, end the progress
                # widget, and clear state. Skip floor-plan/manifest/package
                # since no frames were rendered.
                print(f"[Nukxon] Failed to start render: {e}")
                while nukxon_frame_complete in bpy.app.handlers.render_post:
                    bpy.app.handlers.render_post.remove(nukxon_frame_complete)
                while nukxon_render_complete in bpy.app.handlers.render_complete:
                    bpy.app.handlers.render_complete.remove(nukxon_render_complete)
                _restore_photographer_handlers()
                post_data.rendering_active = False
                try:
                    bpy.context.window_manager.progress_end()
                except Exception:
                    pass
                _publish_status("Export failed to start — render busy")
                return None
        bpy.app.timers.register(_start_render, first_interval=0.5)

        return {'FINISHED'}


# ============================================================================
#  Re-scan cubemap files from disk (helper for process-renders operator)
# ============================================================================

def _scan_cubemap_jpgs(output_path, cameras):
    """Populate post_data.pano_filenames by scanning disk for cubemap faces.
    Prefers .webp; falls back to .jpg for older exports. Returns (ok, missing)."""
    post_data.pano_filenames = {}
    missing = []
    for cam_idx, cam in enumerate(cameras):
        post_data.pano_filenames[cam_idx] = []
        cam_name = _safe_name(cam.name)
        for face_idx in range(6):
            face_name = CUBEMAP_FACE_NAMES[face_idx]
            webp_name = f"{cam_name}_{face_name}.webp"
            jpg_name  = f"{cam_name}_{face_name}.jpg"
            webp_path = os.path.join(output_path, webp_name)
            jpg_path  = os.path.join(output_path, jpg_name)
            if os.path.exists(webp_path):
                post_data.pano_filenames[cam_idx].append(webp_name)
            elif os.path.exists(jpg_path):
                post_data.pano_filenames[cam_idx].append(jpg_name)
            else:
                missing.append(webp_name)
    return (len(missing) == 0), missing


# ============================================================================
#  Manual process operator
# ============================================================================

class NUKXON_OT_process(bpy.types.Operator):
    """Process already-rendered files into .nukxon package"""
    bl_label = "Process Renders"
    bl_idname = "nukxon.process_renders"

    def execute(self, context):
        if not post_data.output_path:
            self.report({'ERROR'}, "No export data. Run Export first.")
            return {'CANCELLED'}

        output_path = post_data.output_path
        scene_name  = post_data.scene_name
        num_cameras = len(post_data.cameras)

        for cam_idx in range(num_cameras):
            post_data.pano_filenames[cam_idx] = []
            if cam_idx < len(post_data.cameras):
                # Index-prefixed to avoid _safe_name collisions — must
                # match the render-handler naming in _handle_cubemap_frame.
                cam_name = f"{_safe_name(post_data.cameras[cam_idx].name)}_{cam_idx:02d}"
            else:
                cam_name = f"cam{cam_idx:02d}"
            for face_idx in range(6):
                frame_num = cam_idx * 6 + face_idx
                face_name = CUBEMAP_FACE_NAMES[face_idx]
                base_name = f"{scene_name}{frame_num:04d}"
                src_name = ensure_webp(
                    output_path, base_name, quality=_CUBEMAP_WEBP_QUALITY)
                if src_name:
                    # Rename to explicit face name
                    new_name = f"{cam_name}_{face_name}.webp"
                    old_path = os.path.join(output_path, src_name)
                    new_path = os.path.join(output_path, new_name)
                    if os.path.exists(new_path) and new_path != old_path:
                        os.remove(new_path)
                    os.rename(old_path, new_path)
                    post_data.pano_filenames[cam_idx].append(new_name)

        # Export mesh GLB
        props    = context.scene.nukxon_props
        glb_path = os.path.join(output_path, "mesh.glb")
        export_mesh_glb(glb_path, props.mesh_collection)

        _write_manifests(output_path)
        results = _build_packages(output_path)
        if results:
            for pkg_type, pkg_path, pkg_size in results:
                self.report({'INFO'}, f"{pkg_type}: {os.path.basename(pkg_path)} ({pkg_size / (1024*1024):.1f} MB)")
        _deferred_restore()
        return {'FINISHED'}


# ============================================================================
#  Camera placement tool (click-to-place with visual preview)
# ============================================================================

# Module-level state — survives operator lifecycle, safe for draw callbacks.
_place_state = {
    "hit_pos": None,
    "cam_pos": None,
    "snap_ref": None,
    "snap_origin": None,
    "snap_axis": None,
    "snap_ref2": None,
    "snap_origin2": None,
}
_place_draw_handler = None


def _draw_placement_callback():
    """Draw camera markers + placement preview cursor.
    Colors: cyan = primary snap ref, orange = secondary, white = default cam,
    Nukxon red = regular markers."""
    import gpu
    from gpu_extras.batch import batch_for_shader

    st = _place_state

    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.depth_test_set('NONE')
    gpu.state.blend_set('ALPHA')
    segments = 32

    try:
        scene = bpy.context.scene
        default_cam_name = scene.nukxon_props.enum_default_cam
    except Exception:
        gpu.state.blend_set('NONE')
        gpu.state.depth_test_set('LESS_EQUAL')
        return

    for obj in scene.objects:
        if not _is_nukxon_camera(obj):
            continue
        mx, my, mz = obj.location.x, obj.location.y, obj.location.z
        is_default = (obj.name == default_cam_name)

        snap_ref = st["snap_ref"]
        snap_ref2 = st["snap_ref2"]
        if snap_ref and obj.name == snap_ref:
            ring_col = (0.0, 0.9, 1.0, 0.9)
            dot_col  = (0.0, 0.9, 1.0, 0.8)
            arr_col  = (0.0, 0.9, 1.0, 0.7)
        elif snap_ref2 and obj.name == snap_ref2:
            ring_col = (1.0, 0.7, 0.0, 0.9)
            dot_col  = (1.0, 0.7, 0.0, 0.8)
            arr_col  = (1.0, 0.7, 0.0, 0.7)
        elif is_default:
            # Starting camera — electric magenta so it's unmistakable against
            # bright archviz (white was too easy to lose). Not used elsewhere
            # in the overlay (red=normal, cyan=snap, orange=snap2).
            ring_col = (1.0, 0.12, 0.95, 1.0)
            dot_col  = (1.0, 0.12, 0.95, 0.95)
            arr_col  = (1.0, 0.12, 0.95, 0.9)
        else:
            ring_col = (0.91, 0.0, 0.18, 0.7)
            dot_col  = (0.91, 0.0, 0.18, 0.5)
            arr_col  = (0.91, 0.0, 0.18, 0.6)

        if is_default:
            # Bold magenta diamond framing the starting camera — an eye-catching
            # "this is the entry point" marker. Drawn as a rotated square
            # (up/right/down/left) in the XY plane around the marker.
            gpu.state.line_width_set(3.0)
            d_outer = 0.30
            diamond_verts = [
                (mx, my + d_outer, mz),
                (mx + d_outer, my, mz),
                (mx, my - d_outer, mz),
                (mx - d_outer, my, mz),
                (mx, my + d_outer, mz),  # close the loop
            ]
            batch_dia = batch_for_shader(shader, 'LINE_STRIP', {"pos": diamond_verts})
            shader.uniform_float("color", (1.0, 0.12, 0.95, 0.95))
            batch_dia.draw(shader)

        gpu.state.line_width_set(2.0 if is_default else 1.5)
        ring_r = 0.15
        ring_verts = []
        for i in range(segments + 1):
            a = (2 * math.pi * i) / segments
            ring_verts.append((mx + ring_r * math.cos(a), my + ring_r * math.sin(a), mz))
        batch_ring = batch_for_shader(shader, 'LINE_STRIP', {"pos": ring_verts})
        shader.uniform_float("color", ring_col)
        batch_ring.draw(shader)

        dot_r = 0.05 if is_default else 0.035
        dot_verts = [(mx, my, mz)]
        for i in range(segments + 1):
            a = (2 * math.pi * i) / segments
            dot_verts.append((mx + dot_r * math.cos(a), my + dot_r * math.sin(a), mz))
        batch_dot = batch_for_shader(shader, 'TRI_FAN', {"pos": dot_verts})
        shader.uniform_float("color", dot_col)
        batch_dot.draw(shader)

        yaw = obj.rotation_euler[2]
        fwd_x = math.sin(yaw)
        fwd_y = math.cos(yaw)
        arrow_len = 0.45 if is_default else 0.3
        tip_x = mx + fwd_x * arrow_len
        tip_y = my + fwd_y * arrow_len

        gpu.state.line_width_set(2.5 if is_default else 1.5)
        batch_shaft = batch_for_shader(shader, 'LINES', {"pos": [(mx, my, mz), (tip_x, tip_y, mz)]})
        shader.uniform_float("color", arr_col)
        batch_shaft.draw(shader)

        head_len = 0.10 if is_default else 0.07
        for sign in (-1, 1):
            ha = yaw + math.pi + sign * 0.45
            hx = tip_x + math.sin(ha) * head_len
            hy = tip_y + math.cos(ha) * head_len
            batch_hl = batch_for_shader(shader, 'LINES', {"pos": [(tip_x, tip_y, mz), (hx, hy, mz)]})
            shader.uniform_float("color", arr_col)
            batch_hl.draw(shader)

    # Placement preview (only active while modal op is running).
    hit_pos = st["hit_pos"]
    cam_pos = st["cam_pos"]
    if hit_pos is None or cam_pos is None:
        gpu.state.line_width_set(1.0)
        gpu.state.blend_set('NONE')
        gpu.state.depth_test_set('LESS_EQUAL')
        return

    snap_origin = st["snap_origin"]
    snap_axis = st["snap_axis"]
    if snap_origin is not None:
        ref = snap_origin
        guide_len = 50

        gpu.state.line_width_set(1.0)
        h_line = [(ref.x - guide_len, ref.y, ref.z), (ref.x + guide_len, ref.y, ref.z)]
        batch_h = batch_for_shader(shader, 'LINES', {"pos": h_line})
        shader.uniform_float("color", (0.0, 0.8, 1.0, 0.4) if snap_axis == 'X' else (0.0, 0.8, 1.0, 0.12))
        batch_h.draw(shader)

        v_line = [(ref.x, ref.y - guide_len, ref.z), (ref.x, ref.y + guide_len, ref.z)]
        batch_v = batch_for_shader(shader, 'LINES', {"pos": v_line})
        shader.uniform_float("color", (0.0, 0.8, 1.0, 0.4) if snap_axis == 'Y' else (0.0, 0.8, 1.0, 0.12))
        batch_v.draw(shader)

        conn = [tuple(ref), tuple(cam_pos)]
        batch_conn = batch_for_shader(shader, 'LINES', {"pos": conn})
        shader.uniform_float("color", (0.0, 0.8, 1.0, 0.25))
        batch_conn.draw(shader)

        snap_origin2 = st["snap_origin2"]
        if snap_origin2 is not None:
            ref2 = snap_origin2
            gpu.state.line_width_set(1.0)
            h2 = [(ref2.x - guide_len, ref2.y, ref2.z), (ref2.x + guide_len, ref2.y, ref2.z)]
            batch_h2 = batch_for_shader(shader, 'LINES', {"pos": h2})
            shader.uniform_float("color", (1.0, 0.7, 0.0, 0.4) if snap_axis == 'Y' else (1.0, 0.7, 0.0, 0.12))
            batch_h2.draw(shader)

            v2 = [(ref2.x, ref2.y - guide_len, ref2.z), (ref2.x, ref2.y + guide_len, ref2.z)]
            batch_v2 = batch_for_shader(shader, 'LINES', {"pos": v2})
            shader.uniform_float("color", (1.0, 0.7, 0.0, 0.4) if snap_axis == 'X' else (1.0, 0.7, 0.0, 0.12))
            batch_v2.draw(shader)

            gpu.state.line_width_set(2.5)
            ix, iy, iz = cam_pos.x, cam_pos.y, cam_pos.z
            d = 0.15
            diamond = [
                (ix, iy - d, iz), (ix + d, iy, iz),
                (ix + d, iy, iz), (ix, iy + d, iz),
                (ix, iy + d, iz), (ix - d, iy, iz),
                (ix - d, iy, iz), (ix, iy - d, iz),
            ]
            batch_dm = batch_for_shader(shader, 'LINES', {"pos": diamond})
            shader.uniform_float("color", (1.0, 1.0, 0.0, 0.9))
            batch_dm.draw(shader)

            conn2 = [tuple(ref2), tuple(cam_pos)]
            batch_c2 = batch_for_shader(shader, 'LINES', {"pos": conn2})
            shader.uniform_float("color", (1.0, 0.7, 0.0, 0.25))
            batch_c2.draw(shader)

    gpu.state.line_width_set(1.5)
    batch = batch_for_shader(shader, 'LINES', {"pos": [tuple(hit_pos), tuple(cam_pos)]})
    shader.uniform_float("color", (1.0, 1.0, 1.0, 0.3))
    batch.draw(shader)

    gpu.state.line_width_set(2.5)
    radius = 0.15
    circle_verts = []
    for i in range(segments + 1):
        a = (2 * math.pi * i) / segments
        circle_verts.append((cam_pos.x + radius * math.cos(a),
                             cam_pos.y + radius * math.sin(a), cam_pos.z))
    batch_c = batch_for_shader(shader, 'LINE_STRIP', {"pos": circle_verts})
    shader.uniform_float("color", (0.91, 0.0, 0.18, 0.9))
    batch_c.draw(shader)

    dot_r = 0.04
    dot_verts = [(cam_pos.x, cam_pos.y, cam_pos.z)]
    for i in range(segments + 1):
        a = (2 * math.pi * i) / segments
        dot_verts.append((cam_pos.x + dot_r * math.cos(a),
                          cam_pos.y + dot_r * math.sin(a), cam_pos.z))
    batch_d = batch_for_shader(shader, 'TRI_FAN', {"pos": dot_verts})
    shader.uniform_float("color", (0.91, 0.0, 0.18, 1.0))
    batch_d.draw(shader)

    gpu.state.line_width_set(1.5)
    s = 0.12
    hx, hy, hz = hit_pos.x, hit_pos.y, hit_pos.z
    cross = [(hx - s, hy, hz), (hx + s, hy, hz), (hx, hy - s, hz), (hx, hy + s, hz)]
    batch_x = batch_for_shader(shader, 'LINES', {"pos": cross})
    shader.uniform_float("color", (1.0, 1.0, 1.0, 0.5))
    batch_x.draw(shader)

    gpu.state.line_width_set(1.0)
    gpu.state.blend_set('NONE')
    gpu.state.depth_test_set('LESS_EQUAL')


class NUKXON_OT_place_camera(bpy.types.Operator):
    """Click on surfaces to place VR camera markers at 1.6m eye height.
    Hold Shift near an existing marker to snap horizontally/vertically."""
    bl_idname = "nukxon.place_camera"
    bl_label = "Place Cameras"

    _is_active = False
    _cameras_placed = 0
    _last_placed = None     # fallback snap target when nothing is near the cursor

    def invoke(self, context, event):
        global _place_draw_handler
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "Must be in 3D Viewport")
            return {'CANCELLED'}

        NUKXON_OT_place_camera._is_active = True
        self._cameras_placed = 0
        self._last_placed = None

        for k in _place_state:
            _place_state[k] = None

        _place_draw_handler = bpy.types.SpaceView3D.draw_handler_add(
            _draw_placement_callback, (), 'WINDOW', 'POST_VIEW')

        context.window.cursor_set('CROSSHAIR')
        context.workspace.status_text_set(
            "LMB: Place  |  Shift: Snap row/column  |  Shift + hover 2nd marker: Grid intersection  |  RMB/ESC: Done")
        context.window_manager.modal_handler_add(self)
        context.area.tag_redraw()

        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        # context.area can be None mid-modal (pointer left the editor, area
        # closed/maximized). An unguarded deref here raises, Blender cancels
        # the modal WITHOUT calling _cleanup, and the draw handler + cursor +
        # status leak. Guard it.
        if context.area:
            context.area.tag_redraw()

        # Wrap the whole dispatch: any exception in _update_preview /
        # _create_marker (e.g. region_data None, ray_cast edge cases) would
        # otherwise leak the draw handler the same way. On any error, clean up
        # and end the modal gracefully.
        try:
            if event.type == 'MOUSEMOVE':
                self._update_preview(context, event)
                return {'RUNNING_MODAL'}

            elif event.type in {'LEFT_SHIFT', 'RIGHT_SHIFT'}:
                if event.value == 'PRESS':
                    ref = self._find_nearest_marker(context, event)
                    if ref:
                        _place_state["snap_ref"] = ref.name
                        _place_state["snap_origin"] = Vector(ref.location)
                    elif self._valid_last_placed() is not None:
                        lp = self._valid_last_placed()
                        _place_state["snap_ref"] = lp.name
                        _place_state["snap_origin"] = Vector(lp.location)
                elif event.value == 'RELEASE':
                    _place_state["snap_ref"] = None
                    _place_state["snap_origin"] = None
                    _place_state["snap_axis"] = None
                    _place_state["snap_ref2"] = None
                    _place_state["snap_origin2"] = None
                return {'RUNNING_MODAL'}

            elif event.type == 'LEFTMOUSE' and event.value == 'PRESS':
                if _place_state["cam_pos"] is not None:
                    self._create_marker(context)
                return {'RUNNING_MODAL'}

            # Intercept Ctrl+Z / Ctrl+Y while modal is active. Letting Blender's
            # native undo run frees the placed marker but self._last_placed still
            # points at the freed object — the next snap-ref read dereferences it
            # and Blender crashes. We do our own scoped undo instead.
            elif event.type == 'Z' and event.value == 'PRESS' and event.ctrl:
                self._undo_last_marker(context)
                return {'RUNNING_MODAL'}
            elif event.type == 'Y' and event.value == 'PRESS' and event.ctrl:
                # Swallow redo too — no redo stack inside the modal.
                return {'RUNNING_MODAL'}

            elif event.type in {'RIGHTMOUSE', 'ESC'}:
                self._cleanup(context)
                if self._cameras_placed > 0:
                    self.report({'INFO'}, f"Placed {self._cameras_placed} camera marker(s)")
                return {'FINISHED' if self._cameras_placed > 0 else 'CANCELLED'}

            return {'PASS_THROUGH'}
        except Exception as e:
            print(f"[Nukxon] place_camera modal error — cleaning up: {e}")
            self._cleanup(context)
            return {'CANCELLED'}

    def cancel(self, context):
        # Called by Blender when the modal is force-terminated (file load,
        # another modal grabbing input, etc). Without this the draw handler +
        # cursor + status leak.
        self._cleanup(context)

    def _valid_last_placed(self):
        """Return self._last_placed only if it's still a live Blender object.
        Accessing a freed Python wrapper hard-crashes Blender, so we always
        check via the data-block lookup before dereferencing."""
        lp = self._last_placed
        if lp is None:
            return None
        try:
            name = lp.name  # raises ReferenceError if the wrapper was freed
            if bpy.data.objects.get(name) is lp:
                return lp
        except (ReferenceError, AttributeError):
            pass
        self._last_placed = None
        return None

    def _undo_last_marker(self, context):
        """Remove the most-recently-placed marker, scoped to this modal."""
        lp = self._valid_last_placed()
        if lp is None:
            return
        try:
            bpy.data.objects.remove(lp, do_unlink=True)
        except Exception:
            pass
        self._last_placed = None
        self._cameras_placed = max(0, self._cameras_placed - 1)
        # Clear any snap state that referenced the just-removed marker.
        _place_state["snap_ref"] = None
        _place_state["snap_origin"] = None
        _place_state["snap_axis"] = None
        _place_state["snap_ref2"] = None
        _place_state["snap_origin2"] = None
        context.area.tag_redraw()

    def _find_nearest_marker(self, context, event):
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        region = context.region
        rv3d = context.region_data
        if not region or not rv3d:
            return None
        mouse = Vector((event.mouse_region_x, event.mouse_region_y))
        best_dist = 60
        best_marker = None
        for obj in context.scene.objects:
            if not _is_nukxon_camera(obj):
                continue
            screen_pos = location_3d_to_region_2d(region, rv3d, obj.location)
            if screen_pos:
                dist = (screen_pos - mouse).length
                if dist < best_dist:
                    best_dist = dist
                    best_marker = obj
        return best_marker

    def _find_secondary_marker(self, context, event):
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        region = context.region
        rv3d = context.region_data
        if not region or not rv3d:
            return None
        mouse = Vector((event.mouse_region_x, event.mouse_region_y))
        best_dist = 80
        best_marker = None
        snap_ref_name = _place_state["snap_ref"]
        for obj in context.scene.objects:
            if not _is_nukxon_camera(obj):
                continue
            if snap_ref_name and obj.name == snap_ref_name:
                continue
            screen_pos = location_3d_to_region_2d(region, rv3d, obj.location)
            if screen_pos:
                dist = (screen_pos - mouse).length
                if dist < best_dist:
                    best_dist = dist
                    best_marker = obj
        return best_marker

    def _update_preview(self, context, event):
        from bpy_extras.view3d_utils import region_2d_to_vector_3d, region_2d_to_origin_3d
        region = context.region
        rv3d = context.region_data
        if not region or not rv3d:
            return
        coord = (event.mouse_region_x, event.mouse_region_y)
        origin = region_2d_to_origin_3d(region, rv3d, coord)
        direction = region_2d_to_vector_3d(region, rv3d, coord)
        scene = context.scene
        depsgraph = context.evaluated_depsgraph_get()
        hit, loc, normal, _, _, _ = scene.ray_cast(depsgraph, origin, direction)

        if hit:
            loc = Vector(loc)
            normal = Vector(normal)
            if abs(normal.z) < 0.3:
                floor_hit, floor_loc, _, _, _, _ = scene.ray_cast(
                    depsgraph, Vector((loc.x, loc.y, loc.z)), Vector((0, 0, -1)))
                floor_z = floor_loc.z if floor_hit else 0
            else:
                floor_z = loc.z

            cam_x, cam_y = loc.x, loc.y

            snap_origin = _place_state["snap_origin"]
            if snap_origin is not None:
                ref = snap_origin
                dx = abs(cam_x - ref.x)
                dy = abs(cam_y - ref.y)
                if dx > dy:
                    cam_y = ref.y
                    _place_state["snap_axis"] = 'X'
                else:
                    cam_x = ref.x
                    _place_state["snap_axis"] = 'Y'

                ref2 = self._find_secondary_marker(context, event)
                if ref2:
                    _place_state["snap_ref2"] = ref2.name
                    _place_state["snap_origin2"] = Vector(ref2.location)
                    if _place_state["snap_axis"] == 'X':
                        cam_x = ref2.location.x
                    else:
                        cam_y = ref2.location.y
                else:
                    _place_state["snap_ref2"] = None
                    _place_state["snap_origin2"] = None

            _place_state["hit_pos"] = Vector((cam_x, cam_y, floor_z))
            _place_state["cam_pos"] = Vector((cam_x, cam_y, floor_z + 1.6))
        else:
            _place_state["hit_pos"] = None
            _place_state["cam_pos"] = None

    def _create_marker(self, context):
        cam_pos = _place_state["cam_pos"]
        marker = bpy.data.objects.new("NukxonCam", None)
        marker.empty_display_type = 'PLAIN_AXES'
        marker.empty_display_size = 0.25
        marker.show_name = True
        marker['NukxonCameraMarker'] = True

        props = context.scene.nukxon_props
        col_name = props.enum_collection
        target_col = None
        if col_name and col_name != "Scene Collection":
            target_col = bpy.data.collections.get(col_name)
        if target_col:
            target_col.objects.link(marker)
        else:
            context.scene.collection.objects.link(marker)

        rv3d = context.region_data
        yaw = 0.0
        if rv3d:
            view_fwd = rv3d.view_rotation @ Vector((0, 0, -1))
            fwd_xy = Vector((view_fwd.x, view_fwd.y, 0))
            if fwd_xy.length > 0.001:
                fwd_xy.normalize()
                yaw = math.atan2(fwd_xy.x, fwd_xy.y)

        marker.location = cam_pos
        marker.rotation_euler = (math.radians(90), 0, yaw)

        self._last_placed = marker
        self._cameras_placed += 1

    def _cleanup(self, context):
        global _place_draw_handler
        NUKXON_OT_place_camera._is_active = False
        for k in _place_state:
            _place_state[k] = None
        # Guard every context access — _cleanup can be called from cancel()
        # or the modal exception path where context.area/window may be None.
        if _place_draw_handler is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(_place_draw_handler, 'WINDOW')
            except Exception:
                pass
            _place_draw_handler = None
        try:
            if context.window:
                context.window.cursor_set('DEFAULT')
        except Exception:
            pass
        try:
            if context.workspace:
                context.workspace.status_text_set(None)
        except Exception:
            pass
        try:
            if context.area:
                context.area.tag_redraw()
        except Exception:
            pass


# ============================================================================
#  Hotspot creator
# ============================================================================

# Object name stays "NukxonHotspot" + custom prop "NukxonHotspotProp" for
# backwards compatibility with older .blend files. Manifest field is "anchors"
# (per Contract v1.0). User-facing language is "Project Link" everywhere.

# Offset placed links along the surface normal so the sphere-empty doesn't
# clip into the geometry it was placed on.
_LINK_SURFACE_OFFSET = 0.10


class NUKXON_OT_link_remove(bpy.types.Operator):
    """Remove the selected Project Link"""
    bl_idname = "nukxon.link_remove"
    bl_label = "Remove Project Link"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(o.get("NukxonHotspotProp", False) for o in context.scene.objects)

    def execute(self, context):
        scene = context.scene
        idx = scene.nukxon_link_active_index
        if 0 <= idx < len(scene.objects):
            obj = scene.objects[idx]
            if obj.get("NukxonHotspotProp", False):
                bpy.data.objects.remove(obj, do_unlink=True)
                return {'FINISHED'}
        self.report({'WARNING'}, "No project link selected")
        return {'CANCELLED'}


class NUKXON_OT_add_hotspot(bpy.types.Operator):
    """Click on a surface to place a Project Link marker. The modal exits
    after one placement — click the + button again for another."""
    bl_idname = "nukxon.add_hotspot"
    bl_label = "Nukxon Project Link"
    bl_options = {'REGISTER', 'UNDO'}

    _is_active = False

    def invoke(self, context, event):
        if context.area is None or context.area.type != 'VIEW_3D':
            self.report({'WARNING'}, "Must be in 3D Viewport")
            return {'CANCELLED'}

        NUKXON_OT_add_hotspot._is_active = True
        # cursor_modal_set is the proper modal-cursor API; it auto-restores
        # on modal end and survives PASS_THROUGH events (vs cursor_set which
        # can get reverted between events when nothing consumes MOUSEMOVE).
        context.window.cursor_modal_set('CROSSHAIR')
        context.workspace.status_text_set(
            "Click on a surface to place a project link  |  ESC: Cancel")
        context.window_manager.modal_handler_add(self)
        context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        # Consume MOUSEMOVE so the crosshair cursor stays applied while the
        # modal is running. Without this, the cursor reverts after the first
        # mouse move because PASS_THROUGH lets other handlers reset it.
        if event.type == 'MOUSEMOVE':
            return {'RUNNING_MODAL'}

        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            placed = self._place_at_mouse(context, event)
            self._cleanup(context)
            if placed:
                self.report({'INFO'}, "Project link placed")
                return {'FINISHED'}
            self.report({'WARNING'}, "No surface under cursor")
            return {'CANCELLED'}

        if event.type in {'RIGHTMOUSE', 'ESC'}:
            self._cleanup(context)
            return {'CANCELLED'}

        return {'PASS_THROUGH'}

    def cancel(self, context):
        # Force-termination (file load, addon disable mid-modal) → restore
        # cursor + status + active flag so they don't stay stuck.
        self._cleanup(context)

    def _place_at_mouse(self, context, event):
        from bpy_extras.view3d_utils import region_2d_to_vector_3d, region_2d_to_origin_3d
        region = context.region
        rv3d = context.region_data
        if not region or not rv3d:
            return False
        coord = (event.mouse_region_x, event.mouse_region_y)
        origin = region_2d_to_origin_3d(region, rv3d, coord)
        direction = region_2d_to_vector_3d(region, rv3d, coord)
        scene = context.scene
        depsgraph = context.evaluated_depsgraph_get()
        hit, loc, normal, _, _, _ = scene.ray_cast(depsgraph, origin, direction)
        if not hit:
            return False
        pos = Vector(loc) + Vector(normal) * _LINK_SURFACE_OFFSET

        # Default outliner name is "Link" (auto-incremented to Link.001 etc).
        # Custom prop tag stays "NukxonHotspotProp" for backwards compat with
        # older .blend files. Manifest field is "anchors" per Contract v1.0.
        obj = bpy.data.objects.new("Link", None)
        scene.collection.objects.link(obj)
        obj.location = pos
        obj.empty_display_size = 0.4
        obj.empty_display_type = 'SPHERE'
        obj['NukxonHotspotProp'] = True
        return True

    def _cleanup(self, context):
        NUKXON_OT_add_hotspot._is_active = False
        try:
            context.window.cursor_modal_restore()
        except Exception:
            pass
        try:
            context.workspace.status_text_set(None)
        except Exception:
            pass
        if context.area:
            context.area.tag_redraw()


# ============================================================================
#  UI Operators
# ============================================================================

_NUKXON_PREVIEW_CAM_NAME = "_NukxonStartingPreview"

# Tracks the viewport space the preview-start toggle flipped to
# lock_camera=True, plus its prior value, so Exit restores ONLY that space
# to ONLY its prior value (same scoped pattern as the floor-plan toggle).
_PREVIEW_START_LOCKSTATE = None


def _preview_start_is_active(scene):
    """True if we're currently in Preview Start mode (preview cam exists and
    is the active scene camera)."""
    cam = scene.objects.get(_NUKXON_PREVIEW_CAM_NAME)
    return cam is not None and cam.type == 'CAMERA' and scene.camera is cam


class NUKXON_OT_preview_default_camera(bpy.types.Operator):
    """Toggle the entry-camera preview. ENTER: switch to a 90° camera matching
    the default marker so you see the viewer's entry direction — you can ROTATE
    to look around but the camera can't move from the entry point. EXIT: leave
    camera view and restore your previous camera."""
    bl_idname  = "nukxon.preview_default_camera"
    bl_label   = "Preview Starting Camera"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        # Allow Exit even with no default cam set; require a default cam to Enter.
        if _preview_start_is_active(context.scene):
            return True
        return bool(getattr(context.scene.nukxon_props, "enum_default_cam", "") or "")

    def execute(self, context):
        global _PREVIEW_START_LOCKSTATE
        scene = context.scene

        # ── EXIT path — already in preview, tear it down ──────────────
        if _preview_start_is_active(scene):
            preview_cam = scene.objects.get(_NUKXON_PREVIEW_CAM_NAME)
            old_name = preview_cam.get('_nukxon_old_scene_cam', '') if preview_cam else ''
            if scene.camera is preview_cam:
                scene.camera = scene.objects.get(old_name) if old_name else None
            # Restore the flipped viewport: drop camera view + restore prior lock.
            st = _PREVIEW_START_LOCKSTATE
            if st is not None:
                sp = st.get('space')
                try:
                    if sp and sp.region_3d.view_perspective == 'CAMERA':
                        sp.region_3d.view_perspective = 'PERSP'
                except Exception:
                    pass
                try:
                    if sp:
                        sp.lock_camera = st.get('prior_lock', False)
                except Exception:
                    pass
                _PREVIEW_START_LOCKSTATE = None
            # Remove the preview cam + its datablock.
            if preview_cam is not None:
                cam_data = preview_cam.data
                try:
                    bpy.data.objects.remove(preview_cam, do_unlink=True)
                except Exception:
                    pass
                try:
                    if cam_data and cam_data.users == 0 and cam_data.name in bpy.data.cameras:
                        bpy.data.cameras.remove(cam_data)
                except Exception:
                    pass
            self.report({'INFO'}, "Exited entry preview")
            return {'FINISHED'}

        # ── ENTER path ────────────────────────────────────────────────
        cam_name = scene.nukxon_props.enum_default_cam or ""
        marker = scene.objects.get(cam_name)
        if not marker:
            self.report({'ERROR'}, f"Default camera '{cam_name}' not found")
            return {'CANCELLED'}

        preview_cam = scene.objects.get(_NUKXON_PREVIEW_CAM_NAME)
        if preview_cam is None or preview_cam.type != 'CAMERA':
            if preview_cam is not None:
                bpy.data.objects.remove(preview_cam, do_unlink=True)
            cam_data = bpy.data.cameras.new(_NUKXON_PREVIEW_CAM_NAME)
            cam_data.lens_unit  = 'FOV'
            cam_data.angle      = radians(90)
            cam_data.clip_start = 0.01
            preview_cam = bpy.data.objects.new(_NUKXON_PREVIEW_CAM_NAME, cam_data)
            preview_cam["NukxonPreviewCam"] = True
            preview_cam.hide_render = True
            scene.collection.objects.link(preview_cam)

        preview_cam.matrix_world = marker.matrix_world.copy()
        if scene.camera is not preview_cam:
            preview_cam['_nukxon_old_scene_cam'] = scene.camera.name if scene.camera else ''
        scene.camera = preview_cam

        # Lock LOCATION (not rotation) so the user can orbit to look around the
        # entry point but the camera can't pan/dolly away from it — mirrors how
        # the viewer anchors you at a hotspot. Clear any prior constraint first.
        for c in list(preview_cam.constraints):
            if c.type == 'LIMIT_LOCATION':
                preview_cam.constraints.remove(c)
        loc = preview_cam.location
        con = preview_cam.constraints.new('LIMIT_LOCATION')
        con.use_min_x = True
        con.use_max_x = True
        con.min_x = loc.x
        con.max_x = loc.x
        con.use_min_y = True
        con.use_max_y = True
        con.min_y = loc.y
        con.max_y = loc.y
        con.use_min_z = True
        con.use_max_z = True
        con.min_z = loc.z
        con.max_z = loc.z
        con.use_transform_limit = True
        con.owner_space = 'WORLD'

        target_space = None
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        target_space = space
                        break
                if target_space:
                    break
        if target_space is None:
            self.report({'WARNING'}, "No 3D viewport found")
            return {'CANCELLED'}
        target_space.region_3d.view_perspective = 'CAMERA'
        # lock_camera so viewport orbit drives the camera (rotation only — the
        # LIMIT_LOCATION constraint pins position). Record prior value to restore.
        try:
            prior_lock = target_space.lock_camera
            target_space.lock_camera = True
            _PREVIEW_START_LOCKSTATE = {'space': target_space, 'prior_lock': prior_lock}
        except Exception:
            pass

        self.report({'INFO'}, f"Entry preview for {cam_name} — orbit to look around, click Exit Preview to leave")
        return {'FINISHED'}


class NUKXON_OT_toggle_floorplan_live(bpy.types.Operator):
    """Toggle the floor-plan live preview: creates an orthographic camera
    positioned at the slice depth, switches the viewport to camera view,
    and the slice slider then drives the cut LIVE. Press again to remove
    the camera and return to normal viewport."""
    bl_idname  = "nukxon.toggle_floorplan_live"
    bl_label   = "Live Preview"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return getattr(context.scene, 'nukxon_props', None) is not None

    def execute(self, context):
        global _FLOORPLAN_LIVE_LOCKSTATE
        scene = context.scene
        props = scene.nukxon_props

        existing = scene.objects.get(_NUKXON_FLOORPLAN_PREVIEW_CAM_NAME)

        # Only treat as "live preview present" if it's actually a camera
        # — guards against name collisions with any user-created object
        # that happens to share the reserved name.
        if existing is not None and existing.type == 'CAMERA':
            old_cam_attr = existing.get('_nukxon_old_scene_cam', '')
            if scene.camera is existing:
                restore = scene.objects.get(old_cam_attr) if old_cam_attr else None
                scene.camera = restore  # may be None — fine

            # Capture the camera datablock BEFORE removing the object so we
            # can also remove the datablock. bpy.data.objects.remove()
            # only drops the object, leaving cam_data orphaned otherwise —
            # each Show/Hide cycle would leak one bpy.data.cameras entry.
            cam_data = existing.data
            try:
                bpy.data.objects.remove(existing, do_unlink=True)
            except Exception:
                pass
            try:
                if cam_data and cam_data.users == 0 and cam_data.name in bpy.data.cameras:
                    bpy.data.cameras.remove(cam_data)
            except Exception:
                pass

            # Restore ONLY the space we flipped, to ONLY its prior lock_camera
            # value — never blanket-clear lock_camera on unrelated viewports
            # (that clobbered a user's own Lock-Camera-to-View setting).
            st = _FLOORPLAN_LIVE_LOCKSTATE
            if st is not None:
                sp = st.get('space')
                try:
                    if sp and sp.region_3d.view_perspective == 'CAMERA':
                        sp.region_3d.view_perspective = 'PERSP'
                except Exception:
                    pass
                try:
                    if sp:
                        sp.lock_camera = st.get('prior_lock', False)
                except Exception:
                    pass
                _FLOORPLAN_LIVE_LOCKSTATE = None
            else:
                # State lost (e.g. addon reloaded mid-preview): drop any
                # camera-view back to PERSP but DON'T touch lock_camera since
                # we no longer know each viewport's prior value.
                for scr in bpy.data.screens:
                    for area in scr.areas:
                        if area.type != 'VIEW_3D':
                            continue
                        for space in area.spaces:
                            if space.type != 'VIEW_3D':
                                continue
                            try:
                                if space.region_3d.view_perspective == 'CAMERA':
                                    space.region_3d.view_perspective = 'PERSP'
                            except Exception:
                                pass

            self.report({'INFO'}, "Floor plan live preview off")
            return {'FINISHED'}

        # Show path — need cameras + computed geometry.
        cameras = _floorplan_collect_cameras(scene, props)
        if len(cameras) < 2:
            self.report({'ERROR'},
                f"Need 2+ cameras for floor plan — found {len(cameras)} "
                f"in '{props.enum_collection}'")
            return {'CANCELLED'}

        geom = _floorplan_compute_geometry(scene, props, cameras)
        if geom is None:
            self.report({'ERROR'}, "Couldn't compute floor plan geometry (no visible mesh?)")
            return {'CANCELLED'}

        # If the user has LOCKED a framing (Set Framing), seed the preview cam
        # from THAT framing so re-entering Live Preview shows what the export
        # will actually render — not the auto-computed default. Only X/Y center
        # + ortho_scale come from the lock; Z (slice height) + clip planes stay
        # slice-driven from geom.
        cx, cy, cz = geom['center']
        ortho = geom['ortho_scale']
        if getattr(props, 'floormap_user_framed', False):
            cx = props.floormap_user_center_x
            cy = props.floormap_user_center_y
            ortho = props.floormap_user_ortho_scale

        cam_data = bpy.data.cameras.new(_NUKXON_FLOORPLAN_PREVIEW_CAM_NAME)
        cam_data.type        = 'ORTHO'
        cam_data.ortho_scale = ortho
        cam_data.clip_start  = geom['clip_start']
        cam_data.clip_end    = geom['clip_end']
        cam_obj = bpy.data.objects.new(_NUKXON_FLOORPLAN_PREVIEW_CAM_NAME, cam_data)
        cam_obj.location       = (cx, cy, cz)
        cam_obj.rotation_euler = (0, 0, 0)
        cam_obj.hide_render    = True
        cam_obj['_nukxon_old_scene_cam'] = scene.camera.name if scene.camera else ''
        scene.collection.objects.link(cam_obj)
        scene.camera = cam_obj

        # Block orbit/tilt: with lock_camera_to_view on, viewport rotation
        # would tilt the cam off top-down. The Limit Rotation constraint
        # pinned to (0,0,0) snaps any attempted rotation back to identity
        # — viewport pan + scroll-zoom still work; orbit is dead.
        con = cam_obj.constraints.new('LIMIT_ROTATION')
        con.use_limit_x = True
        con.min_x = 0
        con.max_x = 0
        con.use_limit_y = True
        con.min_y = 0
        con.max_y = 0
        con.use_limit_z = True
        con.min_z = 0
        con.max_z = 0
        con.use_transform_limit = True  # affects interactive transforms, not just final eval
        con.owner_space = 'WORLD'

        # Find the first 3D viewport, switch to camera view, AND enable
        # lock_camera_to_view so the user's pan/scroll-zoom drive the cam
        # directly. Without it the viewport navigation only zooms the
        # viewport's projection — the cam itself doesn't move.
        target_space = None
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        target_space = space
                        break
                if target_space:
                    break
        if target_space is not None:
            target_space.region_3d.view_perspective = 'CAMERA'
            # Record the space + its prior lock_camera so Hide restores
            # exactly this one space to exactly its prior value.
            try:
                prior_lock = target_space.lock_camera
                target_space.lock_camera = True
                _FLOORPLAN_LIVE_LOCKSTATE = {'space': target_space, 'prior_lock': prior_lock}
            except Exception:
                pass
        else:
            self.report({'WARNING'}, "Live preview on, but no 3D viewport to focus")

        self.report({'INFO'},
            f"Live preview on — Shift+MMB to pan, scroll to zoom, slider to slice "
            f"(currently {props.floormap_slice_above_m:+.2f} m)")
        return {'FINISHED'}


class NUKXON_OT_set_floorplan_framing(bpy.types.Operator):
    """Lock the floor-plan render to the live-preview camera's CURRENT
    framing — captures center + ortho scale + viewport aspect. Slice
    height stays controlled by the slider. Use "Use Auto" to revert to
    the auto-computed bounds."""
    bl_idname  = "nukxon.set_floorplan_framing"
    bl_label   = "Set Framing"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        scene = context.scene
        if getattr(scene, 'nukxon_props', None) is None:
            return False
        # don't allow Set Framing during an in-flight Preview Minimap
        # render — scene.render.resolution is temporarily set to the
        # floor-plan dims, so the captured aspect would be wrong.
        if _FLOORPLAN_PREVIEW_PENDING is not None:
            return False
        # ensure the named object is actually a camera, not a stray
        # object that happens to share the reserved name.
        cam = scene.objects.get(_NUKXON_FLOORPLAN_PREVIEW_CAM_NAME)
        return cam is not None and cam.type == 'CAMERA'

    def execute(self, context):
        scene = context.scene
        props = scene.nukxon_props
        cam_obj = scene.objects.get(_NUKXON_FLOORPLAN_PREVIEW_CAM_NAME)
        if cam_obj is None or cam_obj.type != 'CAMERA':
            self.report({'ERROR'}, "Live preview not active — turn it on first")
            return {'CANCELLED'}

        # Aspect = render-resolution aspect (= the camera frame the user
        # actually sees inside the viewport). The 3D viewport area's own
        # aspect would be wrong: a wide viewport over a square render
        # resolution would falsely lock in a landscape framing even
        # though the user framed the scene inside a square camera frame.
        # Pixel aspect kept at 1:1 (we don't touch render.pixel_aspect_*).
        rx = max(1, scene.render.resolution_x)
        ry = max(1, scene.render.resolution_y)
        aspect = rx / ry

        props.floormap_user_center_x    = cam_obj.location.x
        props.floormap_user_center_y    = cam_obj.location.y
        props.floormap_user_ortho_scale = cam_obj.data.ortho_scale
        props.floormap_user_aspect      = aspect
        props.floormap_user_framed      = True

        self.report({'INFO'},
            f"Framing locked: center=({cam_obj.location.x:.2f}, "
            f"{cam_obj.location.y:.2f}), scale={cam_obj.data.ortho_scale:.2f} m, "
            f"aspect={aspect:.2f}")
        return {'FINISHED'}


class NUKXON_OT_clear_floorplan_framing(bpy.types.Operator):
    """Drop the user-set framing override and return to auto-computed
    bounds (camera positions + visible mesh bbox)."""
    bl_idname  = "nukxon.clear_floorplan_framing"
    bl_label   = "Use Auto"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = getattr(context.scene, 'nukxon_props', None)
        return props is not None and props.floormap_user_framed

    def execute(self, context):
        context.scene.nukxon_props.floormap_user_framed = False
        self.report({'INFO'}, "Floor plan framing reset to auto bounds")
        return {'FINISHED'}


# Module-level state for the async floor-plan preview render. Lives
# OUTSIDE the operator class because Blender's app handlers prefer
# plain functions (classmethod handlers can be fragile across Python
# session reloads, and the @persistent decorator only applies to
# module-level functions).
_FLOORPLAN_PREVIEW_PENDING = None


@bpy.app.handlers.persistent
def _floorplan_preview_on_render_done(*_args):
    """Render handlers fire on the RENDER JOB THREAD — calling
    bpy.data.objects.remove() from there crashes Blender's depsgraph
    (BKE_collections_object_remove_invalids access violation). So we
    just schedule the real teardown on the main thread via a timer."""
    def _on_main():
        _floorplan_preview_finish(success=True)
        return None  # one-shot
    try:
        bpy.app.timers.register(_on_main, first_interval=0)
    except Exception as e:
        print(f"[Nukxon] failed to schedule floor-plan teardown: {e}")


@bpy.app.handlers.persistent
def _floorplan_preview_on_render_cancel(*_args):
    def _on_main():
        _floorplan_preview_finish(success=False)
        return None
    try:
        bpy.app.timers.register(_on_main, first_interval=0)
    except Exception as e:
        print(f"[Nukxon] failed to schedule floor-plan cancel: {e}")


def _floorplan_preview_finish(success):
    """Idempotent teardown: restores scene render settings, removes the
    temp render camera, loads the rendered JPG into the Image Editor.
    Safe to call multiple times (state guard at top)."""
    global _FLOORPLAN_PREVIEW_PENDING
    state = _FLOORPLAN_PREVIEW_PENDING
    if state is None:
        return
    _FLOORPLAN_PREVIEW_PENDING = None

    # Drop every handler we may have registered, across every list. The
    # belt-and-suspenders sweep guards against Blender having moved a
    # handler between lists or against a previous teardown attempt
    # having partially completed.
    for h in (_floorplan_preview_on_render_done,
              _floorplan_preview_on_render_cancel):
        for hl in (bpy.app.handlers.render_complete,
                   bpy.app.handlers.render_post,
                   bpy.app.handlers.render_write,
                   bpy.app.handlers.render_cancel):
            while h in hl:
                hl.remove(h)

    # Use the stored scene ref (in case context.scene drifted in a
    # multi-scene file) and otherwise fall back to the active scene.
    scene = state.get('scene') or bpy.context.scene

    # restore each setting in its OWN try so a dangling old_camera
    # reference (e.g. user toggled Live Preview off mid-render and the
    # old scene.camera was the live preview cam, now removed) doesn't
    # short-circuit resolution + format restoration. Validate the
    # camera reference defensively before assignment.
    def _safe(label, fn):
        try:
            fn()
        except Exception as e:
            print(f"[Nukxon] floor plan preview restore — {label} failed: {e}")

    def _restore_camera():
        cam = state.get('old_camera')
        if cam is None:
            scene.camera = None
            return
        try:
            still_valid = cam.name in bpy.data.objects
        except Exception:
            still_valid = False
        scene.camera = cam if still_valid else None

    _safe('camera',           _restore_camera)
    _safe('res_x',            lambda: setattr(scene.render, 'resolution_x', state['old_res_x']))
    _safe('res_y',            lambda: setattr(scene.render, 'resolution_y', state['old_res_y']))
    _safe('res_pct',          lambda: setattr(scene.render, 'resolution_percentage', state['old_res_pct']))
    _safe('fmt',              lambda: setattr(scene.render.image_settings, 'file_format', state['old_fmt']))
    _safe('quality',          lambda: setattr(scene.render.image_settings, 'quality', state['old_quality']))
    _safe('filepath',         lambda: setattr(scene.render, 'filepath', state['old_filepath']))
    _safe('film_transparent', lambda: setattr(scene.render, 'film_transparent', state['old_film_transparent']))
    if state['old_samples'] is not None:
        _safe('cycles_samples', lambda: setattr(scene.cycles, 'samples', state['old_samples']))

    try:
        if state['fp_cam'].name in bpy.data.objects:
            bpy.data.objects.remove(state['fp_cam'], do_unlink=True)
    except Exception:
        pass
    try:
        if state['fp_cam_data'].name in bpy.data.cameras:
            bpy.data.cameras.remove(state['fp_cam_data'])
    except Exception:
        pass

    post_data.cameras = state['saved_post_cameras']
    print(f"[Nukxon] floor plan preview teardown ok "
          f"(restored res={state['old_res_x']}x{state['old_res_y']})")

    if not success:
        return

    fp_path = state['fp_path']
    if not os.path.exists(fp_path):
        return

    img_name = "Nukxon Floor Plan Preview"
    old = bpy.data.images.get(img_name)
    if old is not None:
        bpy.data.images.remove(old)
    img = bpy.data.images.load(fp_path, check_existing=False)
    img.name = img_name

    target_area = None
    for area in bpy.context.screen.areas:
        if area.type == 'IMAGE_EDITOR':
            target_area = area
            break
    if target_area is None:
        candidates = [a for a in bpy.context.screen.areas
                      if a.type not in {'VIEW_3D', 'PROPERTIES'}]
        if candidates:
            largest = max(candidates, key=lambda a: a.width * a.height)
            largest.type = 'IMAGE_EDITOR'
            target_area = largest
    if target_area is not None:
        for space in target_area.spaces:
            if space.type == 'IMAGE_EDITOR':
                space.image = img
                break


class NUKXON_OT_preview_floor_plan(bpy.types.Operator):
    """Render the floor plan with the current slice depth and open it in
    Blender's Image Editor. The render runs in Blender's normal render
    window so progress is visible and the UI stays responsive (async via
    INVOKE_DEFAULT + render_post / render_complete handlers)."""
    bl_idname  = "nukxon.preview_floor_plan"
    bl_label   = "Preview Minimap"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        if _FLOORPLAN_PREVIEW_PENDING is not None:
            return False   # block re-entry while a preview is rendering
        return getattr(context.scene, 'nukxon_props', None) is not None

    # ── execute: stage state, INVOKE the render, return ──────────────
    def execute(self, context):
        scene = context.scene
        props = scene.nukxon_props

        # Collect cameras — same logic as the export operator (line 1607).
        cameras = []
        for col in bpy.data.collections:
            if props.enum_collection not in {"Scene Collection", col.name}:
                continue
            for obj in col.objects:
                # dedup markers linked to >1 collection
                if _is_nukxon_camera(obj) and obj not in cameras:
                    cameras.append(obj)
        if props.enum_collection == "Scene Collection":
            for obj in bpy.context.scene.objects:
                if _is_nukxon_camera(obj) and obj not in cameras:
                    cameras.append(obj)

        if len(cameras) < 2:
            self.report({'ERROR'},
                f"Need 2+ cameras for floor plan — found {len(cameras)} "
                f"in '{props.enum_collection}'")
            return {'CANCELLED'}

        # ── Geometry — duplicates _render_floor_plan's setup math so we
        #    can keep the export path's sync render untouched ─────────
        collection_name = props.mesh_collection
        use_col = (collection_name and collection_name != "FULL_SCENE"
                   and bpy.data.collections.get(collection_name))
        objects = (bpy.data.collections[collection_name].all_objects
                   if use_col else scene.objects)
        x_vals, y_vals, z_vals = [], [], []
        for obj in objects:
            if obj.type == 'MESH' and obj.visible_get():
                for c in obj.bound_box:
                    w = obj.matrix_world @ Vector(c)
                    x_vals.append(w.x)
                    y_vals.append(w.y)
                    z_vals.append(w.z)
        if not x_vals:
            self.report({'ERROR'}, "No visible mesh geometry found for floor plan")
            return {'CANCELLED'}

        cam_xs = [c.location.x for c in cameras]
        cam_ys = [c.location.y for c in cameras]
        clip_x_min = min(cam_xs) - _FLOOR_PLAN_CAM_RADIUS
        clip_x_max = max(cam_xs) + _FLOOR_PLAN_CAM_RADIUS
        clip_y_min = min(cam_ys) - _FLOOR_PLAN_CAM_RADIUS
        clip_y_max = max(cam_ys) + _FLOOR_PLAN_CAM_RADIUS
        kept = [(x, y, z) for x, y, z in zip(x_vals, y_vals, z_vals)
                if clip_x_min <= x <= clip_x_max and clip_y_min <= y <= clip_y_max]
        if kept:
            xk = [t[0] for t in kept]
            yk = [t[1] for t in kept]
            zk = [t[2] for t in kept]
            x_min, x_max = min(xk) - _FLOOR_PLAN_MARGIN, max(xk) + _FLOOR_PLAN_MARGIN
            y_min, y_max = min(yk) - _FLOOR_PLAN_MARGIN, max(yk) + _FLOOR_PLAN_MARGIN
            z_vals = zk
        else:
            x_min, x_max = clip_x_min, clip_x_max
            y_min, y_max = clip_y_min, clip_y_max

        avg_cam_z = sum(c.location.z for c in cameras) / len(cameras)
        slice_above = getattr(props, 'floormap_slice_above_m', _FLOOR_PLAN_CLIP_ABOVE)
        clip_z = avg_cam_z + slice_above
        far_dist = (clip_z - min(z_vals)) + 51.0

        # User-framing override mirrors _render_floor_plan
        if getattr(props, 'floormap_user_framed', False):
            center_x    = props.floormap_user_center_x
            center_y    = props.floormap_user_center_y
            ortho_scale = props.floormap_user_ortho_scale
            aspect      = props.floormap_user_aspect
            if aspect >= 1.0:
                span_x = ortho_scale
                span_y = ortho_scale / aspect
            else:
                span_y = ortho_scale
                span_x = ortho_scale * aspect
        else:
            span_x = x_max - x_min
            span_y = y_max - y_min
            # floor — see _floorplan_compute_geometry for full rationale
            ortho_scale = max(0.1, max(span_x, span_y))
            center_x = (x_min + x_max) / 2
            center_y = (y_min + y_max) / 2

        # guard against div-by-zero on degenerate spans (single
        # vertical plane mesh gives span_x=0; etc). Floor both spans
        # before the ratio so img dimensions are well-defined.
        span_x_safe = max(span_x, 1e-6)
        span_y_safe = max(span_y, 1e-6)
        if span_x_safe >= span_y_safe:
            img_w = _FLOOR_PLAN_RES
            img_h = max(64, int(_FLOOR_PLAN_RES * span_y_safe / span_x_safe))
        else:
            img_h = _FLOOR_PLAN_RES
            img_w = max(64, int(_FLOOR_PLAN_RES * span_x_safe / span_y_safe))

        cam_z = clip_z + 1.0

        # Temp camera + render state save
        fp_cam_data = bpy.data.cameras.new("NukxonFloorPlanPreviewRenderCam")
        fp_cam_data.type        = 'ORTHO'
        fp_cam_data.ortho_scale = ortho_scale
        fp_cam_data.clip_start  = 1.0
        fp_cam_data.clip_end    = far_dist
        fp_cam = bpy.data.objects.new("NukxonFloorPlanPreviewRenderCam", fp_cam_data)
        scene.collection.objects.link(fp_cam)
        fp_cam.location       = (center_x, center_y, cam_z)
        fp_cam.rotation_euler = (0, 0, 0)

        old_samples = None
        if scene.render.engine == 'CYCLES':
            old_samples = scene.cycles.samples
            scene.cycles.samples = 32

        preview_dir = bpy.app.tempdir
        fp_path = os.path.join(preview_dir, "floor_plan.jpg")

        state = {
            'scene':               scene,
            'fp_cam':              fp_cam,
            'fp_cam_data':         fp_cam_data,
            'fp_path':             fp_path,
            'old_camera':          scene.camera,
            'old_res_x':           scene.render.resolution_x,
            'old_res_y':           scene.render.resolution_y,
            'old_res_pct':         scene.render.resolution_percentage,
            'old_fmt':             scene.render.image_settings.file_format,
            'old_quality':         scene.render.image_settings.quality,
            'old_filepath':        scene.render.filepath,
            'old_film_transparent': scene.render.film_transparent,
            'old_samples':         old_samples,
            'saved_post_cameras':  post_data.cameras,
        }

        post_data.cameras = cameras
        scene.render.film_transparent = True
        scene.camera = fp_cam
        scene.render.resolution_x = img_w
        scene.render.resolution_y = img_h
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = 'JPEG'
        scene.render.image_settings.quality = 90
        scene.render.filepath = fp_path

        # Park pending state + register handlers. render_write is the
        # most reliable signal for write_still=True jobs (fires AFTER
        # the file lands on disk). render_complete is the backup for
        # the rare case render_write is skipped. The teardown is
        # idempotent so duplicate fires are harmless. render_cancel
        # covers the user pressing Esc in the render window.
        global _FLOORPLAN_PREVIEW_PENDING
        _FLOORPLAN_PREVIEW_PENDING = state
        bpy.app.handlers.render_write.append(_floorplan_preview_on_render_done)
        bpy.app.handlers.render_complete.append(_floorplan_preview_on_render_done)
        bpy.app.handlers.render_cancel.append(_floorplan_preview_on_render_cancel)

        # INVOKE_DEFAULT opens Blender's modal render window — user sees
        # progress, UI stays responsive. We return FINISHED immediately;
        # the handlers do teardown + image load when the render lands.
        try:
            bpy.ops.render.render('INVOKE_DEFAULT', write_still=True)
        except Exception as e:
            _floorplan_preview_finish(success=False)
            self.report({'ERROR'}, f"Failed to start render: {e}")
            return {'CANCELLED'}

        self.report({'INFO'},
            f"Rendering floor plan ({img_w}x{img_h}) — watch the render window")
        return {'FINISHED'}


class NUKXON_OT_set_entry_from_view(bpy.types.Operator):
    """Set the default marker's rotation to match the current viewport — the
    direction the viewer will face on entry."""
    bl_idname  = "nukxon.set_entry_from_view"
    bl_label   = "Set Entry From View"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bool(getattr(context.scene.nukxon_props, "enum_default_cam", "") or "")

    def execute(self, context):
        scene = context.scene
        cam_name = scene.nukxon_props.enum_default_cam or ""
        marker = scene.objects.get(cam_name)
        if not marker:
            self.report({'ERROR'}, f"Default camera '{cam_name}' not found")
            return {'CANCELLED'}

        view_rot = None
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        view_rot = space.region_3d.view_rotation
                        break
                if view_rot is not None:
                    break
        if view_rot is None:
            self.report({'WARNING'}, "No 3D viewport found")
            return {'CANCELLED'}

        marker.rotation_mode = 'XYZ'
        marker.rotation_euler = view_rot.to_euler('XYZ')
        self.report({'INFO'}, f"Entry direction for {cam_name} set from view")
        return {'FINISHED'}


class NUKXON_OT_toggle_denoiser(bpy.types.Operator):
    """Toggle the Nukxon component-wise OIDN denoiser on/off"""
    bl_idname = "nukxon.toggle_denoiser"
    bl_label  = "Nukxon Denoiser"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.nukxon_props
        props.use_denoiser = not props.use_denoiser
        state = "ON" if props.use_denoiser else "OFF"
        self.report({'INFO'}, f"Nukxon Denoiser {state}")
        return {'FINISHED'}


class NUKXON_OT_optimized_render(bpy.types.Operator):
    """Apply Nukxon's archviz-tuned Cycles settings — fast + clean at 25 SPP
    (assumes the Nukxon Denoiser handles noise). Forces engine to Cycles."""
    bl_idname  = "nukxon.optimized_render"
    bl_label   = "Optimized Render Settings"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene  = context.scene
        render = scene.render
        was_other_engine = render.engine != 'CYCLES'
        if was_other_engine:
            prev_engine = render.engine
            render.engine = 'CYCLES'

        cycles = scene.cycles
        try:
            scene.cycles.use_preview_denoising = False
            render.compositor_device = 'GPU'
        except AttributeError:
            pass

        cycles.use_adaptive_sampling   = True
        cycles.adaptive_threshold      = 0.1
        cycles.samples                 = 25
        cycles.adaptive_min_samples    = 0
        cycles.time_limit              = 0
        cycles.use_denoising           = False

        cycles.max_bounces             = 12
        cycles.diffuse_bounces         = 4
        cycles.glossy_bounces          = 4
        cycles.transmission_bounces    = 12
        cycles.volume_bounces          = 0
        cycles.transparent_max_bounces = 8

        cycles.sample_clamp_direct     = 0.0
        cycles.sample_clamp_indirect   = 10.0
        cycles.caustics_reflective     = False
        cycles.caustics_refractive     = False

        try:
            cycles.use_auto_tile       = False
        except AttributeError:
            pass

        render.use_persistent_data     = True

        if was_other_engine:
            self.report({'INFO'},
                f"Optimized settings applied (engine switched {prev_engine} → CYCLES)")
        else:
            self.report({'INFO'}, "Optimized render settings applied")
        return {'FINISHED'}


# ============================================================================
#  UI Panel
# ============================================================================

class NUKXON_PT_main_panel(bpy.types.Panel):
    bl_label = "Nukxon VR Exporter"
    bl_idname = "NUKXON_PT_main_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Nukxon"
    bl_context = "objectmode"

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT'

    def draw(self, context):
        layout = self.layout
        layout.use_property_decorate = False
        props = context.scene.nukxon_props
        scene = context.scene
        num_cams = props.get_camera_count(context)
        engine = scene.render.engine

        box = layout.box()
        box.row().label(text="Scene Setup", icon='SCENE_DATA')

        col = box.column(align=True)
        col.prop(props, "enum_collection", text="Cameras", icon="COLLECTION_NEW")
        col.prop(props, "enum_default_cam", text="Starting Camera",
                 icon='ERROR' if num_cams < 2 else 'CAMERA_DATA')

        col = box.column(align=True)
        col.prop(props, "face_resolution", text="Quality", icon="IMAGE_PLANE")
        col.separator(factor=0.5)
        col.prop(props, "mesh_collection", text="Mesh", icon="OUTLINER_COLLECTION")

        if props.mesh_collection and props.mesh_collection != "FULL_SCENE":
            mesh_col = bpy.data.collections.get(props.mesh_collection)
            if mesh_col is None:
                box.label(text="Collection not found!", icon='ERROR')
            else:
                mesh_count = sum(1 for o in mesh_col.all_objects
                                 if o.type in {'MESH','CURVE','SURFACE','META','FONT'})
                if mesh_count == 0:
                    box.label(text="No mesh objects in collection", icon='ERROR')

        if num_cams < 2:
            box.label(text=f"Need 2+ cameras (found {num_cams})", icon='ERROR')

        if num_cams >= 2:
            col = box.column(align=True)
            col.prop(props, "show_cam_topology", icon='OUTLINER_OB_LATTICE')
            if props.show_cam_topology:
                cache = _cam_topology_cache
                if cache.get("edge_count", 0) > 0:
                    cams = cache["cams_by_state"]
                    n_red = len(cams["red"])
                    n_yel = len(cams["yellow"])
                    n_grn = len(cams["green"])
                    total = n_red + n_yel + n_grn
                    if n_red == 0 and n_yel == 0:
                        box.label(text=f"All {total} cameras well-spaced",
                                  icon='CHECKMARK')
                    elif n_red > 0:
                        box.label(text=f"{n_grn} good / {n_yel} off / {n_red} bad cameras",
                                  icon='ERROR')
                    else:
                        box.label(text=f"{n_grn} good / {n_yel} off cameras",
                                  icon='INFO')

        box.separator(factor=0.5)
        row = box.row(align=True)
        row.scale_y = 1.5 if num_cams < 2 else 1.3
        is_placing = NUKXON_OT_place_camera._is_active
        row.operator(
            "nukxon.place_camera",
            text="Placing... (ESC to finish)" if is_placing else "Place Cameras",
            icon='CURSOR',
            depress=is_placing)

        if num_cams >= 1:
            in_preview = _preview_start_is_active(scene)
            row = box.row(align=True)
            row.operator(
                "nukxon.preview_default_camera",
                text="Exit Preview" if in_preview else "Preview Start",
                icon='PANEL_CLOSE' if in_preview else 'HIDE_OFF',
                depress=in_preview)
            row.operator("nukxon.set_entry_from_view", text="Set From View", icon='RESTRICT_VIEW_OFF')

        box = layout.box()
        box.row().label(text="Render Settings", icon='RENDER_STILL')

        row = box.row(align=True)
        row.operator(
            "nukxon.toggle_denoiser",
            text="Denoiser ON" if props.use_denoiser else "Denoiser OFF",
            icon='SHADERFX',
            depress=props.use_denoiser,
        )
        row.operator("nukxon.optimized_render", text="Optimized", icon='SETTINGS')

        if props.use_denoiser and engine != 'CYCLES':
            box.label(text="Denoiser requires Cycles engine", icon='ERROR')

        # Low samples without the denoiser → grainy. Warn the user (the
        # Optimized preset assumes Denoiser is on).
        if engine == 'CYCLES':
            try:
                cur_samples = int(scene.cycles.samples)
            except (AttributeError, ValueError):
                cur_samples = None
            if (cur_samples is not None
                    and cur_samples <= 64
                    and not props.use_denoiser):
                box.label(
                    text=f"Low samples ({cur_samples}) — enable Denoiser for clean output",
                    icon='ERROR')

        box = layout.box()
        box.row().label(text="Floor Plan", icon='IMAGE_DATA')
        col = box.column(align=True)
        col.prop(props, "floormap_slice_above_m", text="Slice Above Cams")

        live_cam = scene.objects.get(_NUKXON_FLOORPLAN_PREVIEW_CAM_NAME)
        # only count a real camera as "live preview on" — guards
        # against any unrelated object sharing the reserved name.
        live_on = live_cam is not None and live_cam.type == 'CAMERA'

        row = box.row(align=True)
        row.scale_y = 1.2
        row.enabled = num_cams >= 2
        row.operator("nukxon.toggle_floorplan_live",
                     text=("Hide Live Preview" if live_on else "Show Live Preview"),
                     icon=('HIDE_ON' if live_on else 'HIDE_OFF'),
                     depress=live_on)
        row.operator("nukxon.preview_floor_plan",
                     text="Preview Minimap" if num_cams >= 2 else "Need 2+ cameras",
                     icon='RENDER_RESULT')

        # Direct camera controls — only when live preview is up. Bound
        # straight to the cam object/data so Blender's prop system
        # handles two-way sync with the viewport (Shift+MMB pan, scroll
        # zoom). No custom update callbacks needed.
        if live_on and live_cam.type == 'CAMERA':
            ctrl = box.column(align=True)
            ctrl.label(text="Framing Controls:", icon='VIEW_CAMERA')
            ctrl.prop(live_cam, "location", index=0, text="Pan X")
            ctrl.prop(live_cam, "location", index=1, text="Pan Y")
            ctrl.prop(live_cam.data, "ortho_scale", text="Zoom")

        # Framing override row — Set Framing (active only while Live
        # Preview is showing) + Use Auto (active only when a framing is
        # currently set). Status label shows which mode the render will
        # use so the user is never confused about what gets exported.
        framed = props.floormap_user_framed
        frame_row = box.row(align=True)
        frame_row.scale_y = 1.0
        sub = frame_row.row(align=True)
        sub.enabled = live_on
        # "Re-Set Framing" label only makes sense when both (a) LP is
        # on AND (b) a framing is currently locked. Otherwise fall back to
        # plain "Set Framing" so the greyed-out button doesn't show a
        # confusing label when LP is off.
        sub.operator("nukxon.set_floorplan_framing",
                     text=("Re-Set Framing" if (framed and live_on) else "Set Framing"),
                     icon='OBJECT_DATA',
                     depress=framed)
        sub2 = frame_row.row(align=True)
        sub2.enabled = framed
        sub2.operator("nukxon.clear_floorplan_framing",
                      text="Use Auto",
                      icon='FILE_REFRESH')

        box.label(
            text=("Framing: LOCKED (export uses your view)" if framed
                  else "Framing: auto (export fits cameras + mesh)"),
            icon='LOCKED' if framed else 'AUTO',
        )

        box = layout.box()
        row = box.row()
        row.label(text="Teleport Points", icon='TRACKER')
        row.operator("nukxon.teleport_add", text="", icon='ADD')

        if len(scene.nukxon_teleports) > 0:
            box.template_list(
                "NUKXON_UL_teleport_list", "",
                scene, "nukxon_teleports",
                scene, "nukxon_teleport_index",
                rows=3,
            )

            row = box.row(align=True)
            op = row.operator("nukxon.teleport_move", text="", icon='TRIA_UP')
            op.direction = 'UP'
            op = row.operator("nukxon.teleport_move", text="", icon='TRIA_DOWN')
            op.direction = 'DOWN'
            row.operator("nukxon.teleport_remove", text="", icon='REMOVE')

            if scene.nukxon_teleport_index < len(scene.nukxon_teleports):
                tp = scene.nukxon_teleports[scene.nukxon_teleport_index]
                col = box.column(align=True)
                col.prop(tp, "label")
                col.prop(tp, "camera")

        else:
            box.label(text="No teleport points", icon='INFO')

        hotspot_objs = [o for o in context.scene.objects if o.get("NukxonHotspotProp", False)]

        box = layout.box()
        row = box.row()
        row.label(text="Project Linking", icon='PROP_CON')
        placing = NUKXON_OT_add_hotspot._is_active
        row.operator(
            "nukxon.add_hotspot",
            text="Placing... (ESC)" if placing else "",
            icon='ADD',
            depress=placing)

        if hotspot_objs:
            box.template_list(
                "NUKXON_UL_link_list", "",
                scene, "objects",
                scene, "nukxon_link_active_index",
                rows=3,
            )
            row = box.row(align=True)
            row.operator("nukxon.link_remove", text="", icon='REMOVE')
        else:
            box.label(text="No project links", icon='INFO')

        layout.separator(factor=1.0)
        box = layout.box()
        row = box.row()
        row.scale_y = 2.0
        row.enabled = num_cams >= 2
        row.operator("nukxon.export_vr", text="Export", icon='EXPORT')

        if post_data.status:
            if "Rendering" in post_data.status:
                status_icon = 'RENDER_ANIMATION'
            elif "Packaging" in post_data.status:
                status_icon = 'PACKAGE'
            elif "complete" in post_data.status:
                status_icon = 'CHECKMARK'
            else:
                status_icon = 'TIME'
            box.label(text=post_data.status, icon=status_icon)

        # Last export path
        if props.export_path:
            path = props.export_path.replace("\\", "/")
            parts = [p for p in path.split("/") if p]
            short = (".../" + "/".join(parts[-2:])) if len(parts) > 2 else path
            box.label(text=short, icon='FILE_FOLDER')

        if num_cams >= 2:
            cubemap_faces = num_cams * 6
            box.label(
                text=f"{num_cams} cams × 6 faces = {cubemap_faces} renders",
                icon='INFO')


# ============================================================================
#  Add menu
# ============================================================================

class NUKXON_MT_submenu(bpy.types.Menu):
    bl_label = "Nukxon"
    bl_idname = "OBJECT_MT_nukxon_menu"

    def draw(self, context):
        self.layout.operator("nukxon.add_hotspot", icon='PROP_CON')


def nukxon_parent_menu(self, context):
    self.layout.menu(NUKXON_MT_submenu.bl_idname, icon='OBJECT_ORIGIN')


# ============================================================================
#  Registration
# ============================================================================

classes = (
    NukxonTeleportPoint,
    NUKXON_UL_teleport_list,
    NUKXON_UL_link_list,
    NUKXON_OT_teleport_add,
    NUKXON_OT_teleport_remove,
    NUKXON_OT_teleport_move,
    NukxonProperties,
    NUKXON_OT_messagebox,
    NUKXON_OT_preview_default_camera,
    NUKXON_OT_preview_floor_plan,
    NUKXON_OT_toggle_floorplan_live,
    NUKXON_OT_set_floorplan_framing,
    NUKXON_OT_clear_floorplan_framing,
    NUKXON_OT_set_entry_from_view,
    NUKXON_OT_toggle_denoiser,
    NUKXON_OT_optimized_render,
    NUKXON_OT_export,
    NUKXON_OT_process,
    NUKXON_OT_place_camera,
    NUKXON_OT_add_hotspot,
    NUKXON_OT_link_remove,
    NUKXON_PT_main_panel,
    NUKXON_MT_submenu,
)


def _nukxon_render_cancel(scene):
    """Cancel handler — strip our render handlers and restore the scene.
    Fires on the RENDER JOB THREAD, so it must NOT touch the window manager
    directly (progress_end walks wm->windows + tags redraws → C-level access
    violation off-main). Handler-list ops + post_data writes are thread-safe;
    everything that touches the WM or bpy.data is deferred to a main-thread
    timer."""
    if nukxon_frame_complete in bpy.app.handlers.render_post:
        bpy.app.handlers.render_post.remove(nukxon_frame_complete)
    if nukxon_render_complete in bpy.app.handlers.render_complete:
        bpy.app.handlers.render_complete.remove(nukxon_render_complete)
    # Clear rendering_active first so _publish_status actually updates the UI.
    post_data.rendering_active = False
    _publish_status("Export cancelled")
    # progress_end() must run on the main thread — defer via a one-shot timer
    # instead of calling it inline on the render job thread. Runs regardless
    # of whether output_path is set (so the cursor progress widget always
    # closes on cancel).
    def _end_progress():
        try:
            bpy.context.window_manager.progress_end()
        except Exception:
            pass
        return None
    try:
        bpy.app.timers.register(_end_progress, first_interval=0)
    except Exception:
        pass
    if post_data.output_path:
        bpy.app.timers.register(_deferred_restore, first_interval=1.0)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.nukxon_props = bpy.props.PointerProperty(type=NukxonProperties)
    bpy.types.Scene.nukxon_teleports = bpy.props.CollectionProperty(type=NukxonTeleportPoint)
    bpy.types.Scene.nukxon_teleport_index = bpy.props.IntProperty(name="Active Teleport", default=0)
    bpy.types.Scene.nukxon_link_active_index = bpy.props.IntProperty(name="Active Project Link", default=0)
    bpy.types.VIEW3D_MT_add.append(nukxon_parent_menu)
    bpy.app.handlers.render_cancel.append(_nukxon_render_cancel)

    global _cam_topology_handle
    _cam_topology_handle = bpy.types.SpaceView3D.draw_handler_add(
        _draw_cam_topology, (), 'WINDOW', 'POST_VIEW')


def _floorplan_unregister_cleanup():
    """Tear down everything the floor-plan / live-preview feature may
    have spun up: persistent render handlers, the pending render state,
    every screen's lock_camera flag, every scene's live-preview camera
    (object + datablock). Called from unregister() so disabling the
    addon (or reloading scripts) doesn't leak handlers pointing at a
    module that's about to disappear."""
    # 1. Drop our render handlers from every list, every time. @persistent
    #    handlers survive script reloads, which means a stale registration
    #    pointing at a vanished module will crash when the next render
    #    fires.
    for h in (_floorplan_preview_on_render_done,
              _floorplan_preview_on_render_cancel):
        for hl in (bpy.app.handlers.render_complete,
                   bpy.app.handlers.render_post,
                   bpy.app.handlers.render_write,
                   bpy.app.handlers.render_cancel):
            while h in hl:
                hl.remove(h)

    # 2. Clear the pending state — if a render is in flight when the
    #    addon is disabled, its deferred timer will see PENDING=None and
    #    no-op gracefully instead of touching a vanished module.
    global _FLOORPLAN_PREVIEW_PENDING, _FLOORPLAN_LIVE_LOCKSTATE, _PREVIEW_START_LOCKSTATE
    _FLOORPLAN_PREVIEW_PENDING = None
    _PREVIEW_START_LOCKSTATE = None

    # 3. Restore lock_camera ONLY on the single space we flipped, to its
    #    prior value (never blanket-clear unrelated viewports). If state is
    #    lost, leave lock_camera alone — we don't know prior values.
    st = _FLOORPLAN_LIVE_LOCKSTATE
    if st is not None:
        sp = st.get('space')
        try:
            if sp:
                if sp.region_3d.view_perspective == 'CAMERA':
                    sp.region_3d.view_perspective = 'PERSP'
        except Exception:
            pass
        try:
            if sp:
                sp.lock_camera = st.get('prior_lock', False)
        except Exception:
            pass
        _FLOORPLAN_LIVE_LOCKSTATE = None

    # 4. Sweep every scene for ANY temp camera the addon may have left behind
    #    (live-preview, starting-preview, floor-plan render cams). For each,
    #    restore the user's prior scene.camera (stored on the object) before
    #    removing the object + its datablock.
    _temp_cam_names = (
        _NUKXON_FLOORPLAN_PREVIEW_CAM_NAME,   # live preview
        _NUKXON_PREVIEW_CAM_NAME,             # 'Preview Starting Camera'
        "NukxonFloorPlanCam",                 # export-path floor-plan render cam
        "NukxonFloorPlanPreviewRenderCam",    # async preview render cam
    )
    for scn in bpy.data.scenes:
        for cam_name in _temp_cam_names:
            obj = scn.objects.get(cam_name)
            if obj is None:
                continue
            cam_data = obj.data if obj.type == 'CAMERA' else None
            # Restore the user's prior active camera if this temp cam stole it.
            try:
                if scn.camera is obj:
                    old_name = obj.get('_nukxon_old_scene_cam', '')
                    scn.camera = scn.objects.get(old_name) if old_name else None
            except Exception:
                pass
            try:
                bpy.data.objects.remove(obj, do_unlink=True)
            except Exception:
                pass
            try:
                if cam_data and cam_data.users == 0 and cam_data.name in bpy.data.cameras:
                    bpy.data.cameras.remove(cam_data)
            except Exception:
                pass


def unregister():
    global _cam_topology_handle
    if _cam_topology_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_cam_topology_handle, 'WINDOW')
        _cam_topology_handle = None
    _cam_topology_cache["sig"] = None
    _cam_topology_cache["lines_by_color"] = {"green": [], "yellow": [], "red": []}
    _cam_topology_cache["cams_by_state"] = {"green": [], "yellow": [], "red": []}
    _cam_topology_cache["edge_count"] = 0

    # Tear down floor-plan handlers + scene state BEFORE unregistering
    # classes, so the helper still has access to its module-level globals.
    try:
        _floorplan_unregister_cleanup()
    except Exception as e:
        print(f"[Nukxon] floor-plan unregister cleanup failed: {e}")

    # sweep the MAIN export render handlers. They're appended at export
    # start and only removed on render completion/cancel — disabling the addon
    # or reloading scripts mid-render would otherwise leave them registered,
    # pointing into a dead module, crashing the next render frame.
    while nukxon_frame_complete in bpy.app.handlers.render_post:
        bpy.app.handlers.render_post.remove(nukxon_frame_complete)
    while nukxon_render_complete in bpy.app.handlers.render_complete:
        bpy.app.handlers.render_complete.remove(nukxon_render_complete)

    # sweep the place-camera modal's draw handler + reset its active flag.
    # The modal removes this only on ESC/RMB; disable/reload while placing
    # would leave a stale POST_VIEW callback firing into a vanished module
    # every viewport redraw.
    global _place_draw_handler
    if _place_draw_handler is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_place_draw_handler, 'WINDOW')
        except Exception:
            pass
        _place_draw_handler = None
    try:
        NUKXON_OT_place_camera._is_active = False
        NUKXON_OT_add_hotspot._is_active = False
    except Exception:
        pass

    if _nukxon_render_cancel in bpy.app.handlers.render_cancel:
        bpy.app.handlers.render_cancel.remove(_nukxon_render_cancel)
    bpy.types.VIEW3D_MT_add.remove(nukxon_parent_menu)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.nukxon_link_active_index
    del bpy.types.Scene.nukxon_teleport_index
    del bpy.types.Scene.nukxon_teleports
    del bpy.types.Scene.nukxon_props


if __name__ == "__main__":
    register()
