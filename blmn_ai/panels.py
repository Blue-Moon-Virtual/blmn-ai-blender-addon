"""N-panel UI in the 3D Viewport sidebar ("Blue Moon" tab).

Mirrors the web Render tool settings bar: model, scene type, style, sliders,
resolution, seed. Account connection lives in the add-on Preferences; the
panel only shows a short status line with credits.
"""
import os
import bpy
from bpy.types import Panel

from . import properties as props
from . import history
from . import net
from . import operators
from . import utils
from . import addon_updater_ops


def _status_icon(status):
    return {
        props.STATUS_IDLE: "CHECKMARK",
        props.STATUS_FINISHED: "CHECKMARK",
        props.STATUS_FAILED: "ERROR",
    }.get(status, "SORTTIME")


class BLMN_PT_main(Panel):
    bl_label = "BLUE MOON AI"
    bl_idname = "BLMN_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Blue Moon"

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
            box.label(text="Connect in Add-on Preferences")
            box.operator("blmn.open_connect_page", icon="URL")
            return

        # --- Input ---
        box = layout.box()
        box.label(text="Capture", icon="OUTLINER_OB_CAMERA")
        box.prop(sp, "source", text="")
        row = box.row()
        row.enabled = not busy
        row.operator("blmn.capture_preview", icon="RESTRICT_RENDER_OFF")

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
        box.label(text="Prompt (optional)", icon="GREASEPENCIL")
        box.prop(sp, "prompt", text="")
        if sp.prompt:
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
        col = layout.column()
        col.scale_y = 1.5
        col.enabled = not busy
        cost = sp.estimated_credits()
        cost_label = "Generate ({0} credit{1})".format(cost, "" if cost == 1 else "s") \
            if cost else "Generate (no credits)"
        col.operator("blmn.generate_render", text=cost_label, icon="SHADERFX")

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
    bl_category = "Blue Moon"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        entries = _safe_history(context)
        if not entries:
            layout.label(text="No renders yet")
            return
        for entry in entries[:8]:
            layout.label(text="• " + entry.get("label", "Render"), icon="IMAGE_DATA")
        layout.label(text="Full history: blmn.ai → Library", icon="URL")


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
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
