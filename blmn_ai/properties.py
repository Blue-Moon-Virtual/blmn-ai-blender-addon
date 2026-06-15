"""Scene-level settings and runtime status.

The model list, styles, sliders and credit costs mirror the web app's Render
tool settings bar. Costs shown here are estimates for the UI only — the server
is the single source of truth and charges the ledger itself.
"""
import bpy
from bpy.props import (
    StringProperty,
    EnumProperty,
    IntProperty,
    BoolProperty,
    CollectionProperty,
)
from bpy.types import PropertyGroup


# Status values used across operators and the panel.
STATUS_IDLE = "IDLE"
STATUS_CAPTURING = "CAPTURING"
STATUS_UPLOADING = "UPLOADING"
STATUS_QUEUED = "QUEUED"
STATUS_RENDERING = "RENDERING"
STATUS_DOWNLOADING = "DOWNLOADING"
STATUS_FINISHED = "FINISHED"
STATUS_FAILED = "FAILED"

STATUS_LABELS = {
    STATUS_IDLE: "Ready",
    STATUS_CAPTURING: "Capturing",
    STATUS_UPLOADING: "Uploading",
    STATUS_QUEUED: "Queued",
    STATUS_RENDERING: "Rendering",
    STATUS_DOWNLOADING: "Downloading",
    STATUS_FINISHED: "Finished",
    STATUS_FAILED: "Failed",
}

BUSY_STATUSES = {STATUS_CAPTURING, STATUS_UPLOADING, STATUS_QUEUED,
                 STATUS_RENDERING, STATUS_DOWNLOADING}

PROMPT_MAX = 1000

# Maximum reference images, matching the web tool and backend (slice(0, 4)).
MAX_REFERENCES = 4

# (wire id, label, description) — same ids the web app sends.
MODELS = [
    ("light", "Light", "Fast all-round render model (1 credit, 2 in HD)"),
    ("pro", "Pro", "Highest quality renders (2–8 credits by resolution)"),
    ("nano-banana-2-edit", "Nano Banana 2", "Strong geometry fidelity (2–8 credits by resolution)"),
    ("nano-banana-pro-edit", "Nano Banana Pro", "Premium quality (4–16 credits by resolution)"),
    ("flux-2-edit", "FLUX 2 Edit", "Creative restyling (1–4 credits by resolution)"),
    ("unlimited", "Unlimited", "Unlimited SD renders — no credits charged (Unlimited plans only)"),
]

IMAGE_STYLES = [
    ("realistic", "Photorealistic", "Photoreal render, daylight"),
    ("night_render", "Night", "Night-time photorealistic render"),
    ("sunset_render", "Sunset", "Golden-hour photorealistic render"),
    ("architectural_sketch", "Architectural Sketch", "Clean architectural sketch style"),
    ("artistic_sketch", "Artistic Sketch", "Loose artistic sketch style"),
    ("painting", "Painting", "Painterly illustration style"),
]

RENDER_TYPES = [
    ("exterior", "Exterior", "Outdoor architecture scene"),
    ("interior", "Interior", "Indoor scene"),
]

RESOLUTIONS = [
    ("1K", "1K", "Standard resolution"),
    ("2K", "2K", "High resolution"),
    ("4K", "4K", "Maximum resolution"),
]

CAPTURE_SOURCES = [
    ("CAMERA", "Active Camera", "Capture what the active scene camera sees"),
    ("VIEWPORT", "Viewport", "Capture the current 3D viewport view"),
]

# Models that take the 1K/2K/4K resolution setting.
RESOLUTION_MODELS = {"pro", "nano-banana-2-edit", "nano-banana-pro-edit", "flux-2-edit"}


def estimate_credits(model, resolution, hd, reference_count=0):
    """Mirror of the web app's cost preview. Server-side cost is authoritative."""
    res = (resolution or "1K").upper()
    factor = {"1K": 1, "2K": 2, "4K": 4}.get(res, 1)
    if model == "unlimited":
        return 0
    if model == "pro":
        base = 2 * factor
    elif model == "nano-banana-2-edit":
        base = 2 * factor
    elif model == "nano-banana-pro-edit":
        base = 4 * factor
    elif model == "flux-2-edit":
        base = 1 * factor
    else:  # light
        base = 2 if hd else 1
    return base + max(0, int(reference_count))


class BLMNReferenceImage(PropertyGroup):
    """One reference image path the AI uses to guide style/materials."""
    path: StringProperty(name="Reference", subtype="FILE_PATH", default="")


class BLMNProperties(PropertyGroup):
    source: EnumProperty(
        name="Source",
        description="Where the reference image is captured from",
        items=CAPTURE_SOURCES,
        default="CAMERA",
    )

    prompt: StringProperty(
        name="Prompt",
        description="Optional: describe materials, lighting and atmosphere. "
                    "The render style is already handled for you",
        default="",
        maxlen=PROMPT_MAX,
    )

    model: EnumProperty(
        name="Model",
        description="Which blmn.ai model renders your view",
        items=MODELS,
        default="light",
    )

    render_type: EnumProperty(
        name="Scene",
        description="Interior or exterior scene",
        items=RENDER_TYPES,
        default="exterior",
    )

    image_style: EnumProperty(
        name="Style",
        description="Output style of the render",
        items=IMAGE_STYLES,
        default="realistic",
    )

    resolution: EnumProperty(
        name="Resolution",
        description="Output resolution (affects credit cost)",
        items=RESOLUTIONS,
        default="1K",
    )

    hd: BoolProperty(
        name="HD",
        description="Higher quality output for the Light model (2 credits instead of 1)",
        default=False,
    )

    # Reference images (optional, max MAX_REFERENCES) — same as the web tool.
    references: CollectionProperty(type=BLMNReferenceImage)

    creativity: IntProperty(
        name="Creativity",
        description="How freely the AI may interpret your scene (1 = strict, 10 = bold)",
        default=3, min=1, max=10,
    )

    environment_fill: IntProperty(
        name="Environment",
        description="How much surrounding environment the AI adds (exteriors)",
        default=5, min=1, max=10,
    )

    decoration_fill: IntProperty(
        name="Decoration",
        description="How much decoration and furnishing the AI adds (interiors)",
        default=5, min=1, max=10,
    )

    seed: IntProperty(
        name="Seed",
        description="0 = random. Use the same seed to reproduce a result",
        default=0, min=0,
    )

    # Runtime state (not meant to be user-edited directly).
    status: StringProperty(name="Status", default=STATUS_IDLE)
    status_message: StringProperty(name="Status Message", default="")

    last_capture_path: StringProperty(name="Last Capture", default="", subtype="FILE_PATH")
    last_result_path: StringProperty(name="Last Result", default="", subtype="FILE_PATH")

    def status_label(self):
        return STATUS_LABELS.get(self.status, self.status)

    def is_busy(self):
        return self.status in BUSY_STATUSES

    def supports_resolution(self):
        return self.model in RESOLUTION_MODELS

    def reference_paths(self):
        """Absolute paths of the configured reference images (existing files)."""
        out = []
        for ref in self.references:
            path = (ref.path or "").strip()
            if path:
                out.append(bpy.path.abspath(path))
        return out

    def estimated_credits(self):
        return estimate_credits(self.model, self.resolution, self.hd, len(self.references))


_classes = (BLMNReferenceImage, BLMNProperties)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
