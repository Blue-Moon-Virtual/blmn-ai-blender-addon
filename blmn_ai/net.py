"""HTTP client for the blmn.ai API.

All functions here are blocking and must only be called from a worker thread
(the Generate operator owns that thread) — never from Blender's main thread.

Authentication uses a device token (``blmn_pat_…``) obtained by pasting a
one-time connect code from https://blmn.ai/blender. The server enforces the
allowed API surface and charges credits; nothing here can change pricing.
"""
import base64
import json
import os
import platform
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import utils

USER_AGENT = "blmn-ai-blender-addon/1.0"

API_BASES = {
    "PRODUCTION": "https://api.blmn.ai",
    "STAGING": "https://stagingapi.blmn.ai",
}
SITE_BASES = {
    "PRODUCTION": "https://blmn.ai",
    "STAGING": "https://staging.blmn.ai",
}

# The add-on is a personal device login, so it always operates on the user's
# personal credit balance. This must match where renders are charged: the
# server defaults *writes* (render spend) to personal, but defaults *reads*
# (/me/credits) to team billing when the user belongs to a paid team — which
# would otherwise show the team's balance instead of the personal one. Sending
# this mode explicitly keeps the displayed balance and the charged balance in
# sync.
BILLING_MODE = "personal"

# Runtime (non-persisted) account snapshot shown in the panel.
ACCOUNT = {
    "balance": None,
    "plan": "",
    "email": "",
    "checked_at": 0.0,
}

# Optional Cloudflare Access service-token headers. Only used for internal
# staging testing, where the API sits behind Cloudflare Access; a service token
# is the supported way for a headless client to pass that gate without weakening
# it. Empty on production. Set from preferences via set_access_headers().
_ACCESS_HEADERS = {}


def set_access_headers(client_id, client_secret):
    """Install (or clear) Cloudflare Access service-token headers."""
    _ACCESS_HEADERS.clear()
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if client_id and client_secret:
        _ACCESS_HEADERS["CF-Access-Client-Id"] = client_id
        _ACCESS_HEADERS["CF-Access-Client-Secret"] = client_secret


class ApiError(Exception):
    """Error with a short, user-facing message."""


def reset_account_cache():
    ACCOUNT.update({"balance": None, "plan": "", "email": "", "checked_at": 0.0})


# Background credit-refresh state. The panel and the Refresh operator both kick
# an async fetch; this guards against piling up duplicate requests (and against
# hammering the server when a fetch keeps failing). bpy is never touched here —
# callers redraw on the main thread via an app timer once the fetch settles.
_refresh_lock = threading.Lock()
_refresh_inflight = False
_last_refresh_attempt = 0.0
_AUTO_REFRESH_MIN_INTERVAL = 20.0


def is_refresh_inflight():
    return _refresh_inflight


def should_auto_refresh():
    """True if it's worth kicking an automatic credit refresh right now."""
    return (not _refresh_inflight) and (time.time() - _last_refresh_attempt > _AUTO_REFRESH_MIN_INTERVAL)


def refresh_credits_async(api_base, token):
    """Fetch credits in a daemon thread. Returns True if a fetch was started."""
    global _refresh_inflight, _last_refresh_attempt
    with _refresh_lock:
        if _refresh_inflight:
            return False
        _refresh_inflight = True
        _last_refresh_attempt = time.time()

    def worker():
        global _refresh_inflight
        try:
            fetch_credits(api_base, token)
        except ApiError as exc:
            utils.log("Credits refresh failed:", exc)
        except Exception as exc:  # noqa: BLE001 - last-resort guard for the thread
            utils.log("Credits refresh error:", exc)
        finally:
            _refresh_inflight = False

    threading.Thread(target=worker, daemon=True).start()
    return True


def _ssl_context():
    try:
        return ssl.create_default_context()
    except Exception:  # noqa: BLE001 - very defensive; only hit on broken installs
        return None


