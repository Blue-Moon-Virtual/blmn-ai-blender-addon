"""Operators: the modal Generate job plus small action operators.

The Generate operator captures on the main thread, then runs the network
pipeline (upload → render → poll → download) in a worker thread. A modal timer
drains a thread-safe event queue, so Blender's UI never freezes and all
bpy.data writes stay on the main thread.
"""
import os
import queue
import threading

import bpy
from bpy.types import Operator

from . import properties as props
from . import capture
from . import history
from . import net
from . import utils


def _set_status(scene_props, status, message=""):
    scene_props.status = status
    scene_props.status_message = message


def kick_credits_refresh(context, force=False):
    """Start a background credit refresh and repaint the panel when it lands.

    Safe to call from a panel draw: it self-throttles (see should_auto_refresh)
    so repeated draws don't pile up requests. The redraw happens on the main
    thread via an app timer once the worker thread settles.
    """
    prefs = utils.prefs(context)
    if not prefs.connected():
        return
    if not force and not net.should_auto_refresh():
        return
    prefs.apply_access()
    if not net.refresh_credits_async(prefs.api_base(), prefs.device_token):
        return

    def _poll():
        if net.is_refresh_inflight():
            return 0.25
        utils.tag_redraw_all()
        return None

    bpy.app.timers.register(_poll, first_interval=0.25)


def build_render_request(sp):
    """Translate scene settings into the blmn.ai wire payload.

    Uses the same routes and fields as the web Render tool; the server
    composes the system prompt and charges credits.
    """
    prompt = sp.prompt.strip()
    prompt_settings = {
        "creativity": sp.creativity,
        "environmentFill": sp.environment_fill,
        "decorationFill": sp.decoration_fill,
    }
    seed = sp.seed if sp.seed > 0 else None

    if sp.model == "pro":
        payload = {
            "prompt": prompt,
            "imageStyle": sp.image_style,
            "renderType": sp.render_type,
            "resolution": sp.resolution,
            "aspectRatio": "auto",
            "promptSettings": prompt_settings,
            # Charge the personal balance — same mode the panel displays.
            "billingMode": net.BILLING_MODE,
        }
        if seed is not None:
            payload["seed"] = seed
        return {
            "route": "/api/fal/render-pro",
            "wait_route": "/api/fal/render-pro/wait",
            "payload": payload,
            "image_key": "imageUrl",
        }

    payload = {
        "userPrompt": prompt,
        "renderType": sp.render_type,
        "model": sp.model,
        "imageStyle": sp.image_style,
        "promptSettings": prompt_settings,
        # Charge the personal balance — same mode the panel displays.
        "billingMode": net.BILLING_MODE,
    }
    if sp.model == "light":
        payload["quality"] = "hd" if sp.hd else "sd"
        payload["resolutionPreset"] = "Auto"
        payload["maxMegapixels"] = 1
    elif sp.model in props.RESOLUTION_MODELS:
        payload["resolution"] = sp.resolution
    if seed is not None:
        payload["seed"] = seed
    return {
        "route": "/api/fal/plan-render",
        "wait_route": "/api/fal/plan-render/wait",
        "payload": payload,
        "image_key": "imageUrl",
    }


class BLMN_OT_capture_preview(Operator):
    bl_idname = "blmn.capture_preview"
    bl_label = "Preview Capture"
    bl_description = "Capture the chosen view and show it in the Image Editor (nothing is uploaded)"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return not context.scene.blmn_ai.is_busy()

    def execute(self, context):
        sp = context.scene.blmn_ai
        try:
            out_dir = utils.get_output_dir(context)
            path = os.path.join(out_dir, utils.make_filename("capture"))
            capture.capture_view(context, sp.source, path)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        sp.last_capture_path = path
        img = utils.load_image(path, name="blmn_capture")
        utils.open_in_image_editor(img, context)
        self.report({"INFO"}, "Capture created.")
        return {"FINISHED"}


