# blmn.ai — Blender add-on.
#
# Distributed as a legacy (bl_info) add-on so it can self-update from this
# repository's GitHub releases via the bundled CGCookie addon updater. The
# canonical source of truth is the Blue-Moon-Virtual/blmn-ai-blender-addon
# repository; blmn.ai/blender links to the latest release binary.
import bpy
import importlib

bl_info = {
    "name": "blmn.ai",
    "author": "blmn.ai <support@blmn.ai>",
    "version": (1, 1, 4),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar (N) > blmn.ai",
    "description": "Render your viewport or camera view with blmn.ai",
    "doc_url": "https://blmn.ai/blender",
    "tracker_url": "https://blmn.ai/blender",
    "category": "Render",
}

if "properties" in locals():
    # Re-enable / reload in a running session: reload submodules.
    importlib.reload(addon_updater)
    importlib.reload(addon_updater_ops)
    importlib.reload(utils)
    importlib.reload(net)
    importlib.reload(properties)
    importlib.reload(preferences)
    importlib.reload(capture)
    importlib.reload(history)
    importlib.reload(operators)
    importlib.reload(panels)
else:
    from . import addon_updater
    from . import addon_updater_ops
    from . import utils
    from . import net
    from . import properties
    from . import preferences
    from . import capture
    from . import history
    from . import operators
    from . import panels


def register():
    # Register the updater first so its operators exist before the add-on's own
    # preferences draw() calls into them.
    addon_updater_ops.register(bl_info)

    properties.register()
    preferences.register()
    operators.register()
    panels.register()

    bpy.types.Scene.blmn_ai = bpy.props.PointerProperty(type=properties.BLMNProperties)


def unregister():
    if hasattr(bpy.types.Scene, "blmn_ai"):
        del bpy.types.Scene.blmn_ai

    for module in (panels, operators, preferences, properties):
        try:
            module.unregister()
        except Exception as exc:  # noqa: BLE001 - tolerate Blender partial reload state.
            if "missing bl_rna" not in str(exc) and "is not registered" not in str(exc):
                raise
            utils.log("Module unregister skipped:", module.__name__, exc)

    addon_updater_ops.unregister()


if __name__ == "__main__":
    register()
