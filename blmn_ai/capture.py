"""Capture the active camera view or the current viewport to a PNG.

Uses GPU offscreen rendering (this is a reference image for the AI, not a
final F12 render). Works on Blender 4.0+. Never modifies the user's scene.
"""
import os
import bpy
import gpu

from . import utils

# Long edge of the capture sent to the API. The AI output resolution is a
# separate server-side setting; this only needs to carry the composition.
CAPTURE_LONG_EDGE = 1600


def _resolve_capture_dimensions(scene, long_edge):
    """Return (width, height) preserving the render aspect ratio, scaled so the
    longest edge equals long_edge."""
    rx = scene.render.resolution_x
    ry = scene.render.resolution_y
    pa_x = scene.render.pixel_aspect_x or 1.0
    pa_y = scene.render.pixel_aspect_y or 1.0

    eff_x = rx * pa_x
    eff_y = ry * pa_y
    if eff_x <= 0 or eff_y <= 0:
        eff_x, eff_y = 1.0, 1.0

    if eff_x >= eff_y:
        width = long_edge
        height = max(1, int(round(long_edge * eff_y / eff_x)))
    else:
        height = long_edge
        width = max(1, int(round(long_edge * eff_x / eff_y)))
    return width, height


def _find_view3d(context):
    """Return (area, space, region) of a 3D viewport, preferring the active one."""
    if context.space_data and context.space_data.type == "VIEW_3D" and context.area:
        for region in context.area.regions:
            if region.type == "WINDOW":
                return context.area, context.space_data, region
    for area in context.screen.areas:
        if area.type == "VIEW_3D":
            for region in area.regions:
                if region.type == "WINDOW":
                    return area, area.spaces.active, region
    raise ValueError("Camera capture failed — no 3D viewport available.")


def capture_view(context, source, filepath, long_edge=CAPTURE_LONG_EDGE):
    """Render the chosen view to filepath (PNG). source: 'CAMERA' | 'VIEWPORT'.

    Returns filepath on success. Raises ValueError with a short, user-facing
    message on failure.
    """
    scene = context.scene
    area, space, region = _find_view3d(context)

    if source == "CAMERA":
        camera = scene.camera
        if camera is None or camera.type != "CAMERA":
            raise ValueError("No active camera — set one or capture the viewport instead.")
        width, height = _resolve_capture_dimensions(scene, long_edge)
        view_matrix = camera.matrix_world.inverted()
        projection_matrix = camera.calc_matrix_camera(
            context.evaluated_depsgraph_get(),
            x=width,
            y=height,
            scale_x=scene.render.pixel_aspect_x or 1.0,
            scale_y=scene.render.pixel_aspect_y or 1.0,
        )
    else:
        rv3d = space.region_3d
        if rv3d is None:
            raise ValueError("Camera capture failed — no 3D viewport available.")
        # Preserve the viewport's aspect ratio.
        rw, rh = max(1, region.width), max(1, region.height)
        if rw >= rh:
            width, height = long_edge, max(1, int(round(long_edge * rh / rw)))
        else:
            width, height = max(1, int(round(long_edge * rw / rh))), long_edge
        view_matrix = rv3d.view_matrix.copy()
        projection_matrix = rv3d.window_matrix.copy()

    offscreen = None
    try:
        offscreen = gpu.types.GPUOffScreen(width, height)
        with offscreen.bind():
            fb = gpu.state.active_framebuffer_get()
            fb.clear(color=(0.0, 0.0, 0.0, 1.0), depth=1.0)
            offscreen.draw_view3d(
                scene,
                context.view_layer,
                space,
                region,
                view_matrix,
                projection_matrix,
                do_color_management=True,
            )
            buffer = fb.read_color(0, 0, width, height, 4, 0, "UBYTE")
    except Exception as exc:  # noqa: BLE001
        utils.log("Offscreen capture failed:", exc)
        raise ValueError("Capture failed — the view could not be rendered.")
    finally:
        if offscreen is not None:
            offscreen.free()

    return _write_buffer_to_png(buffer, width, height, filepath)


def _write_buffer_to_png(buffer, width, height, filepath):
    """Convert a GPU UBYTE buffer to a PNG via a temporary bpy image."""
    img = None
    try:
        buffer.dimensions = width * height * 4
        img = bpy.data.images.new("blmn_capture_tmp", width, height, alpha=True)
        img.pixels.foreach_set([v / 255.0 for v in buffer])

        img.filepath_raw = filepath
        img.file_format = "PNG"
        img.save()
    except Exception as exc:  # noqa: BLE001
        utils.log("PNG write failed:", exc)
        raise ValueError("Capture failed — the image could not be saved.")
    finally:
        if img is not None:
            bpy.data.images.remove(img)

    if not os.path.isfile(filepath):
        raise ValueError("Capture failed — the image could not be saved.")
    return filepath