class BLMN_OT_generate(Operator):
    bl_idname = "blmn.generate_render"
    bl_label = "Generate Render"
    bl_description = "Capture the view and render it with blmn.ai (charges credits like the web app)"
    bl_options = {"REGISTER"}

    _timer = None
    _thread = None
    _events = None
    _cancel = None
    _out_dir = ""
    _capture_path = ""
    _model_label = ""

    @classmethod
    def poll(cls, context):
        sp = context.scene.blmn_ai
        if sp.is_busy():
            return False
        return utils.prefs(context).connected()

    def execute(self, context):
        sp = context.scene.blmn_ai
        prefs = utils.prefs(context)

        if not prefs.connected():
            self.report({"ERROR"}, "Connect your blmn.ai account in the add-on preferences first.")
            return {"CANCELLED"}

        # Install any CF Access headers before the worker thread starts (the
        # thread reads the module-level headers set here on the main thread).
        prefs.apply_access()

        # --- Capture (synchronous, fast, main thread) ---
        _set_status(sp, props.STATUS_CAPTURING)
        try:
            self._out_dir = utils.get_output_dir(context)
            self._capture_path = os.path.join(self._out_dir, utils.make_filename("capture"))
            capture.capture_view(context, sp.source, self._capture_path)
        except ValueError as exc:
            _set_status(sp, props.STATUS_FAILED, str(exc))
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        sp.last_capture_path = self._capture_path
        self._model_label = dict((m[0], m[1]) for m in props.MODELS).get(sp.model, sp.model)

        # --- Hand the network pipeline to a worker thread ---
        self._events = queue.Queue()
        self._cancel = threading.Event()
        self._thread = threading.Thread(
            target=net.run_render_job,
            args=(prefs.api_base(), prefs.device_token, self._capture_path,
                  sp.prompt.strip(), build_render_request(sp), self._out_dir,
                  self._events, self._cancel, sp.reference_paths()),
            daemon=True,
        )
        self._thread.start()

        _set_status(sp, props.STATUS_UPLOADING)
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.25, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        sp = context.scene.blmn_ai

        if event.type == "ESC":
            self._cancel.set()
            _set_status(sp, props.STATUS_IDLE)
            self.report({"INFO"}, "Cancelled. A render that was already queued may "
                                  "still appear in your blmn.ai library.")
            return self._finish(context, cancelled=True)

        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        try:
            while True:
                kind, data = self._events.get_nowait()
                if kind == "status":
                    _set_status(sp, data)
                elif kind == "error":
                    _set_status(sp, props.STATUS_FAILED, data)
                    self.report({"ERROR"}, data)
                    return self._finish(context)
                elif kind == "finished":
                    return self._on_finished(context, sp, data)
        except queue.Empty:
            pass

        _tag_redraw(context)
        return {"RUNNING_MODAL"}

    def _on_finished(self, context, sp, data):
        result_path = data["result_path"]
        sp.last_result_path = result_path
        img = utils.load_image(result_path, name="blmn_result")
        utils.open_in_image_editor(img, context)

        history.add(
            self._out_dir, sp.prompt.strip(), self._model_label,
            self._capture_path, result_path,
            limit=utils.prefs(context).history_limit,
        )

        _set_status(sp, props.STATUS_FINISHED)
        self.report({"INFO"}, "Render finished.")
        return self._finish(context)

    def _finish(self, context, cancelled=False):
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        _tag_redraw(context)
        return {"CANCELLED"} if cancelled else {"FINISHED"}


class BLMN_OT_refresh_credits(Operator):
    bl_idname = "blmn.refresh_credits"
    bl_label = "Refresh Credits"
    bl_description = "Fetch your current credit balance from blmn.ai"
    bl_options = {"REGISTER", "INTERNAL"}

    @classmethod
    def poll(cls, context):
        return utils.prefs(context).connected()

    def execute(self, context):
        kick_credits_refresh(context, force=True)
        return {"FINISHED"}


class BLMN_OT_view_result(Operator):
    bl_idname = "blmn.view_result"
    bl_label = "View Result"
    bl_description = "Show the latest result in the Image Editor"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        sp = context.scene.blmn_ai
        return bool(sp.last_result_path) and os.path.isfile(sp.last_result_path)

    def execute(self, context):
        sp = context.scene.blmn_ai
        img = utils.load_image(sp.last_result_path, name="blmn_result")
        if not utils.open_in_image_editor(img, context):
            self.report({"WARNING"}, "Could not open the result image.")
            return {"CANCELLED"}
        return {"FINISHED"}


