"""N-panel UI in the 3D Viewport sidebar ("blmn.ai" tab).

Mirrors the web Render tool settings bar: model, scene type, style, sliders,
resolution, seed. Account connection lives in the add-on Preferences; the
panel only shows a short status line with credits.
"""
import os
import textwrap
import bpy
import bpy.utils.previews
from bpy.types import Panel

from . import properties as props
from . import history
from . import net
from . import operators
from . import utils
from . import addon_updater_ops

# Number of recent renders shown (with thumbnails) in the History panel.
HISTORY_VISIBLE = 6

# Preview collection backing the History thumbnails. Loading from disk here
# (rather than bpy.data.images.load) keeps result images out of the .blend.
_thumbs = None


def _thumb_icon_id(result_path):
    """Return an icon_id for a result image's thumbnail, or 0 if unavailable."""
    if _thumbs is None or not result_path:
        return 0
    abspath = bpy.path.abspath(result_path)
    if not os.path.isfile(abspath):
        return 0
    entry = _thumbs.get(abspath)
    if entry is None:
        try:
            entry = _thumbs.load(abspath, abspath, "IMAGE")
        except (KeyError, RuntimeError):
            return 0
    return entry.icon_id


def _status_icon(status):
    return {
        props.STATUS_IDLE: "CHECKMARK",
        props.STATUS_FINISHED: "CHECKMARK",
        props.STATUS_FAILED: "ERROR",
    }.get(status, "SORTTIME")


class BLMN_PT_main(Panel):
    bl_label = "blmn.ai"
    bl_idname = "BLMN_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "blmn.ai"

    def draw(self, context):
        layout = self.layout
        sp = context.scene.blmn_ai
        prefs = utils.prefs(context)
        busy = sp.is_busy()

        # --- Update notice (only drawn when a newer release is available) ---
        addon_updater_ops.update_notice_box_ui(self, context)

        # --- Account ---
        box = layout.box()
        if prefs.connected():
            # Load the balance the first time we have a token but no figure yet
            # (e.g. just after a Blender restart). Self-throttled.
            if net.ACCOUNT.get("balance") is None:
                operators.kick_credits_refresh(context)
            row = box.row(align=True)
            balance = net.ACCOUNT.get("balance")
            credits_text = "Credits: {0}".format(balance) if balance is not None else "Credits: …"
            row.label(text=credits_text, icon="USER")
            row.operator("blmn.refresh_credits", text="", icon="FILE_REFRESH")
            if prefs.account_email:
                box.label(text=prefs.account_email)
        else:
            box.label(text="Not connected", icon="ERROR")
            col = box.column(align=True)
            col.label(text="1. Get a one-time code")
            col.label(text="2. Paste it below and Link")
            box.operator("blmn.open_connect_page", icon="URL")
            row = box.row(align=True)
            row.prop(prefs, "connect_code", text="Code")
            row.operator("blmn.link_account", text="", icon="LINKED")
            return

        # --- Input ---
        box = layout.box()
        box.label(text="Capture", icon="OUTLINER_OB_CAMERA")
        box.prop(sp, "source", text="")
        # One visual bar of preview target, camera helper, and capture.
        row = box.row(align=True)
        row.enabled = not busy
        row.operator("blmn.pick_preview_area", text="", icon="EYEDROPPER",
                     depress=operators.BLMN_OT_pick_preview_area.is_active())
        row.operator("blmn.camera_from_view", text="", icon="VIEW_CAMERA")
        row.operator("blmn.capture_preview", text="Preview", icon="HIDE_OFF")

        # --- Model + settings ---
        box = layout.box()
        box.label(text="Render Settings", icon="SHADERFX")
        box.prop(sp, "model", text="Model")
        row = box.row(align=True)
        row.prop(sp, "render_type", expand=True)
        box.prop(sp, "image_style", text="Style")
        if sp.supports_resolution():
            row = box.row(align=True)
            row.prop(sp, "resolution", expand=True)
        elif sp.model == "light":
            box.prop(sp, "hd")

        col = box.column(align=True)
        col.prop(sp, "creativity", slider=True)
        if sp.render_type == "exterior":
            col.prop(sp, "environment_fill", slider=True)
        else:
            col.prop(sp, "decoration_fill", slider=True)
        box.prop(sp, "seed")

        # --- Prompt ---
        box = layout.box()
        row = box.row(align=True)
        row.label(text="Prompt (optional)", icon="GREASEPENCIL")
        row.operator("blmn.edit_prompt", text="", icon="GREASEPENCIL")
        prompt_col = box.column()
        prompt_col.scale_y = 1.35
        prompt_col.prop(sp, "prompt", text="")
        if sp.prompt:
            _draw_wrapped_prompt(box, sp.prompt)
            sub = box.row()
            sub.alignment = "RIGHT"
            sub.label(text="{0} / {1}".format(len(sp.prompt), props.PROMPT_MAX))

        # --- Reference images (optional) ---
        box = layout.box()
        header = box.row(align=True)
        header.label(text="Reference Images (optional)", icon="IMAGE_REFERENCE")
        header.label(text="{0}/{1}".format(len(sp.references), props.MAX_REFERENCES))
        for i, ref in enumerate(sp.references):
            name = os.path.basename(bpy.path.abspath(ref.path)) if ref.path else "(image)"
            r = box.row(align=True)
            r.label(text=name, icon="FILE_IMAGE")
            rm = r.operator("blmn.remove_reference", text="", icon="X")
            rm.index = i
        if len(sp.references) < props.MAX_REFERENCES:
            box.operator("blmn.add_reference", text="Add Reference", icon="ADD")

        # --- Generate ---
        preview_ready = operators._preview_capture_exists(sp)
        col = layout.column()
        col.scale_y = 1.5
        col.enabled = not busy and preview_ready
        cost = sp.estimated_credits()
        cost_label = "Generate ({0} credit{1})".format(cost, "" if cost == 1 else "s") \
            if cost else "Generate (no credits)"
        col.operator("blmn.generate_render", text=cost_label, icon="SHADERFX")
        if not preview_ready:
            row = layout.row()
            row.active = False
            row.label(text="Click Preview before Generate", icon="INFO")

        # --- Status ---
        row = layout.row()
        row.label(text=sp.status_label(), icon=_status_icon(sp.status))
        if busy:
            row.label(text="Esc to cancel")
        if sp.status == props.STATUS_FAILED and sp.status_message:
            layout.label(text=sp.status_message, icon="INFO")

        # --- Result ---
        result_img = _result_image(sp)
        if result_img is not None:
            box = layout.box()
            box.label(text="Latest Result", icon="IMAGE_DATA")
            _draw_image_preview(box, result_img)
            row = box.row(align=True)
            row.operator("blmn.view_result", icon="WORKSPACE")
            row.operator("blmn.open_output_folder", text="Folder", icon="FILE_FOLDER")


