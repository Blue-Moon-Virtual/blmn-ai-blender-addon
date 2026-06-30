"""Operators: the modal Generate job plus small action operators.

The Generate operator captures on the main thread, then runs the network
pipeline (upload → render → poll → download) in a worker thread. A modal timer
drains a thread-safe event queue, so Blender's UI never freezes and all
bpy.data writes stay on the main thread.
"""
import os
import shutil

import bpy
from bpy.types import Operator

from . import properties as props
from . import capture
from . import jobs
from . import net
from . import utils


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


def build_animation_request(sp):
    """Translate scene settings into the blmn.ai image-to-video wire payload.

    Mirrors the web app's animate endpoint (Kling v3 image-to-video). The model
    is fixed server-side, so it is not sent; the server composes the system
    prompt, validates aspect ratio and charges credits (5s = 8, 10s = 16).

    Returns a render_request dict consumed by net.run_render_job:
      route / wait_route : POST + poll endpoints
      payload            : JSON body (image URLs are filled in by the worker)
      image_key          : payload key for the FIRST frame's uploaded URL
      end_image_key      : payload key for the LAST frame's uploaded URL
    """
    # Duration is a number the server snaps to 5 or 10; seed/model are ignored
    # by the animate endpoint, so we don't send them.
    try:
        duration = int(sp.video_duration)
    except (TypeError, ValueError):
        duration = 5

    payload = {
        "userPrompt": sp.prompt.strip(),
        "duration": duration,
        # Charge the personal balance — same mode the panel displays.
        "billingMode": net.BILLING_MODE,
    }

    return {
        "route": "/api/fal/animate",
        "wait_route": "/api/fal/animate/wait",
        "payload": payload,
        "image_key": "imageUrl",          # first/start frame
        "end_image_key": "end_image_url",  # last/end frame
    }


class BLMN_OT_capture_preview(Operator):
    bl_idname = "blmn.capture_preview"
    bl_label = "Preview Capture"
    bl_description = "Capture the chosen view and show it in the Image Editor (nothing is uploaded)"
    bl_options = {"REGISTER"}

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


def _preview_capture_exists(sp):
    path = (sp.last_capture_path or "").strip()
    return bool(path) and os.path.isfile(bpy.path.abspath(path))


def _frame_exists(path):
    path = (path or "").strip()
    return bool(path) and os.path.isfile(bpy.path.abspath(path))


def _animation_frames_exist(sp):
    """Both the first and last animation frames have been captured."""
    return _frame_exists(sp.first_frame_path) and _frame_exists(sp.last_frame_path)


class BLMN_OT_generate(Operator):
    bl_idname = "blmn.generate_render"
    bl_label = "Generate Render"
    bl_description = ("Render the last Preview capture with blmn.ai (charges credits like "
                     "the web app). Fire several — each runs concurrently in the panel")
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        sp = context.scene.blmn_ai
        return (utils.prefs(context).connected()
                and _preview_capture_exists(sp)
                and jobs.can_start())

    def execute(self, context):
        sp = context.scene.blmn_ai
        prefs = utils.prefs(context)

        if not prefs.connected():
            self.report({"ERROR"}, "Connect your blmn.ai account in the add-on preferences first.")
            return {"CANCELLED"}

        if not _preview_capture_exists(sp):
            self.report({"ERROR"}, "Click Preview first so Generate has a captured image to render.")
            return {"CANCELLED"}

        if not jobs.can_start():
            self.report({"WARNING"}, "Too many renders in progress — wait for one to finish.")
            return {"CANCELLED"}

        # Install any CF Access headers before the worker thread starts (the
        # thread reads the module-level headers set here on the main thread).
        prefs.apply_access()

        try:
            out_dir = utils.get_output_dir(context)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        # Snapshot the current Preview into a per-job input file so each tray
        # item has its own stable thumbnail (and a re-Preview can't disturb an
        # in-flight upload).
        capture_path = bpy.path.abspath(sp.last_capture_path)
        input_path = os.path.join(out_dir, utils.make_unique_filename("input"))
        try:
            shutil.copyfile(capture_path, input_path)
        except OSError as exc:
            utils.log("Could not copy capture for job:", exc)
            input_path = capture_path  # fall back to the shared capture

        model_label = dict((m[0], m[1]) for m in props.MODELS).get(sp.model, sp.model)

        jobs.start_job(
            prefs.api_base(), prefs.device_token, input_path, sp.prompt.strip(),
            model_label, build_render_request(sp), out_dir, sp.reference_paths(),
        )

        running = jobs.active_count()
        self.report({"INFO"}, "Generation started ({0} running).".format(running))
        utils.tag_redraw_all()
        return {"FINISHED"}


