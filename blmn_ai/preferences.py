"""Add-on Preferences: account connection, storage, environment.

Connecting works with a one-time code from https://blmn.ai/blender — no
passwords or API keys are ever typed into Blender. The resulting device token
only allows rendering/upload/credit endpoints and can be revoked from the web
app at any time.
"""
import os
import webbrowser

import bpy
from bpy.types import AddonPreferences, Operator
from bpy.props import StringProperty, IntProperty, EnumProperty, BoolProperty

from . import net
from . import utils
from . import addon_updater_ops


class BLMN_OT_open_connect_page(Operator):
    bl_idname = "blmn.open_connect_page"
    bl_label = "Get Connect Code"
    bl_description = "Open blmn.ai/blender in your browser to generate a connect code"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        prefs = utils.prefs(context)
        webbrowser.open(net.SITE_BASES.get(prefs.api_environment, net.SITE_BASES["PRODUCTION"]) + "/blender#connect")
        return {"FINISHED"}


class BLMN_OT_link_account(Operator):
    bl_idname = "blmn.link_account"
    bl_label = "Link Account"
    bl_description = "Exchange the connect code for a secure device token"
    bl_options = {"REGISTER", "INTERNAL"}

    @classmethod
    def poll(cls, context):
        return bool(utils.prefs(context).connect_code.strip())

    def execute(self, context):
        prefs = utils.prefs(context)
        prefs.apply_access()
        api_base = prefs.api_base()
        try:
            payload = net.exchange_code(api_base, prefs.connect_code.strip())
            prefs.device_token = payload["token"]
            prefs.account_email = str(payload.get("email") or "")
            prefs.connect_code = ""
            net.ACCOUNT["email"] = prefs.account_email
            try:
                net.fetch_credits(api_base, prefs.device_token)
            except net.ApiError:
                pass
        except net.ApiError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        self.report({"INFO"}, "Connected to blmn.ai" + (
            " as " + prefs.account_email if prefs.account_email else "."))
        utils.tag_redraw_all()
        return {"FINISHED"}


class BLMN_OT_unlink_account(Operator):
    bl_idname = "blmn.unlink_account"
    bl_label = "Disconnect"
    bl_description = "Sign out and revoke this device's token on the server"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        prefs = utils.prefs(context)
        prefs.apply_access()
        if prefs.device_token:
            net.revoke_self(prefs.api_base(), prefs.device_token)
        prefs.device_token = ""
        prefs.account_email = ""
        net.reset_account_cache()
        self.report({"INFO"}, "Disconnected from blmn.ai.")
        utils.tag_redraw_all()
        return {"FINISHED"}


class BLMNPreferences(AddonPreferences):
    bl_idname = __package__

    # The device token is the only credential stored. It is hashed server-side,
    # limited to render/upload/credits endpoints, and revocable from the web app.
    device_token: StringProperty(
        name="Device Token",
        default="",
        subtype="PASSWORD",
        options={"HIDDEN"},
    )
    account_email: StringProperty(name="Account", default="")
    connect_code: StringProperty(
        name="Connect Code",
        description="One-time code from blmn.ai/blender (e.g. AB2C-D3EF)",
        default="",
    )

    # Storage
    output_folder: StringProperty(
        name="Output Folder",
        description="Where captures and results are saved (empty = Blender user folder)",
        subtype="DIR_PATH",
        default="",
    )
    api_environment: EnumProperty(
        name="Environment",
        items=[
            ("PRODUCTION", "Production", "blmn.ai"),
            ("STAGING", "Staging", "staging.blmn.ai (internal testing)"),
        ],
        default="PRODUCTION",
    )

    # Cloudflare Access service token — internal staging testing only. The
    # staging API sits behind Cloudflare Access; a service token is the
    # supported way for a headless client to pass that gate. Never needed on
    # Production. These are NOT blmn.ai credentials.
    cf_access_client_id: StringProperty(
        name="CF Access Client ID",
        description="Cloudflare Access service-token Client ID (staging testing only)",
        default="",
    )
    cf_access_client_secret: StringProperty(
        name="CF Access Client Secret",
        description="Cloudflare Access service-token Client Secret (staging testing only)",
        default="",
        subtype="PASSWORD",
        options={"HIDDEN"},
    )

    # --- CGCookie addon-updater settings (read by addon_updater_ops UI) ---
    auto_check_update: BoolProperty(
        name="Auto-check for Update",
        description="If enabled, check blmn.ai releases for a newer version on the chosen interval",
        default=True,
    )
    updater_interval_months: IntProperty(
        name="Months",
        description="Number of months between update checks",
        default=0,
        min=0,
    )
    updater_interval_days: IntProperty(
        name="Days",
        description="Number of days between update checks",
        default=1,
        min=0,
        max=31,
    )
    updater_interval_hours: IntProperty(
        name="Hours",
        description="Number of hours between update checks",
        default=0,
        min=0,
        max=23,
    )
    updater_interval_minutes: IntProperty(
        name="Minutes",
        description="Number of minutes between update checks",
        default=0,
        min=0,
        max=59,
    )

    def api_base(self):
        return net.API_BASES.get(self.api_environment, net.API_BASES["PRODUCTION"])

    def apply_access(self):
        """Install CF Access service-token headers for staging; clear otherwise."""
        if self.api_environment == "STAGING":
            net.set_access_headers(self.cf_access_client_id, self.cf_access_client_secret)
        else:
            net.set_access_headers("", "")

    def connected(self):
        return bool(self.device_token)

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.label(text="blmn.ai Account", icon="USER")
        if self.connected():
            row = box.row()
            row.label(text="Connected" + (" — " + self.account_email if self.account_email else ""),
                      icon="CHECKMARK")
            box.operator("blmn.unlink_account", icon="UNLINKED")
        else:
            col = box.column()
            col.label(text="1. Get a one-time code from blmn.ai/blender")
            col.label(text="2. Paste it below and click Link Account")
            row = box.row(align=True)
            row.operator("blmn.open_connect_page", icon="URL")
            row = box.row(align=True)
            row.prop(self, "connect_code", text="Code")
            row.operator("blmn.link_account", icon="LINKED")

        box = layout.box()
        box.label(text="Storage", icon="FILE_FOLDER")
        box.prop(self, "output_folder")

        if _show_internal_settings(self):
            box = layout.box()
            box.label(text="Internal", icon="PREFERENCES")
            box.prop(self, "api_environment")
            if self.api_environment == "STAGING":
                col = box.column(align=True)
                col.label(text="Cloudflare Access service token (staging only):", icon="LOCKED")
                col.prop(self, "cf_access_client_id")
                col.prop(self, "cf_access_client_secret")
                col.label(text="Leave empty on Production.", icon="INFO")

        # Updates — CGCookie addon updater UI (auto-check toggle, interval,
        # "Check now" / "Update now" buttons). Pulls from this repo's releases.
        box = layout.box()
        box.label(text="Updates", icon="FILE_REFRESH")
        addon_updater_ops.update_settings_ui(self, context, element=box)


def _show_internal_settings(prefs):
    """Expose staging controls only for internal sessions or existing staging users."""
    if prefs.api_environment == "STAGING":
        return True
    return os.environ.get("BLMN_AI_INTERNAL") == "1"


_classes = (
    BLMN_OT_open_connect_page,
    BLMN_OT_link_account,
    BLMN_OT_unlink_account,
    BLMNPreferences,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError as exc:
            if "missing bl_rna" not in str(exc) and "is not registered" not in str(exc):
                raise
            utils.log("Preferences unregister skipped:", cls.__name__, exc)