class BLMN_OT_show_image(Operator):
    bl_idname = "blmn.show_image"
    bl_label = "View Image"
    bl_description = "Open this render in the Image Editor (a new window if none is open)"
    bl_options = {"REGISTER", "INTERNAL"}

    filepath: bpy.props.StringProperty(default="", options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        img = utils.load_image(self.filepath, name="blmn_result")
        if img is None or not utils.open_in_image_editor(img, context):
            self.report({"WARNING"}, "Could not open the image.")
            return {"CANCELLED"}
        return {"FINISHED"}


def _latest_preview_image(context):
    """The most recent result or capture image, loaded, or None."""
    sp = context.scene.blmn_ai
    path = sp.last_result_path or sp.last_capture_path
    if not path or not os.path.isfile(bpy.path.abspath(path)):
        return None
    return utils.load_image(path, name="blmn_result")


def _draw_preview_target_menu(menu, context):
    layout = menu.layout
    op = layout.operator("blmn.spawn_image_editor", text="New Window", icon="ADD")
    op.window_index = -1
    windows = context.window_manager.windows
    if windows:
        layout.separator()
        for i, win in enumerate(windows):
            has_ie = any(a.type == "IMAGE_EDITOR" for a in win.screen.areas)
            label = "Current Window" if win == context.window else "Window {0}".format(i + 1)
            if has_ie:
                label += " (reuse)"
            op = layout.operator(
                "blmn.spawn_image_editor",
                text=label,
                icon="IMAGE_DATA" if has_ie else "WINDOW",
            )
            op.window_index = i


class BLMN_OT_open_preview_target(Operator):
    bl_idname = "blmn.open_preview_target"
    bl_label = "Preview Window"
    bl_description = ("Choose where renders preview — a new window or split into "
                      "an existing one. Highlighted when an Image Editor is open")
    bl_options = {"REGISTER", "INTERNAL"}

    def invoke(self, context, event):
        context.window_manager.popup_menu(
            _draw_preview_target_menu, title="Open Preview In", icon="RENDER_RESULT")
        return {"FINISHED"}

    def execute(self, context):
        return {"FINISHED"}


class BLMN_OT_spawn_image_editor(Operator):
    bl_idname = "blmn.spawn_image_editor"
    bl_label = "Open Preview Editor"
    bl_description = "Open an Image Editor here to preview captures and renders"
    bl_options = {"REGISTER", "INTERNAL"}

    window_index: bpy.props.IntProperty(default=-1, options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        img = _latest_preview_image(context)
        if not utils.show_preview_in_window(context, self.window_index, img):
            self.report({"WARNING"}, "Could not open an Image Editor.")
            return {"CANCELLED"}
        return {"FINISHED"}


class BLMN_OT_camera_from_view(Operator):
    bl_idname = "blmn.camera_from_view"
    bl_label = "Camera From View"
    bl_description = "Create a camera matching the current viewport view and make it the active camera"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        # Only offered when the scene has no camera at all — greyed otherwise.
        return not any(o.type == "CAMERA" for o in context.scene.objects)

    def execute(self, context):
        scene = context.scene

        space = None
        if context.space_data and context.space_data.type == "VIEW_3D":
            space = context.space_data
        else:
            for area in context.screen.areas:
                if area.type == "VIEW_3D":
                    space = area.spaces.active
                    break

        cam_data = bpy.data.cameras.new("blmn_Camera")
        cam_obj = bpy.data.objects.new("blmn_Camera", cam_data)
        scene.collection.objects.link(cam_obj)

        if space is not None:
            if space.region_3d is not None:
                cam_obj.matrix_world = space.region_3d.view_matrix.inverted()
            # Match the viewport's focal length so framing lines up.
            if getattr(space, "lens", 0):
                cam_data.lens = space.lens

        scene.camera = cam_obj
        context.scene.blmn_ai.source = "CAMERA"
        self.report({"INFO"}, "Camera created from current view.")
        return {"FINISHED"}


class BLMN_OT_open_output(Operator):
    bl_idname = "blmn.open_output_folder"
    bl_label = "Open Output Folder"
    bl_description = "Open the output folder in your file browser"
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            out_dir = utils.get_output_dir(context)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if not utils.open_folder(out_dir):
            self.report({"WARNING"}, "Could not open output folder.")
            return {"CANCELLED"}
        return {"FINISHED"}


class BLMN_OT_add_reference(Operator):
    bl_idname = "blmn.add_reference"
    bl_label = "Add Reference Image"
    bl_description = "Add reference image(s) to guide the render's style and materials (max 4)"
    bl_options = {"REGISTER", "INTERNAL"}

    files: bpy.props.CollectionProperty(
        type=bpy.types.OperatorFileListElement, options={"HIDDEN", "SKIP_SAVE"})
    directory: bpy.props.StringProperty(subtype="DIR_PATH", options={"HIDDEN", "SKIP_SAVE"})
    filepath: bpy.props.StringProperty(subtype="FILE_PATH", options={"HIDDEN", "SKIP_SAVE"})
    filter_glob: bpy.props.StringProperty(
        default="*.png;*.jpg;*.jpeg;*.webp", options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return len(context.scene.blmn_ai.references) < props.MAX_REFERENCES

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        sp = context.scene.blmn_ai
        paths = []
        if self.files:
            for f in self.files:
                if f.name:
                    paths.append(os.path.join(self.directory, f.name))
        elif self.filepath:
            paths.append(self.filepath)

        added = 0
        for path in paths:
            if len(sp.references) >= props.MAX_REFERENCES:
                break
            item = sp.references.add()
            item.path = path
            added += 1

        if added == 0:
            self.report({"WARNING"}, "No image selected.")
            return {"CANCELLED"}
        if added < len(paths):
            self.report({"WARNING"}, "Only {0} reference images are allowed.".format(props.MAX_REFERENCES))
        utils.tag_redraw_all()
        return {"FINISHED"}


class BLMN_OT_remove_reference(Operator):
    bl_idname = "blmn.remove_reference"
    bl_label = "Remove Reference Image"
    bl_description = "Remove this reference image"
    bl_options = {"REGISTER", "INTERNAL"}

    index: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        sp = context.scene.blmn_ai
        if 0 <= self.index < len(sp.references):
            sp.references.remove(self.index)
            utils.tag_redraw_all()
        return {"FINISHED"}


def _tag_redraw(context):
    for area in context.screen.areas:
        if area.type == "VIEW_3D":
            area.tag_redraw()


_classes = (
    BLMN_OT_capture_preview,
    BLMN_OT_generate,
    BLMN_OT_refresh_credits,
    BLMN_OT_view_result,
    BLMN_OT_show_image,
    BLMN_OT_open_preview_target,
    BLMN_OT_spawn_image_editor,
    BLMN_OT_camera_from_view,
    BLMN_OT_open_output,
    BLMN_OT_add_reference,
    BLMN_OT_remove_reference,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