def _request(method, url, token=None, body=None, timeout=60):
    """Perform an HTTP request, return (status, parsed_json_or_None).

    Raises ApiError for transport failures. HTTP error statuses are returned,
    not raised, so callers can map them to user-facing messages.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    # Pass a Cloudflare Access gate (staging only) when a service token is set.
    headers.update(_ACCESS_HEADERS)

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    final_url = url
    content_type = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            raw = resp.read()
            status = resp.status
            final_url = resp.geturl() or url
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
        try:
            final_url = exc.geturl() or url
            content_type = exc.headers.get("Content-Type", "")
        except Exception:  # noqa: BLE001
            pass
    except urllib.error.URLError as exc:
        utils.log("Network error:", url, exc)
        raise ApiError("Could not reach blmn.ai — check your internet connection.")
    except Exception as exc:  # noqa: BLE001
        utils.log("Request failed:", url, exc)
        raise ApiError("Network request failed.")

    # If we were redirected to a different host, the API is sitting behind an
    # access wall (e.g. Cloudflare Access on a non-production environment). A
    # headless add-on cannot pass SSO, so surface this clearly instead of a
    # vague "connecting failed".
    try:
        req_host = urllib.parse.urlsplit(url).netloc
        final_host = urllib.parse.urlsplit(final_url).netloc or req_host
    except Exception:  # noqa: BLE001
        req_host = final_host = ""
    if req_host and final_host and final_host != req_host:
        utils.log("Redirected away from API:", url, "->", final_url, "status", status)
        raise ApiError(
            "The blmn.ai API redirected to '{0}', which the add-on cannot pass. "
            "The API is behind access control — use Environment: Production, or "
            "for internal staging testing add a Cloudflare Access service token "
            "in the add-on preferences.".format(final_host))

    parsed = None
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            parsed = None

    # A successful HTTP status carrying an HTML body (not JSON) is another
    # tell-tale of a login/redirect page rather than the real API.
    if parsed is None and status == 200 and "html" in content_type.lower():
        utils.log("Expected JSON but got HTML from", final_url)
        raise ApiError(
            "The blmn.ai API returned a web page instead of data. Check the "
            "Environment setting in the add-on preferences.")

    return status, parsed


def _error_message(status, payload, fallback):
    if status == 401:
        return "Not connected — link your blmn.ai account in the add-on preferences."
    if isinstance(payload, dict):
        msg = payload.get("error") or payload.get("message")
        if isinstance(msg, str) and msg.strip():
            low = msg.strip().lower()
            if "insufficient" in low or "credit" in low:
                return "Not enough credits — top up or upgrade on blmn.ai."
            return msg.strip()[:200]
    if status == 402:
        return "Not enough credits — top up or upgrade on blmn.ai."
    if status == 403:
        return "This action is not available for your account or plan."
    return fallback


def device_name():
    try:
        import bpy
        host = platform.node() or platform.system() or "computer"
        return "Blender {0} on {1}".format(
            ".".join(str(v) for v in bpy.app.version[:2]), host)
    except Exception:  # noqa: BLE001
        return "Blender add-on"


# ---------------- Account ----------------

def exchange_code(api_base, code):
    """Exchange a one-time connect code for a device token."""
    status, payload = _request(
        "POST", api_base + "/api/device/exchange",
        body={"code": code, "deviceName": device_name()}, timeout=30,
    )
    if status == 200 and isinstance(payload, dict) and payload.get("token"):
        return payload
    if status in (400, 401):
        raise ApiError("Invalid or expired connect code — generate a new one on blmn.ai/blender.")
    utils.log("Exchange failed:", status, payload)
    raise ApiError(_error_message(
        status, payload,
        "Connecting failed (server returned {0}) — please try again.".format(status)))


def fetch_credits(api_base, token):
    """GET /me/credits → updates the ACCOUNT cache, returns (balance, plan)."""
    status, payload = _request(
        "GET", api_base + "/me/credits?billingMode=" + BILLING_MODE,
        token=token, timeout=30)
    if status != 200 or not isinstance(payload, dict):
        raise ApiError(_error_message(status, payload, "Could not load credits."))
    # Top-level balance reflects the requested (personal) billing mode; fall
    # back to the explicit personal subscription block if ever absent.
    balance = payload.get("balance")
    if not isinstance(balance, (int, float)):
        personal = payload.get("personalSubscription")
        if isinstance(personal, dict):
            balance = personal.get("balance")
    plan = payload.get("plan") or "free"
    ACCOUNT["balance"] = int(balance) if isinstance(balance, (int, float)) else None
    ACCOUNT["plan"] = str(plan)
    ACCOUNT["checked_at"] = time.time()
    return ACCOUNT["balance"], ACCOUNT["plan"]


def fetch_me(api_base, token):
    status, payload = _request("GET", api_base + "/api/device/me", token=token, timeout=30)
    if status != 200 or not isinstance(payload, dict):
        raise ApiError(_error_message(status, payload, "Could not verify the connection."))
    ACCOUNT["email"] = str(payload.get("email") or "")
    return payload


def revoke_self(api_base, token):
    """Best-effort disconnect: revoke this device token server-side."""
    try:
        _request("DELETE", api_base + "/api/device/self", token=token, timeout=15)
    except ApiError:
        pass


# ---------------- Upload / render / result ----------------

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _data_url(filepath):
    """Read an image file and return a base64 data URL with the right mime."""
    ext = os.path.splitext(filepath)[1].lower()
    mime = _MIME_BY_EXT.get(ext, "image/png")
    with open(filepath, "rb") as fh:
        encoded = base64.b64encode(fh.read()).decode("ascii")
    return "data:{0};base64,{1}".format(mime, encoded)


def upload_image(api_base, token, filepath, prompt, kind="blender-capture"):
    """POST /api/save with the image as a data URL. Returns the stored image URL."""
    try:
        data_url = _data_url(filepath)
    except OSError as exc:
        utils.log("Image read failed:", filepath, exc)
        raise ApiError("Could not read the image file.")

    body = {
        "image": data_url,
        "prompt": prompt or "",
        "type": kind,
    }
    status, payload = _request("POST", api_base + "/api/save", token=token, body=body, timeout=180)
    if status != 200 or not isinstance(payload, dict) or not payload.get("url"):
        raise ApiError(_error_message(status, payload, "Upload failed — please try again."))
    return str(payload["url"])


def upload_capture(api_base, token, filepath, prompt):
    """Upload the viewport/camera capture. Returns the stored image URL."""
    return upload_image(api_base, token, filepath, prompt, kind="blender-capture")


def start_render(api_base, token, route, payload):
    """POST the render request. Returns the parsed response (200 or 202 body)."""
    status, parsed = _request("POST", api_base + route, token=token, body=payload, timeout=120)
    if status in (200, 202) and isinstance(parsed, dict):
        return parsed
    raise ApiError(_error_message(status, parsed, "Render request failed."))


def wait_render(api_base, token, wait_route, queued_payload, cancel_event, on_status=None,
                max_seconds=900):
    """Poll the server-side wait endpoint until the render finishes.

    Returns the result image URL. Raises ApiError on failure/cancel/timeout.
    """
    response_url = queued_payload.get("response_url")
    generation = queued_payload.get("generation")
    ledger_key = None
    if isinstance(generation, dict):
        ledger_key = generation.get("ledger_idempotency_key")
    if not response_url:
        raise ApiError("Render request failed — queue information is missing.")

    body = {
        "response_url": response_url,
        "generation": generation,
        "ledger_idempotency_key": ledger_key,
    }
    started = time.monotonic()
    while time.monotonic() - started < max_seconds:
        if cancel_event.is_set():
            raise ApiError("Cancelled.")
        status, parsed = _request("POST", api_base + wait_route, token=token, body=body, timeout=90)
        if status != 200 or not isinstance(parsed, dict):
            raise ApiError(_error_message(status, parsed, "Render failed — server reported an error."))
        state = str(parsed.get("status") or "")
        if state == "succeeded" and parsed.get("url"):
            return str(parsed["url"]), generation
        if state == "failed":
            raise ApiError(str(parsed.get("error") or "Render failed."))
        if on_status:
            on_status("RENDERING")
        if cancel_event.wait(2.0):
            raise ApiError("Cancelled.")
    raise ApiError("Render timed out — please try again.")


def download_file(url, filepath):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=180, context=_ssl_context()) as resp:
            data = resp.read()
        with open(filepath, "wb") as fh:
            fh.write(data)
    except Exception as exc:  # noqa: BLE001
        utils.log("Download failed:", url, exc)
        raise ApiError("Download failed — the result could not be downloaded.")
    if not os.path.isfile(filepath) or os.path.getsize(filepath) == 0:
        raise ApiError("Download failed — the result could not be saved.")
    return filepath


def save_result_to_library(api_base, token, url, prompt, generation):
    """Persist the result into the user's blmn.ai library (shows up in web History).

    Best effort: failures are logged, never raised — the local file already exists.
    """
    try:
        body = {
            "url": url,
            "prompt": prompt or "",
            "type": "render",
        }
        if isinstance(generation, dict):
            body["generation"] = generation
        status, payload = _request("POST", api_base + "/api/save-from-url",
                                   token=token, body=body, timeout=120)
        if status != 200:
            utils.log("save-from-url failed:", status, payload)
    except ApiError as exc:
        utils.log("save-from-url error:", exc)


# ---------------- Job orchestration (worker thread) ----------------

def run_render_job(api_base, token, capture_path, prompt, render_request, output_dir,
                   events, cancel_event, reference_paths=None, extra_images=None,
                   result_ext="png"):
    """Full pipeline: upload → start → wait → download → persist to library.

    render_request: dict with keys 'route', 'wait_route', 'payload' (payload is
    completed here with the uploaded imageUrl).

    reference_paths: optional list of local image paths (max 4) uploaded and
    sent as referenceImageUrls, mirroring the web tool.

    extra_images: optional list of (payload_key, local_path) tuples. Each image
    is uploaded the same way as the capture and its stored URL is written to
    payload[payload_key]. Used by the animation flow to attach the last frame
    (the first frame goes through capture_path/image_key like an image render).

    result_ext: file extension for the downloaded result ('png' for images,
    'mp4' for animations).

    Emits (kind, data) tuples into the events queue:
      ('status', 'UPLOADING' | 'QUEUED' | 'RENDERING' | 'DOWNLOADING')
      ('finished', {'result_path': …, 'image_url': …})
      ('error', message)
    """
    try:
        events.put(("status", "UPLOADING"))
        image_url = upload_capture(api_base, token, capture_path, prompt)
        if cancel_event.is_set():
            raise ApiError("Cancelled.")

        payload = dict(render_request["payload"])
        payload[render_request.get("image_key", "imageUrl")] = image_url

        # Extra named images (e.g. the animation's last frame). Uploaded the
        # same way as the capture, then written to their payload key.
        for payload_key, img_path in (extra_images or []):
            if cancel_event.is_set():
                raise ApiError("Cancelled.")
            if img_path and os.path.isfile(img_path):
                payload[payload_key] = upload_image(
                    api_base, token, img_path, prompt, kind="blender-capture")

        # Reference images (optional, max 4) — uploaded the same way as the
        # capture, then passed as referenceImageUrls (same field the web uses).
        ref_urls = []
        for ref_path in (reference_paths or [])[:4]:
            if cancel_event.is_set():
                raise ApiError("Cancelled.")
            if ref_path and os.path.isfile(ref_path):
                ref_urls.append(upload_image(api_base, token, ref_path, prompt, kind="blender-reference"))
        if ref_urls:
            payload["referenceImageUrls"] = ref_urls

        events.put(("status", "QUEUED"))
        queued = start_render(api_base, token, render_request["route"], payload)

        # Unlimited model fair-use queue: wait, then retry with the queue token.
        retries = 0
        while queued.get("deferred") and queued.get("queue_token") and retries < 5:
            delay = max(1, int(queued.get("retry_after_seconds") or 1))
            for _ in range(delay):
                if cancel_event.wait(1.0):
                    raise ApiError("Cancelled.")
            payload["unlimitedQueueToken"] = queued["queue_token"]
            queued = start_render(api_base, token, render_request["route"], payload)
            retries += 1

        if queued.get("url") and not queued.get("queued"):
            # Synchronous result (older code paths).
            result_url, generation = str(queued["url"]), queued.get("generation")
        else:
            events.put(("status", "RENDERING"))
            result_url, generation = wait_render(
                api_base, token, render_request["wait_route"], queued,
                cancel_event, on_status=lambda s: events.put(("status", s)),
            )

        events.put(("status", "DOWNLOADING"))
        result_path = os.path.join(output_dir, utils.make_filename("result", result_ext))
        download_file(result_url, result_path)

        save_result_to_library(api_base, token, result_url, prompt, generation)

        try:
            fetch_credits(api_base, token)
        except ApiError:
            pass

        events.put(("finished", {"result_path": result_path, "image_url": result_url}))
    except ApiError as exc:
        events.put(("error", str(exc)))
    except Exception as exc:  # noqa: BLE001 - last-resort guard for the thread
        utils.log("Unexpected job error:", exc)
        events.put(("error", "Unexpected error — see the system console for details."))