class BLMN_OT_capture_frame(Operator):
    bl_idname = "blmn.capture_frame"
    bl_label = "Capture Frame"
    bl_description = ("Capture the chosen view as the animation's first or last frame "
                     "(nothing is uploaded)")
    bl_options = {"REGISTER"}

    slot: bpy.props.EnumProperty(
        items=[
            ("FIRST", "First Frame", "Capture the start frame of the animation"),
            ("LAST", "Last Frame", "Capture the end frame of the animation"),
        ],
        default="FIRST",
        options={"SKIP_SAVE"},
    )

    def execute(self, context):
        sp = context.scene.blmn_ai
        try:
            out_dir = utils.get_output_dir(context)
            kind = "first_frame" if self.slot == "FIRST" else "last_frame"
            path = os.path.join(out_dir, utils.make_unique_filename(kind))
            capture.capture_view(context, sp.source, path)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        if self.slot == "FIRST":
            sp.first_frame_path = path
        else:
            sp.last_frame_path = path

        img = utils.load_image(path, name="blmn_frame")
        utils.open_in_image_editor(img, context)
        self.report({"INFO"}, "{0} captured.".format(
            "First frame" if self.slot == "FIRST" else "Last frame"))
        utils.tag_redraw_all()
        return {"FINISHED"}


class BLMN_OT_generate_animation(Operator):
    bl_idname = "blmn.generate_animation"
    bl_label = "Generate Animation"
    bl_description = ("Animate between the captured first and last frames with blmn.ai "
                     "(charges credits). Runs concurrently in the panel")
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        sp = context.scene.blmn_ai
        return (utils.prefs(context).connected()
                and _animation_frames_exist(sp)
                and jobs.can_start())

    def execute(self, context):
        sp = context.scene.blmn_ai
        prefs = utils.prefs(context)

        if not prefs.connected():
            self.report({"ERROR"}, "Connect your blmn.ai account in the add-on preferences first.")
            return {"CANCELLED"}

        if not _animation_frames_exist(sp):
            self.report({"ERROR"}, "Capture both a first and last frame before generating.")
            return {"CANCELLED"}

        if not jobs.can_start():
            self.report({"WARNING"}, "Too many generations in progress — wait for one to finish.")
            return {"CANCELLED"}

        prefs.apply_access()

        try:
            out_dir = utils.get_output_dir(context)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        # Snapshot both frames into per-job input files so a re-capture can't
        # disturb an in-flight upload (mirrors the image Generate flow).
        first_src = bpy.path.abspath(sp.first_frame_path)
        last_src = bpy.path.abspath(sp.last_frame_path)
        first_input = os.path.join(out_dir, utils.make_unique_filename("anim_first"))
        last_input = os.path.join(out_dir, utils.make_unique_filename("anim_last"))
        try:
            shutil.copyfile(first_src, first_input)
            shutil.copyfile(last_src, last_input)
        except OSError as exc:
            utils.log("Could not copy frames for animation job:", exc)
            first_input, last_input = first_src, last_src  # fall back to shared captures

        model_label = props.VIDEO_MODEL_LABEL

        request = build_animation_request(sp)
        # The last frame rides along as an extra named upload; the first frame
        # goes through the standard capture/image_key path.
        extra_images = [(request["end_image_key"], last_input)]

        jobs.start_job(
            prefs.api_base(), prefs.device_token, first_input, sp.prompt.strip(),
            model_label, request, out_dir,
            extra_images=extra_images, result_ext="mp4", is_video=True,
        )

        running = jobs.active_count()
        self.report({"INFO"}, "Animation started ({0} running).".format(running))
        utils.tag_redraw_all()
        return {"FINISHED"}


