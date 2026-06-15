"""Shared helpers: paths, filenames, image loading, console logging.

Keep user-facing errors short (panel/report); log technical detail to console.
"""
import os
import time
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


def open_in_image_editor(image):
    """Show the given image in an Image Editor area if one is visible."""
    if image is None:
        return False
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "IMAGE_EDITOR":
                area.spaces.active.image = image
                return True
    return False


def tag_redraw_all():
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in {"VIEW_3D", "IMAGE_EDITOR"}:
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
