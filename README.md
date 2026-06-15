# blmn.ai — Blender Add-on

Render your Blender viewport or active-camera view with [blmn.ai](https://blmn.ai),
using the same API routes, credit ledger and plan gating as the web Render tool.

This repository is the **single source of truth** for the add-on. The binary
served from **[blmn.ai/blender](https://blmn.ai/blender)** is the latest
[GitHub Release](../../releases/latest) asset built from this repo, and the
add-on updates itself in place from these releases (see [Updates](#updates)).

Requires **Blender 4.2+**.

## Install

1. Download **`blmn-ai-blender-addon.zip`** from the
   [latest release](../../releases/latest) (or from blmn.ai/blender).
2. In Blender: **Edit → Preferences → Add-ons → ▾ (top-right) → Install from Disk…**,
   pick the zip, and enable **blmn.ai**.
3. Click **Get Connect Code** (opens blmn.ai/blender, requires login), paste the
   one-time code, and click **Link**. You can do this either in the add-on
   preferences or directly in the **blmn.ai** sidebar tab.
4. Render from the **blmn.ai** tab in the 3D viewport sidebar (press `N`).

## Updates

The add-on bundles the [CGCookie Blender Add-on Updater](https://github.com/CGCookie/blender-addon-updater).
With **Auto-check for Update** enabled (on by default, daily) the add-on checks
this repo's releases and offers a one-click update; you can also check manually
in **Preferences → Add-ons → blmn.ai → Updates**. Updates install
the same release-attached `blmn-ai-blender-addon.zip` that blmn.ai serves, so the
in-app updater and a fresh manual download always match.

Unlike Blender's native extension repository, this works on every Blender 4.2+
install regardless of network gateway, so updates are reliable everywhere.

## Release process

Releases are cut by tag. The [release workflow](.github/workflows/release.yml)
builds the zip and attaches it to a GitHub Release automatically:

1. Bump `version` in [`blmn_ai/__init__.py`](blmn_ai/__init__.py) (the `bl_info`
   tuple, e.g. `(1, 1, 2)`).
2. Commit, then tag and push the matching version:
   ```bash
   git tag v1.1.2
   git push origin v1.1.2
   ```
3. The workflow builds `blmn-ai-blender-addon.zip`, verifies the tag matches
   `bl_info`, and publishes a release with the zip attached.
4. `blmn.ai/blender` links to `releases/latest/download/blmn-ai-blender-addon.zip`,
   so it picks up the new binary with no further action.

To build the zip locally for testing: `./build.sh` → `dist/blmn-ai-blender-addon.zip`.

## Security model

- The add-on never sees a password or Auth0 token. The connect code
  (single-use, 10-minute expiry) is exchanged for a scoped `blmn_pat_…` device
  token. The server stores only the SHA-256 hash; tokens expire after 1 year and
  are revocable per device from blmn.ai/blender or the add-on's Disconnect button.
- The device token only authorizes render/upload/credits endpoints. Credits are
  charged server-side by the same ledger the web app uses; the add-on's cost
  label is only an estimate.

## Code layout (`blmn_ai/`)

| File | Purpose |
| --- | --- |
| `__init__.py` | `bl_info`, registration, updater bootstrap |
| `net.py` | Blocking HTTP client (urllib); `run_render_job` is the full pipeline |
| `operators.py` | Modal Generate operator (capture on main thread, worker thread + queue) |
| `capture.py` | GPU offscreen capture of the active camera / current viewport |
| `properties.py` | Models, styles, sliders mirroring the web settings bar + credit estimate |
| `preferences.py` | Connect-code pairing, token storage, output folder, updater UI |
| `panels.py` | N-panel UI; shows the update notice when a release is available |
| `history.py` | Local JSON render history |
| `utils.py` | Shared helpers |
| `addon_updater.py`, `addon_updater_ops.py` | CGCookie updater (vendored, configured for this repo) |

## License

This add-on bundles the GPLv2+ CGCookie updater and links Blender's GPL Python
API, so the add-on is distributed under the **GNU GPL v2 or later** (see
[`LICENSE`](LICENSE)). This covers the add-on source only; the blmn.ai backend,
account, and credit services remain proprietary.
