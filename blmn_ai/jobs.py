"""Concurrent render-job manager.

The add-on can run several generations at once. Each job owns its own worker
thread (running net.run_render_job), a cancel Event and a thread-safe event
queue — exactly like the old single modal operator, but now there are many.

A single app timer (_drain) runs on Blender's main thread: it drains every
job's queue, applies results to bpy data, and repaints the N-panel. That
repaint is also what animates the "generating" tray (the panel recomputes its
spinner from the wall clock each draw). The timer stops itself when no jobs are
left, so there are no idle repaints.

All bpy access happens here on the main thread; worker threads only touch their
own queue/cancel and the filesystem. _JOBS is likewise only mutated on the main
thread (the Generate operator and this timer), so no lock is needed.

This state is intentionally runtime-only — in-flight jobs (and their threads)
do not survive an add-on reload.
"""
import queue
import threading
import time

import bpy

from . import history
from . import net
from . import properties as props
from . import utils

# Most generations a user may have in flight at once. Bounds burst credit spend
# and server load; the panel disables Generate past this.
MAX_CONCURRENT = 8

# App-timer interval. Small enough that the tray animation looks smooth and
# status updates feel live, large enough not to burn the main thread.
TICK = 0.15

# How long a finished job lingers in the tray (with a ✓) before it drops out —
# by then it is already in History and loaded in the preview window.
DONE_LINGER = 5.0

# Job lifecycle states.
RUNNING = "running"
DONE = "done"
FAILED = "failed"

_JOBS = []           # list[Job], newest first; main-thread only
_timer_running = False
_seq = 0             # monotonic job id source


class Job:
    """One in-flight (or just-finished) generation."""

    def __init__(self, job_id, input_path, prompt, model_label, out_dir, is_video=False):
        self.id = job_id
        self.input_path = input_path        # per-job capture copy; tray thumbnail + upload
        self.prompt = prompt
        self.model_label = model_label
        self.out_dir = out_dir
        self.is_video = is_video            # animation job → result is an .mp4
        self.status = props.STATUS_UPLOADING
        self.message = ""
        self.state = RUNNING
        self.result_path = ""
        self.started_at = time.time()
        self.done_at = 0.0
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.thread = None


def active_count():
    return sum(1 for j in _JOBS if j.state == RUNNING)


def can_start():
    return active_count() < MAX_CONCURRENT


def tray():
    """Jobs to show in the panel (newest first): running + lingering done/failed."""
    return list(_JOBS)


def start_job(api_base, token, input_path, prompt, model_label, render_request,
              out_dir, reference_paths=None, extra_images=None, result_ext="png",
              is_video=False):
    """Spawn a worker thread for one generation and start tracking it.

    extra_images / result_ext are forwarded to net.run_render_job; is_video
    marks the job so the tray and finish handler treat the result as a video.
    """
    global _seq
    _seq += 1
    job = Job(_seq, input_path, prompt, model_label, out_dir, is_video=is_video)
    job.thread = threading.Thread(
        target=net.run_render_job,
        args=(api_base, token, input_path, prompt, render_request, out_dir,
              job.events, job.cancel, reference_paths),
        kwargs={"extra_images": extra_images, "result_ext": result_ext},
        daemon=True,
    )
    _JOBS.insert(0, job)
    job.thread.start()
    _ensure_timer()
    return job


def _find(job_id):
    for j in _JOBS:
        if j.id == job_id:
            return j
    return None


def cancel_job(job_id):
    """Cancel a running job and remove it from the tray.

    A render already queued server-side may still finish and appear in the
    user's blmn.ai library — same caveat as the old Esc-to-cancel.
    """
    job = _find(job_id)
    if job is None:
        return
    job.cancel.set()
    try:
        _JOBS.remove(job)
    except ValueError:
        pass
    utils.tag_redraw_all()


def dismiss_job(job_id):
    """Remove a finished/failed job from the tray (user acknowledgement)."""
    job = _find(job_id)
    if job is None:
        return
    try:
        _JOBS.remove(job)
    except ValueError:
        pass
    utils.tag_redraw_all()


def _ensure_timer():
    global _timer_running
    if _timer_running:
        return
    _timer_running = True
    bpy.app.timers.register(_drain)


def _scene_props():
    scene = getattr(bpy.context, "scene", None)
    return getattr(scene, "blmn_ai", None) if scene else None


def _on_finished(job):
    """A job succeeded: record history, update the preview window."""
    kind = "video" if job.is_video else "image"
    history.add(job.out_dir, job.prompt, job.model_label, job.input_path,
                job.result_path, kind=kind)

    sp = _scene_props()

    if job.is_video:
        # Blender's Image Editor can't play video, so just record the path —
        # the panel/tray offer an "Open Folder" action to reveal the .mp4.
        if sp is not None:
            sp.last_video_path = job.result_path
            sp.status = props.STATUS_FINISHED
        return

    if sp is not None:
        sp.last_result_path = job.result_path
        sp.status = props.STATUS_FINISHED

    img = utils.load_image(job.result_path, name="blmn_result")
    if img is not None:
        utils.open_in_image_editor(img)


def _drain():
    """Main-thread tick: apply worker events, expire finished jobs, repaint.

    Returns the next interval (keep ticking) or None (stop the timer).
    """
    global _timer_running
    now = time.time()

    for job in list(_JOBS):
        if job.state != RUNNING:
            continue
        try:
            while True:
                kind, data = job.events.get_nowait()
                if kind == "status":
                    job.status = data
                elif kind == "error":
                    job.state = FAILED
                    job.status = props.STATUS_FAILED
                    job.message = data
                    job.done_at = now
                    sp = _scene_props()
                    if sp is not None:
                        sp.status = props.STATUS_FAILED
                        sp.status_message = data
                elif kind == "finished":
                    job.state = DONE
                    job.status = props.STATUS_FINISHED
                    job.result_path = data.get("result_path", "")
                    job.done_at = now
                    _on_finished(job)
        except queue.Empty:
            pass

    # Finished jobs drop out after a short linger; failures stay until dismissed.
    for job in list(_JOBS):
        if job.state == DONE and (now - job.done_at) > DONE_LINGER:
            try:
                _JOBS.remove(job)
            except ValueError:
                pass

    utils.tag_redraw_all()

    # Keep ticking while work is animating (running) or a ✓ still needs to
    # expire (done). Failed jobs are static — they sit until dismissed, and the
    # dismiss handler repaints — so they must NOT hold the timer open forever.
    needs_tick = any(j.state in (RUNNING, DONE) for j in _JOBS)
    if needs_tick:
        return TICK
    _timer_running = False
    return None


def shutdown():
    """Stop all jobs and the timer (called on add-on unregister)."""
    global _timer_running
    for job in _JOBS:
        job.cancel.set()
    _JOBS.clear()
    if _timer_running:
        try:
            bpy.app.timers.unregister(_drain)
        except Exception:  # noqa: BLE001 - timer may already be unregistered
            pass
        _timer_running = False