class BLMN_PT_history(Panel):
    bl_label = "History"
    bl_idname = "BLMN_PT_history"
    bl_parent_id = "BLMN_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "blmn.ai"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context):
        self.layout.operator("blmn.refresh_history", text="", icon="FILE_REFRESH")

    def draw(self, context):
        layout = self.layout
        entries = _safe_history(context)
        if not entries:
            layout.label(text="No renders yet")
            return
        for entry in entries[:HISTORY_VISIBLE]:
            _draw_history_entry(layout, entry)
        layout.label(text="Full history: blmn.ai → Library", icon="URL")


def _draw_wrapped_prompt(layout, prompt):
    box = layout.box()
    col = box.column(align=True)
    for line in textwrap.wrap(prompt.strip(), width=34) or [""]:
        col.label(text=line)


def _draw_history_entry(layout, entry):
    """One history row: thumbnail + model + prompt (or 'No prompt') + View."""
    result_path = entry.get("result_path", "")
    has_result = bool(result_path) and os.path.isfile(bpy.path.abspath(result_path))

    box = layout.box()
    row = box.row()

    icon_id = _thumb_icon_id(result_path) if has_result else 0
    if icon_id:
        row.template_icon(icon_value=icon_id, scale=5.0)
    else:
        row.label(text="", icon="IMAGE_DATA")

    col = row.column(align=True)
    col.label(text=entry.get("model") or "Render", icon="SHADERFX")

    prompt = (entry.get("prompt") or "").strip()
    if prompt:
        col.label(text=prompt[:32] + ("…" if len(prompt) > 32 else ""),
                  icon="GREASEPENCIL")
    else:
        sub = col.row()
        sub.active = False
        sub.label(text="No prompt", icon="GREASEPENCIL")

    if has_result:
        op = col.operator("blmn.show_image", text="View", icon="IMAGE_DATA")
        op.filepath = result_path


def _draw_image_preview(layout, image):
    """Render a thumbnail of the result image in the panel."""
    try:
        image.preview_ensure()
        if image.preview and image.preview.icon_id:
            layout.template_icon(icon_value=image.preview.icon_id, scale=8.0)
            return
    except Exception:  # noqa: BLE001
        pass
    layout.label(text=os.path.basename(image.filepath or "result"), icon="IMAGE_DATA")


def _result_image(sp):
    path = sp.last_result_path
    if not path or not os.path.isfile(bpy.path.abspath(path)):
        return None
    return utils.load_image(path, name="blmn_result")


def _safe_history(context):
    try:
        out_dir = utils.get_output_dir(context)
    except ValueError:
        return []
    return history.load(out_dir)


_classes = (BLMN_PT_main, BLMN_PT_history)


def register():
    global _thumbs
    _thumbs = bpy.utils.previews.new()
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    global _thumbs
    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception as exc:  # noqa: BLE001 - Blender reload state can be partially stale.
            if "missing bl_rna" not in str(exc) and "is not registered" not in str(exc):
                raise
            utils.log("Panel unregister skipped:", cls.__name__, exc)
    if _thumbs is not None:
        bpy.utils.previews.remove(_thumbs)
        _thumbs = None
