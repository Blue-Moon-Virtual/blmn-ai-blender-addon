"""Shared helpers: paths, filenames, image loading, console logging.

Keep user-facing errors short (panel/report); log technical detail to console.
"""
import os
import time
import uuid
import bpy

LOG_PREFIX = "[blmn.ai]"


def log(*args):
    """Technical logging goes to the console only, never the N-panel."""
    print(LOG_PREFIX, *args)


def prefs(context=None):
    ctx = context or bpy.context
    return ctx.preferences.addons[__package__].preferences


def get_output_dir(context=None):
    """Return the configured output directory, falling back to a per-user dir.

    Always returns an absolute, existing directory or raises ValueError with a
    short, user-facing message.
    """
    raw = (prefs(context).output_folder or "").strip()

    if raw:
        path = bpy.path.abspath(raw)
    else:
        base = bpy.utils.user_resource("DATAFILES", path="blmn_ai", create=True)
        path = base or os.path.join(os.path.expanduser("~"), "blmn_ai")

    path = os.path.normpath(path)

    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        log("Failed to create output dir:", path, exc)
        raise ValueError("Invalid output folder — storage location is not available.")

    if not os.path.isdir(path) or not os.access(path, os.W_OK):
        raise ValueError("Invalid output folder — storage location is not available.")

    return path


def timestamp():
    return time.strftime("%Y%m%d_%H%M%S")


def make_filename(kind, ext="png"):
    """kind is e.g. 'capture' or 'result'."""
    return "blmn_{0}_{1}.{2}".format(kind, timestamp(), ext)


def make_unique_filename(kind, ext="png"):
    """Like make_filename but with a short random suffix.

    Used for per-job files (e.g. the input copy each Generate snapshots), so
    several generations fired within the same second never collide.
    """
    return "blmn_{0}_{1}_{2}.{3}".format(kind, timestamp(), uuid.uuid4().hex[:6], ext)


def load_image(filepath, name=None, reuse=True):
    """Load an image datablock from disk, reloading if it already exists.

    Returns the bpy image or None on failure.
    """
    if not filepath or not os.path.isfile(filepath):
        log("load_image: file missing:", filepath)
        return None

    abspath = bpy.path.abspath(filepath)
    basename = name or os.path.basename(filepath)

    if reuse:
        for img in bpy.data.images:
            try:
                if img.filepath and bpy.path.abspath(img.filepath) == abspath:
                    img.reload()
                    return img
            except Exception:  # noqa: BLE001 - defensive against odd image states
                continue

    try:
        img = bpy.data.images.load(abspath, check_existing=True)
        img.name = basename
        return img
    except RuntimeError as exc:
        log("load_image failed:", exc)
        return None


def open_in_image_editor(image, context=None):
    """Show the given image in an Image Editor.

    Reuses a visible Image Editor if one exists. Returns False if the user has
    not selected a preview area yet.
    """
    if image is None:
        return False

    ctx = context or bpy.context

    # 1. Reuse a visible Image Editor.
    for window in ctx.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "IMAGE_EDITOR":
                area.spaces.active.image = image
                area.tag_redraw()
                return True

    return False


def _set_image_editor_area(area, image=None):
    _force_area_type(area, "IMAGE_EDITOR")
    space = area.spaces.active
    if space.type != "IMAGE_EDITOR":
        log("Preview area did not become an Image Editor:", space.type)
        return False
    if hasattr(space, "mode"):
        space.mode = "VIEW"
    if image is not None:
        space.image = image
    area.tag_redraw()
    return True


def _force_area_type(area, area_type):
    if area.type == area_type and area.spaces.active.type != area_type:
        area.type = "VIEW_3D" if area_type != "VIEW_3D" else "IMAGE_EDITOR"
    area.type = area_type


def show_image_in_area(context, area, image=None, window=None):
    """Turn an existing editor area into the add-on preview Image Editor."""
    ctx = context or bpy.context
    win = window or ctx.window
    if area is None or win is None:
        return False
    try:
        if not _set_image_editor_area(area, image):
            return False
        _fit_image_view(ctx, win, area)
        area.tag_redraw()
        return True
    except Exception as exc:  # noqa: BLE001
        log("Could not use selected preview area:", exc)
        return False


def show_preview_in_window(context, window_index, image=None):
    return False
    """Ensure an Image Editor in the chosen window and show image in it.

    Legacy helper disabled; use the preview-area selector instead. For an existing window: reuse
    its Image Editor if present, otherwise split its largest area in half and
    turn the new half into an Image Editor (non-destructive — the original area
    is only resized, never replaced).
    """


def _fit_image_view(ctx, window, area, screen=None):
    """Best-effort 'View All' so the image fits the editor."""
    region = next((r for r in area.regions if r.type == "WINDOW"), None)
    if region is None:
        return
    try:
        override = {"window": window, "area": area, "region": region}
        if screen is not None:
            override["screen"] = screen
        with ctx.temp_override(**override):
            bpy.ops.image.view_all(fit_view=True)
    except Exception:  # noqa: BLE001 - cosmetic only
        pass


def tag_redraw_all():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
    except Exception:  # noqa: BLE001 - never fail because of a redraw
        pass


def open_folder(path):
    """Best-effort cross-platform 'reveal in file browser'."""
    import sys
    import subprocess

    if not path or not os.path.isdir(path):
        return False
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606 - intended OS file browser
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception as exc:  # noqa: BLE001
        log("open_folder failed:", exc)
        return False
