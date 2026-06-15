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


def open_in_image_editor(image, context=None):
    """Show the given image in an Image Editor.

    Reuses a visible Image Editor if one exists; otherwise opens a dedicated
    Image Editor window (like Blender's own "Render → New Window" display), so
    the button always shows the result without the user having to split their
    layout first. Returns True if the image is now on screen.
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

    # 2. None open — spawn a dedicated window for it.
    return _open_image_in_new_window(image, ctx)


def _open_image_in_new_window(image, ctx):
    """Open a new window and turn its largest area into an Image Editor."""
    try:
        win_count = len(ctx.window_manager.windows)
        bpy.ops.wm.window_new()
        windows = ctx.window_manager.windows
        if len(windows) <= win_count:
            return False

        win = windows[-1]
        area = max(win.screen.areas, key=lambda a: a.width * a.height)
        area.type = "IMAGE_EDITOR"
        if image is not None:
            area.spaces.active.image = image

        _fit_image_view(ctx, win, area)
        return True
    except Exception as exc:  # noqa: BLE001
        log("Could not open image window:", exc)
        return False


def has_image_editor(context=None):
    """True if any Image Editor is currently visible in any window."""
    ctx = context or bpy.context
    for window in ctx.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "IMAGE_EDITOR":
                return True
    return False


def show_preview_in_window(context, window_index, image=None):
    """Ensure an Image Editor in the chosen window and show image in it.

    window_index < 0 opens a new floating window. For an existing window: reuse
    its Image Editor if present, otherwise split its largest area in half and
    turn the new half into an Image Editor (non-destructive — the original area
    is only resized, never replaced).
    """
    ctx = context
    if window_index < 0:
        return _open_image_in_new_window(image, ctx)

    windows = ctx.window_manager.windows
    if not (0 <= window_index < len(windows)):
        return False
    win = windows[window_index]

    # Reuse an Image Editor already in this window.
    for area in win.screen.areas:
        if area.type == "IMAGE_EDITOR":
            if image is not None:
                area.spaces.active.image = image
            _fit_image_view(ctx, win, area)
            area.tag_redraw()
            return True

    # Otherwise split the largest area and make the new half an Image Editor.
    try:
        area = max(win.screen.areas, key=lambda a: a.width * a.height)
        region = next((r for r in area.regions if r.type == "WINDOW"), None)
        before = {a.as_pointer() for a in win.screen.areas}
        with ctx.temp_override(window=win, area=area, region=region):
            bpy.ops.screen.area_split(direction="VERTICAL", factor=0.5)
        new_areas = [a for a in win.screen.areas if a.as_pointer() not in before]
        target = new_areas[0] if new_areas else area
        target.type = "IMAGE_EDITOR"
        if image is not None:
            target.spaces.active.image = image
        _fit_image_view(ctx, win, target)
        target.tag_redraw()
        return True
    except Exception as exc:  # noqa: BLE001
        log("Could not split window for preview:", exc)
        return False


def _fit_image_view(ctx, window, area):
    """Best-effort 'View All' so the image fits the editor."""
    region = next((r for r in area.regions if r.type == "WINDOW"), None)
    if region is None:
        return
    try:
        with ctx.temp_override(window=window, area=area, region=region):
            bpy.ops.image.view_all(fit_view=True)
    except Exception:  # noqa: BLE001 - cosmetic only
        pass


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
