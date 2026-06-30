"""N-panel UI in the 3D Viewport sidebar ("blmn.ai" tab).

Mirrors the web Render tool settings bar: model, scene type, style, sliders,
resolution, seed. Account connection lives in the add-on Preferences; the
panel only shows a short status line with credits.
"""
import os
import textwrap
import time
import bpy
import bpy.utils.previews
from bpy.types import Panel

from . import properties as props
from . import history
from . import jobs
from . import net
from . import operators
from . import utils
from . import addon_updater_ops

# Braille spinner frames for the generating tray when UILayout.progress() is
# unavailable. Cycled by wall-clock time so successive panel repaints animate.
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

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


def _draw_frame_slot(layout, sp, frame_path, slot, label):
    """One animation key-frame row: thumbnail + Capture/Recapture button."""
    box = layout.box()
    row = box.row()

    has_frame = bool(frame_path) and os.path.isfile(bpy.path.abspath(frame_path))
    icon_id = _thumb_icon_id(frame_path) if has_frame else 0
    if icon_id:
        row.template_icon(icon_value=icon_id, scale=5.0)
    else:
        row.label(text="", icon="IMAGE_DATA")

    col = row.column(align=True)
    col.label(text=label, icon="CHECKMARK" if has_frame else "ADD")
    op = col.operator("blmn.capture_frame",
                      text="Recapture" if has_frame else "Capture",
                      icon="HIDE_OFF")
    op.slot = slot


