"""Shared helpers: paths, filenames, image loading, console logging.

Keep user-facing errors short (panel/report); log technical detail to console.
"""
import os
import time
import bpy

LOG_PREFIX = "[blmn.ai]"
PREVIEW_WORKSPACE_NAME = "blmn.ai"


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

    Reuses a visible Image Editor if one exists; otherwise opens/creates the
    blmn.ai workspace and shows it there. Returns True if the image is on
    screen.
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

    # 2. None open: use the add-on workspace.
    return ensure_preview_workspace(ctx, image)


def ensure_preview_workspace(context=None, image=None):
    """Open/create the blmn.ai workspace and ensure it has an Image Editor."""
    ctx = context or bpy.context
    win = ctx.window
    if win is None:
        return False

    workspace = bpy.data.workspaces.get(PREVIEW_WORKSPACE_NAME)
    if workspace is None:
        workspace = _duplicate_current_workspace(ctx)
        if workspace is None:
            return False
        workspace.name = PREVIEW_WORKSPACE_NAME

    try:
        win.workspace = workspace
    except Exception as exc:  # noqa: BLE001
        log("Could not activate preview workspace:", exc)
        return False

    return _configure_preview_workspace(ctx, win, workspace, image)


def _duplicate_current_workspace(ctx):
    """Duplicate the active workspace so Blender places it beside the source tab."""
    try:
        current = ctx.window.workspace
        before = {workspace.as_pointer() for workspace in bpy.data.workspaces}
        bpy.ops.workspace.duplicate()
        for workspace in bpy.data.workspaces:
            if workspace.as_pointer() not in before:
                return workspace
        if ctx.window.workspace != current:
            return ctx.window.workspace
        log("Preview workspace duplicate produced no new workspace.")
        return None
    except Exception as exc:  # noqa: BLE001
        log("Could not create preview workspace:", exc)
        return None


def _configure_preview_workspace(ctx, window, workspace, image=None):
    """Layout: Image Editor left, 3D View middle, existing utility areas right."""
    screen = workspace.screens[0] if workspace.screens else window.screen
    image_areas = [area for area in screen.areas if area.type == "IMAGE_EDITOR"]
    view_areas = sorted(
        (area for area in screen.areas if area.type == "VIEW_3D"),
        key=lambda area: area.x,
    )

    if not image_areas:
        if len(view_areas) >= 2:
            image_area = view_areas[0]
            view_area = view_areas[-1]
        else:
            target = _largest_area(screen, preferred_type="VIEW_3D")
            if target is None:
                return False

            region = next((r for r in target.regions if r.type == "WINDOW"), None)
            before = {area.as_pointer() for area in screen.areas}
            try:
                with ctx.temp_override(window=window, screen=screen, area=target, region=region):
                    bpy.ops.screen.area_split(direction="VERTICAL", factor=0.32)
            except Exception as exc:  # noqa: BLE001
                log("Could not split preview workspace:", exc)
                return False

            created = [area for area in screen.areas if area.as_pointer() not in before]
            pair = created + [target]
            pair.sort(key=lambda area: area.x)
            image_area = pair[0]
            view_area = pair[-1]
        _set_image_editor_area(image_area, image)
        _set_view3d_area(view_area)
    else:
        image_area = min(image_areas, key=lambda area: area.x)
        view_area = _preview_view_area(screen, image_area)
        if view_area is not None:
            _set_view3d_area(view_area)

    _set_image_editor_area(image_area, image)
    _fit_image_view(ctx, window, image_area, screen)
    image_area.tag_redraw()
    return True


def _preview_view_area(screen, image_area):
    view_areas = [area for area in screen.areas if area.type == "VIEW_3D"]
    if not view_areas:
        return None
    right_of_image = [area for area in view_areas if area.x > image_area.x]
    if right_of_image:
        return max(right_of_image, key=lambda area: area.width * area.height)
    return max(view_areas, key=lambda area: area.width * area.height)


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


def _set_view3d_area(area):
    _force_area_type(area, "VIEW_3D")
    space = area.spaces.active
    if space.type != "VIEW_3D":
        log("Preview workspace view area did not become a 3D View:", space.type)
        return False
    if hasattr(space, "show_region_ui"):
        space.show_region_ui = True
    area.tag_redraw()
    return True


def _force_area_type(area, area_type):
    if area.type == area_type and area.spaces.active.type != area_type:
        area.type = "VIEW_3D" if area_type != "VIEW_3D" else "IMAGE_EDITOR"
    area.type = area_type


def _largest_area(screen, preferred_type=None):
    areas = list(screen.areas)
    if preferred_type is not None:
        preferred = [area for area in areas if area.type == preferred_type]
        if preferred:
            areas = preferred
    if not areas:
        return None
    return max(areas, key=lambda area: area.width * area.height)


def show_image_in_area(context, area, image=None, window=None):
    """Turn an existing editor area into the add-on preview Image Editor."""
    ctx = context or bpy.context
    win = window or ctx.window
    if area is None or win is None:
        return False
    try:
        area.type = "IMAGE_EDITOR"
        if image is not None:
            area.spaces.active.image = image
        _fit_image_view(ctx, win, area)
        area.tag_redraw()
        return True
    except Exception as exc:  # noqa: BLE001
        log("Could not use selected preview area:", exc)
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

    window_index < 0 opens the blmn.ai workspace. For an existing window: reuse
    its Image Editor if present, otherwise split its largest area in half and
    turn the new half into an Image Editor (non-destructive — the original area
    is only resized, never replaced).
    """
    ctx = context
    if window_index < 0:
        return ensure_preview_workspace(ctx, image)

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