class BLMN_OT_cancel_job(Operator):
    bl_idname = "blmn.cancel_job"
    bl_label = "Cancel Generation"
    bl_description = ("Stop this generation. A render already queued on the server may still "
                     "appear in your blmn.ai library")
    bl_options = {"REGISTER", "INTERNAL"}

    job_id: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        jobs.cancel_job(self.job_id)
        return {"FINISHED"}


class BLMN_OT_dismiss_job(Operator):
    bl_idname = "blmn.dismiss_job"
    bl_label = "Dismiss"
    bl_description = "Remove this finished item from the list"
    bl_options = {"REGISTER", "INTERNAL"}

    job_id: bpy.props.IntProperty(default=-1)

    def execute(self, context):
        jobs.dismiss_job(self.job_id)
        return {"FINISHED"}


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


class BLMN_OT_refresh_history(Operator):
    bl_idname = "blmn.refresh_history"
    bl_label = "Refresh History"
    bl_description = "Reload the local render history shown in the panel"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        utils.tag_redraw_all()
        return {"FINISHED"}


class BLMN_OT_edit_prompt(Operator):
    bl_idname = "blmn.edit_prompt"
    bl_label = "Edit Prompt"
    bl_description = "Edit the prompt in a wider input dialog"
    bl_options = {"REGISTER", "INTERNAL"}

    prompt: bpy.props.StringProperty(
        name="Prompt",
        description="Optional render prompt",
        default="",
        maxlen=props.PROMPT_MAX,
        options={"SKIP_SAVE"},
    )

    def invoke(self, context, event):
        self.prompt = context.scene.blmn_ai.prompt
        return context.window_manager.invoke_props_dialog(self, width=640)

    def draw(self, context):
        col = self.layout.column()
        col.scale_y = 1.4
        col.prop(self, "prompt", text="")

    def execute(self, context):
        context.scene.blmn_ai.prompt = self.prompt
        utils.tag_redraw_all()
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
    bl_description = "Open this render in the Image Editor"
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


class BLMN_OT_pick_preview_area(Operator):
    bl_idname = "blmn.pick_preview_area"
    bl_label = "Select Preview Area"
    bl_description = "Click an editor area to turn it into the blmn.ai preview Image Editor"
    bl_options = {"REGISTER", "INTERNAL"}

    _active = False

    @classmethod
    def is_active(cls):
        return cls._active

    def invoke(self, context, event):
        cls = self.__class__
        if cls._active:
            cls._active = False
            utils.tag_redraw_all()
            return {"CANCELLED"}
        cls._active = True
        context.window_manager.modal_handler_add(self)
        utils.tag_redraw_all()
        self.report({"INFO"}, "Click an editor area for the blmn.ai preview.")
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        cls = self.__class__
        if not cls._active:
            return {"CANCELLED"}
        if event.type in {"ESC", "RIGHTMOUSE"}:
            cls._active = False
            utils.tag_redraw_all()
            return {"CANCELLED"}
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            area = _area_at_mouse(context.window.screen, event.mouse_x, event.mouse_y)
            img = _latest_preview_image(context)
            if area is not None and utils.show_image_in_area(context, area, img):
                cls._active = False
                utils.tag_redraw_all()
                return {"FINISHED"}
            cls._active = False
            utils.tag_redraw_all()
            self.report({"WARNING"}, "Could not use that area for preview.")
            return {"CANCELLED"}
        return {"RUNNING_MODAL"}


def _area_at_mouse(screen, mouse_x, mouse_y):
    for area in screen.areas:
        if area.x <= mouse_x < area.x + area.width and area.y <= mouse_y < area.y + area.height:
            return area
    return None


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


_classes = (
    BLMN_OT_capture_preview,
    BLMN_OT_generate,
    BLMN_OT_capture_frame,
    BLMN_OT_generate_animation,
    BLMN_OT_cancel_job,
    BLMN_OT_dismiss_job,
    BLMN_OT_refresh_credits,
    BLMN_OT_refresh_history,
    BLMN_OT_edit_prompt,
    BLMN_OT_view_result,
    BLMN_OT_show_image,
    BLMN_OT_pick_preview_area,
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