def _draw_job(layout, job):
    """One generating-tray row: input thumbnail + live status (animated) + action."""
    box = layout.box()
    row = box.row()

    icon_id = _thumb_icon_id(job.input_path)
    if icon_id:
        row.template_icon(icon_value=icon_id, scale=5.0)
    else:
        row.label(text="", icon="IMAGE_DATA")

    col = row.column(align=True)
    col.label(text=job.model_label or "Render", icon="SHADERFX")

    if job.state == jobs.RUNNING:
        label = props.STATUS_LABELS.get(job.status, job.status)
        line = col.row(align=True)
        if hasattr(line, "progress"):
            # Sweeping ring driven by the wall clock — animates as the job
            # manager's timer keeps repainting the panel.
            line.progress(factor=(time.time() * 0.8) % 1.0, type="RING", text=label)
        else:
            frame = _SPINNER_FRAMES[int(time.time() * 12) % len(_SPINNER_FRAMES)]
            line.label(text="{0} {1}".format(frame, label))
        line.operator("blmn.cancel_job", text="", icon="X").job_id = job.id
    elif job.state == jobs.DONE:
        done = col.row(align=True)
        done.label(text="Done", icon="CHECKMARK")
        if job.result_path and os.path.isfile(bpy.path.abspath(job.result_path)):
            if job.is_video:
                done.operator("blmn.open_output_folder", text="Folder",
                              icon="FILE_FOLDER")
            else:
                op = done.operator("blmn.show_image", text="View", icon="IMAGE_DATA")
                op.filepath = job.result_path
    else:  # FAILED
        col.label(text="Failed", icon="ERROR")
        if job.message:
            sub = col.row()
            sub.active = False
            sub.label(text=job.message[:40] + ("…" if len(job.message) > 40 else ""))
        col.operator("blmn.dismiss_job", text="Dismiss", icon="X").job_id = job.id


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

        # --- Mode (Image render vs Animation) ---
        row = layout.row(align=True)
        row.prop(sp, "mode", expand=True)

        # --- Input ---
        box = layout.box()
        box.label(text="Capture", icon="OUTLINER_OB_CAMERA")
        box.prop(sp, "source", text="")
        # Shared helpers: pick the preview area and build a camera from the view.
        row = box.row(align=True)
        row.operator("blmn.pick_preview_area", text="", icon="EYEDROPPER",
                     depress=operators.BLMN_OT_pick_preview_area.is_active())
        row.operator("blmn.camera_from_view", text="", icon="VIEW_CAMERA")
        if sp.is_animation():
            _draw_frame_slot(box, sp, sp.first_frame_path, "FIRST", "First Frame")
            _draw_frame_slot(box, sp, sp.last_frame_path, "LAST", "Last Frame")
        else:
            row.operator("blmn.capture_preview", text="Preview", icon="HIDE_OFF")

        # --- Model + settings ---
        box = layout.box()
        if sp.is_animation():
            box.label(text="Animation Settings", icon="RENDER_ANIMATION")
            info = box.row()
            info.active = False
            info.label(text="Model: {0}".format(props.VIDEO_MODEL_LABEL), icon="SHADERFX")
            box.label(text="Duration")
            row = box.row(align=True)
            row.prop(sp, "video_duration", expand=True)
        else:
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

        # --- Reference images (optional, image render only) ---
        if not sp.is_animation():
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
        at_capacity = not jobs.can_start()
        cost = sp.estimated_credits()
        if sp.is_animation():
            ready = operators._animation_frames_exist(sp)
            verb, op_id, icon = "Animate", "blmn.generate_animation", "RENDER_ANIMATION"
            not_ready_hint = "Capture both frames before generating"
        else:
            ready = operators._preview_capture_exists(sp)
            verb, op_id, icon = "Generate", "blmn.generate_render", "SHADERFX"
            not_ready_hint = "Click Preview before Generate"

        col = layout.column()
        col.scale_y = 1.5
        col.enabled = ready and not at_capacity
        cost_label = "{0} ({1} credit{2})".format(verb, cost, "" if cost == 1 else "s") \
            if cost else "{0} (no credits)".format(verb)
        col.operator(op_id, text=cost_label, icon=icon)
        if not ready:
            row = layout.row()
            row.active = False
            row.label(text=not_ready_hint, icon="INFO")
        elif at_capacity:
            row = layout.row()
            row.active = False
            row.label(text="Max {0} running".format(jobs.MAX_CONCURRENT), icon="INFO")

        # --- Generating tray (live, animated) ---
        tray = jobs.tray()
        if tray:
            box = layout.box()
            active = jobs.active_count()
            header = "Generating ({0})".format(active) if active else "Recent"
            box.label(text=header, icon="RENDER_ANIMATION")
            for job in tray:
                _draw_job(box, job)

        # --- Result ---
        if sp.is_animation():
            video_path = (sp.last_video_path or "").strip()
            if video_path and os.path.isfile(bpy.path.abspath(video_path)):
                box = layout.box()
                box.label(text="Latest Animation", icon="FILE_MOVIE")
                box.label(text=os.path.basename(video_path))
                box.operator("blmn.open_output_folder", text="Open Folder",
                             icon="FILE_FOLDER")
        else:
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
    """One history row: thumbnail + model + prompt (or 'No prompt') + View/Folder."""
    result_path = entry.get("result_path", "")
    has_result = bool(result_path) and os.path.isfile(bpy.path.abspath(result_path))
    is_video = entry.get("kind") == "video"

    box = layout.box()
    row = box.row()

    # Video results have no image thumbnail; show a film icon instead.
    icon_id = 0 if is_video else (_thumb_icon_id(result_path) if has_result else 0)
    if icon_id:
        row.template_icon(icon_value=icon_id, scale=5.0)
    else:
        row.label(text="", icon="FILE_MOVIE" if is_video else "IMAGE_DATA")

    col = row.column(align=True)
    col.label(text=entry.get("model") or "Render",
              icon="RENDER_ANIMATION" if is_video else "SHADERFX")

    prompt = (entry.get("prompt") or "").strip()
    if prompt:
        col.label(text=prompt[:32] + ("…" if len(prompt) > 32 else ""),
                  icon="GREASEPENCIL")
    else:
        sub = col.row()
        sub.active = False
        sub.label(text="No prompt", icon="GREASEPENCIL")

    if has_result:
        if is_video:
            col.operator("blmn.open_output_folder", text="Folder", icon="FILE_FOLDER")
        else:
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
