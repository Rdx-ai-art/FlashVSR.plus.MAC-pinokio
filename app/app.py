# FlashVSR+ Video Upscaler — web backend
#
# Drives the vpipe engine CLI as a subprocess to run FlashVSR+ video
# super-resolution on Apple Silicon. The UI (static/) talks to this app over
# REST; long-running jobs (model fetch, video upscale) execute in background
# threads with live progress parsed from vpipe's stdout.
#
# Layout (all relative to this file):
#   vpipe/vpipe — engine CLI (committed to the repo; FFmpeg dylibs sit beside it)
#   work/                             — vpipe CWD: models/, data.mdb, pipelines
#   uploads/<job>/input.<ext>         — incoming video
#   outputs/<job>/output.mp4          — finished upscaling

import argparse
import json
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import traceback
import uuid
from collections import deque
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
VPIPE_BIN = APP_DIR / "vpipe" / "vpipe"
WORK_DIR = APP_DIR / "work"
MODELS_DIR = WORK_DIR / "models" / "Dirdx" / "FlashVSR_plus"
UPLOAD_DIR = APP_DIR / "uploads"
OUTPUT_DIR = APP_DIR / "outputs"
STATIC_DIR = APP_DIR / "static"

# Model variants. `files` are checked under MODELS_DIR to decide if a variant
# is fully downloaded. Shared files (VAE, decoder, prompt) count for both.
VARIANTS = {
    "turbo": {
        "name": "FlashVSR+ Turbo",
        "desc": "Faster inference — hybrid attention. Recommended for speed.",
        "files": [
            "config.json",
            "turbo/dit.safetensors",
            "turbo/lq_proj_in.safetensors",
            "TCDecoder.ckpt",
            "Wan2.1_VAE.pth",
            "posi_prompt.pth",
        ],
    },
    "base": {
        "name": "FlashVSR+ Base",
        "desc": "Higher reconstruction quality — sparse attention.",
        "files": [
            "config.json",
            "base/dit.safetensors",
            "base/lq_proj_in.safetensors",
            "TCDecoder.ckpt",
            "Wan2.1_VAE.pth",
            "posi_prompt.pth",
        ],
    },
}
SHARED_FILES = [
    "config.json",
    "TCDecoder.ckpt",
    "Wan2.1_VAE.pth",
    "posi_prompt.pth",
]

# ---------------------------------------------------------------------------
# vpipe output parsing
# ---------------------------------------------------------------------------

# Milestone line (piped mode):  [PROGRESS] 40% of 'denoise ...' completed at 00:52:14 (640/1600)
MILESTONE_RE = re.compile(
    r"^\[PROGRESS\]\s+(\d+)%\s+of\s+'([^']*)'\s+completed\s+at\s+[\d:]+\s+\((\d+)/(\d+)\)"
)
# Terminating line:  [PROGRESS] 'denoise ...' ended at 00:52:14, last reported 88% (640/1600)
ENDED_RE = re.compile(
    r"^\[PROGRESS\]\s+'([^']*)'\s+ended\s+at\s+[\d:]+(?:,\s+last\s+reported\s+\d+%\s+\(\d+/\d+\))?"
)
# TTY footer line (defensive — we pipe, but keep it):  [####----] 34% desc
FOOTER_RE = re.compile(r"^\[([#\-]{4,})\]\s+(\d+)%\s+(.*)$")
TAG_RE = re.compile(r"^\[(INFO|WARN|ERROR|PROGRESS)\]\s?(.*)$")


def strip_ansi(s: str) -> str:
    s = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", s)
    s = re.sub(r"\x1b\][^\x07\x1b]*(\x07|\x1b\\)", "", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s


def ffprobe_video(path: Path) -> dict:
    """Best-effort metadata via PyAV (bundles its own FFmpeg libs); {} on failure."""
    try:
        import av
        with av.open(str(path)) as c:
            vs = c.streams.video[0]
            fps = None
            try:
                if vs.average_rate and vs.average_rate > 0:
                    fps = float(vs.average_rate)
            except Exception:
                pass
            dur = None
            try:
                if c.duration:
                    dur = float(c.duration) / 1_000_000
            except Exception:
                pass
            frames = vs.frames or (int(dur * (fps or 24.0)) if dur else None)
            # Fallback: derive fps from frame count / duration when the stream
            # doesn't report a usable average_rate (some encoders / VFR sources).
            if not fps and frames and dur:
                fps = frames / dur
            if not fps:
                fps = 24.0
            return {
                "width": vs.codec_context.width,
                "height": vs.codec_context.height,
                "fps": round(fps, 3),
                "frames": frames,
                "duration": round(dur, 3) if dur else None,
            }
    except Exception:
        return {}


def has_audio(path: Path) -> bool:
    try:
        import av
        with av.open(str(path)) as c:
            return len(c.streams.audio) > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Pipeline construction
# ---------------------------------------------------------------------------

def _snap_to_grid_aspect(w: float, h: float) -> tuple:
    """Snap an aspect-preserving (w, h) target to the vpipe 128-px grid.

    FlashVSR tiles on a 128-px grid; off-grid sizes get padded, which
    misaligns the src-encoder conditioning with generate-video and yields a
    static first frame. Rounding each axis independently (ceil to 128) drifts
    the output away from the source ratio and stretches the picture. Instead
    we search a small band of on-grid heights around the ideal and derive each
    width from the aspect, so W/H tracks the source. Aspect error dominates
    the score; area is a soft tiebreaker so the result stays near the requested
    size. Always returns (w, h) that are multiples of 128.
    """
    ratio = w / h
    ideal_n = math.sqrt((w * h) / ratio) / 128  # height in 128-units
    ideal_units = (w * h) / (128 * 128)
    best = (128, 128)
    best_score = float("inf")
    lo = max(1, round(ideal_n) - 4)
    hi = round(ideal_n) + 4
    for n in range(lo, hi + 1):
        m = max(1, round(n * ratio))  # width from the aspect
        aspect_err = abs(m / n - ratio) / ratio
        area_err = abs(m * n - ideal_units) / ideal_units
        score = aspect_err + 0.15 * area_err
        if score < best_score:
            best_score = score
            best = (m * 128, n * 128)
    return best


def build_pipeline(job: "Job", params: dict) -> dict:
    """Build the vpipe pipeline JSON for one upscale job."""
    variant = params["variant"]
    out_W = int(params["width"])
    out_H = int(params["height"])
    # Ensure final output dimensions are even for MP4/YUV420p encoding
    out_W = max(64, (out_W // 2) * 2)
    out_H = max(64, (out_H // 2) * 2)

    # FlashVSR's VAE + DiT 2x2 patch + window attention tile on a 128-px grid
    # (e.g. 1920x1088 gets rounded up by generate-video to 1920x1152).
    # Both upscale-in and generate-video MUST use the exact same 128-aligned
    # dimensions so flashvsr-src-encoder conditioning token counts match
    # generate-video's expected tokens per frame.
    tile_W = max(128, ((out_W + 127) // 128) * 128)
    tile_H = max(128, ((out_H + 127) // 128) * 128)

    fps = float(params.get("fps") or 24.0)
    frames = int(params.get("frames") or 49)
    frames = max(1, frames)
    seed = int(params.get("seed") if params.get("seed") is not None else 42)
    # The DiT's native temporal window is 24 RGB frames (FlashVSR+ — valtz's
    # catalog uses group:24 for both turbo and base). The temporal-stack MUST
    # emit groups of this size, NOT the whole clip: feeding every frame as one
    # group exceeds the window and the model degenerates to a static first
    # frame. With groups of 24 and continuity=auto (-> stream), the
    # src-encoder carries KV/decode state across groups, so a 72-frame clip
    # runs as three 24-frame passes with no seam and real motion.
    group_size = 24
    # Map the UI's color-fix choice ("wavelet" / "adaIn" / "none") to the
    # src-encoder config. The src-encoder takes a `color_fix` BOOL (on/off)
    # plus a `color_fix_method` string (auto|adain|wavelet). "none" must set
    # color_fix=false — passing color_fix_method="none" (or "adaIn" with the
    # wrong case) is an invalid method, so the stage refuses and the whole
    # pipeline dies with no video in a few seconds.
    _cf = (params.get("color_fix") or "wavelet").strip().lower()
    color_fix_on = _cf not in ("none", "off", "false", "")
    color_fix_method = "adain" if _cf in ("adain", "ada_in", "ada-in") else "wavelet"
    # "tiny" = the TAEHV decoder (fast, low-mem); "vae" = the full Wan2.1 VAE
    # (heavier, different Metal kernels). The tiny decoder's command buffer
    # faults on some GPUs (kIOGPU…ImpactingInteractivity) → static output; the
    # Full VAE is the workaround/diagnostic.
    decoder = (params.get("decoder") or "tiny").strip().lower()
    if decoder not in ("tiny", "vae", "auto"):
        decoder = "tiny"
    temporal_comp = int(params.get("temporal_compression") or 4)
    keep_audio = bool(params.get("keep_audio") and job.src_has_audio)

    rel_in = f"../uploads/{job.id}/input{job.in_ext}"
    rel_out = f"out_{job.id}.mp4"

    stages = [
        {
            "id": "model-select",
            "type": "model-select",
            "iports": [],
            "config": {
                "hf_dir": f"Dirdx/FlashVSR_plus-{variant}",
            },
        },
        {
            "id": "load-video",
            "type": "load-video",
            "iports": [],
            "config": {"input_url": rel_in, "enable_audio": False},
        },
        {
            "id": "video-to-rgb",
            "type": "video-to-rgb",
            "iports": [{"src": "load-video", "oport": 0}],
            "config": {"output_dtype": "u8", "hwaccel": "auto", "oport_capacity": 2},
        },
        {
            "id": "upscale-in",
            "type": "image-resample",
            "iports": [{"src": "video-to-rgb", "oport": 0}],
            # `crop` (fill + centre-crop) maps the source onto the tile WITHOUT
            # changing its aspect ratio. The old `stretch` forced the source's
            # ratio into the tile's ratio, visibly stretching the picture
            # whenever the 128-grid tile ratio != the source ratio (e.g. a
            # 768x448 source stretched to 1280x768). Now the small residual
            # ratio gap becomes a tiny centre-crop instead of distortion.
            "config": {"width": tile_W, "height": tile_H, "fit": "crop", "algorithm": "bicubic"},
        },
        {
            "id": "temporal-stack",
            "type": "temporal-stack",
            "iports": [{"src": "upscale-in", "oport": 0}],
            "config": {"mode": "video", "group_size": group_size, "overlap": 0, "max_mb": 512},
        },
        {
            "id": "flashvsr-src-encoder",
            "type": "flashvsr-src-encoder",
            "iports": [
                {"src": "temporal-stack", "oport": 0},
                {"src": "model-select", "oport": 0},
            ],
            "config": {
                "decoder": decoder,
                "color_fix": color_fix_on,
                "color_fix_method": color_fix_method,
                "temporal_compression": temporal_comp,
            },
        },
        {
            "id": "generate-video",
            "type": "generate-video",
            "iports": [
                {"src": "flashvsr-src-encoder", "oport": 0},
                {"src": "", "oport": 0},
                {"src": "model-select", "oport": 0},
            ],
            "config": {
                "width": tile_W,
                "height": tile_H,
                "frames": frames,
                "seed": seed,
                "unload_when_idle": "auto",
            },
        },
        {
            "id": "vae-decode",
            "type": "vae-decode",
            "iports": [
                {"src": "generate-video", "oport": 0},
                {"src": "model-select", "oport": 0},
                {"src": "flashvsr-src-encoder", "oport": 1},
            ],
            "config": {},
        },
        {
            "id": "squeeze-out",
            "type": "image-resample",
            "iports": [{"src": "vae-decode", "oport": 0}],
            "config": {"width": out_W, "height": out_H, "fit": "crop", "algorithm": "lanczos"},
        },
        {
            "id": "rgb-to-video",
            "type": "rgb-to-video",
            "iports": [{"src": "squeeze-out", "oport": 0}],
            # Two colour fixes vs the stage defaults, both needed for accurate
            # colour (matches Valtz, which uses the same vpipe engine):
            #  - colorspace: True  -> BT.709. The default BT.601 chroma matrix
            #    shifts/desaturates colour on the YUV round-trip.
            #  - color_range: True -> full range. The default *limited* range maps
            #    the model's 0-255 RGB into 16-235, compressing contrast to
            #    219/255 (~14%) which reads as "washed out / low saturation".
            #    Full range preserves the model's full contrast.
            "config": {"fps": fps, "colorspace": True, "color_range": True},
        },
    ]

    if keep_audio:
        stages.append(
            {
                "id": "load-audio",
                "type": "load-audio",
                "iports": [],
                "config": {"input_url": rel_in},
            }
        )
        stages.append(
            {
                "id": "audio-to-pcm",
                "type": "audio-to-pcm",
                "iports": [{"src": "load-audio", "oport": 0}],
                "config": {
                    "output_sample_rate": 32000,
                    "channels": 2,
                    "chunk_duration_s": 10.0,
                    "max_chunk_duration_s": 30.0,
                },
            }
        )

    save_iports = [{"src": "rgb-to-video", "oport": 0}]
    if keep_audio:
        save_iports.append({"src": "audio-to-pcm", "oport": 0})

    stages.append(
        {
            "id": "save-video",
            "type": "save-video",
            "iports": save_iports,
            "config": {
                "output_url": rel_out,
                "enable_video": True,
                "enable_audio": keep_audio,
                "video_codec": "libx264",
                "video_crf": 18,
                "video_bitrate": 6400000,
                "video_gop_size": 48,
                "video_preset": "medium",
                "audio_bitrate": 192000,
                "muxer_options": {"movflags": "+faststart"},
            },
        }
    )

    return {
        "id": f"flashvsr-{job.id}",
        "stages": stages,
        "subpipelines": [],
    }


# ---------------------------------------------------------------------------
# Job registry + runners
# ---------------------------------------------------------------------------

class Job:
    def __init__(self, kind: str, id: str):
        self.id = id
        self.kind = kind  # "fetch" | "upscale"
        self.state = "queued"  # queued|running|done|failed|cancelled
        self.phase = "starting"
        self.pct = 0.0
        self.desc = ""
        self.detail = ""
        self.error = ""
        self.log = deque(maxlen=400)
        self.created = time.time()
        self.finished = None
        # upscale-only
        self.filename = ""
        self.params = {}
        self.meta = {}
        self.output_path: Path | None = None
        self.proc: subprocess.Popen | None = None
        self.mu = threading.Lock()

    def snapshot(self) -> dict:
        with self.mu:
            return {
                "id": self.id,
                "kind": self.kind,
                "state": self.state,
                "phase": self.phase,
                "pct": round(self.pct, 1),
                "desc": self.desc,
                "detail": self.detail,
                "error": self.error,
                "log": list(self.log)[-120:],
                "created": self.created,
                "finished": self.finished,
                "filename": self.filename,
                "params": self.params,
                "meta": self.meta,
                "output": str(self.output_path) if self.output_path else None,
            }


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def add_job(kind: str, id: str) -> Job:
    job = Job(kind, id)
    with JOBS_LOCK:
        JOBS[id] = job
    return job


def set_state(job: Job, state: str, phase: str = "", error: str = ""):
    with job.mu:
        job.state = state
        if phase:
            job.phase = phase
        if error:
            job.error = error
        if state in ("done", "failed", "cancelled"):
            job.finished = time.time()


def push_log(job: Job, line: str):
    with job.mu:
        job.log.append(line)
    # Mirror every job line to the Pinokio terminal too, so progress and
    # errors are visible there even when the UI can't reach the server.
    print(f"[job {job.kind}/{job.id[:8]}] {line}", flush=True)


def human_duration(seconds: float) -> str:
    """Compact wall-clock string for metadata: 41s / 4m 05s / 1h 02m 03s."""
    s = int(round(max(0.0, seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def run_vpipe(job: Job, args: list[str]):
    """Run the vpipe CLI with piped stdout; parse milestones into job state."""
    job.proc = subprocess.Popen(
        [str(VPIPE_BIN)] + args,
        cwd=str(WORK_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        # vpipe gets its OWN process group/session. Without this it shares
        # the server's group, and the cancel path's os.killpg(SIGINT) would
        # kill the uvicorn server itself (silent death, no traceback).
        start_new_session=True,
    )
    proc = job.proc
    denoise_done = 0
    denoise_total_est = job.meta.get("est_clips") or 0
    cur_pct = 0.0
    cur_total = 0
    gpu_failures = 0
    for raw in proc.stdout:
        line = strip_ansi(raw).rstrip()
        if not line:
            continue
        # Metal "Impacting Interactivity" / command-buffer failures mean the GPU
        # driver evicted our workload (memory or interactivity pressure). vpipe
        # logs these as [WARN] and "skips" the frame, then keeps going — churning
        # through generations that all fail, so the progress bar stalls and the
        # job never completes. Abort after a couple so the user gets a real
        # failure instead of an endless stuck run.
        if "Impacting Interactivity" in line or "GPU command buffer error" in line:
            gpu_failures += 1
            push_log(job, line)
            if gpu_failures >= 2:
                with job.mu:
                    job.error = (
                        "GPU command-buffer failure (Metal 'Impacting "
                        "Interactivity'): the GPU driver evicted the workload, "
                        "usually under memory pressure. Lower the output "
                        "resolution, close other GPU apps, or use the Tiny "
                        "decoder, then retry."
                    )
                push_log(job, "Aborting: repeated GPU command-buffer failures.")
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                break
            continue
        m = MILESTONE_RE.match(line)
        if m:
            pct = int(m.group(1))
            desc = m.group(2)
            done, total = int(m.group(3)), int(m.group(4))
            push_log(job, line)
            with job.mu:
                job.desc = desc
                if desc.startswith("denoise"):
                    if pct >= 100:
                        denoise_done += 1
                        pct_now = min(99.0, 5.0 + 85.0 * denoise_done / max(1, denoise_total_est)) if denoise_total_est else min(90.0, 5.0 + 8.0 * denoise_done)
                        job.pct = pct_now
                        job.phase = f"denoise {denoise_done}/{denoise_total_est or '?'} clips"
                    else:
                        job.pct = 5.0 + 85.0 * (denoise_done + pct / 100.0) / max(1, denoise_total_est) if denoise_total_est else min(90.0, 5.0 + 8.0 * denoise_done + pct / 10)
                        job.phase = "denoise"
                elif job.kind == "fetch":
                    # Repo-level + per-file reports: the repo report is the
                    # last to reach 100%, so max-across-reports is the honest
                    # overall percentage.
                    job.pct = min(100.0, max(job.pct, pct))
                    job.phase = "fetching model"
                else:
                    cur_pct = pct
                    cur_total = total
                    job.phase = desc
                    if total:
                        # Map the active report onto the post-denoise band.
                        base = 95.0 if denoise_done else (5.0 + 85.0 * denoise_done / max(1, denoise_total_est)) if denoise_total_est else 5.0
                        job.pct = min(98.0, base + pct / 10.0)
                job.detail = f"{done}/{total}" if total else ""
            continue
        m = ENDED_RE.match(line)
        if m:
            push_log(job, line)
            continue
        m = FOOTER_RE.match(line)
        if m:
            with job.mu:
                job.pct = max(job.pct, float(m.group(2)))
                job.phase = m.group(3).strip()
            continue
        m = TAG_RE.match(line)
        if m:
            push_log(job, line)
            if m.group(1) == "ERROR":
                job.error = m.group(2)
        else:
            push_log(job, line)
    # Wait for the process to exit. Normally stdout has already closed (process
    # done) so this returns at once; if we broke out early on a GPU abort the
    # process is still alive and being signalled, so bound the wait and
    # escalate to SIGKILL if it ignores SIGTERM.
    try:
        rc = proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        rc = proc.wait()
    return rc


def fetch_worker(job: Job, variant: str):
    set_state(job, "running", "fetching model")
    push_log(job, f"Fetching {VARIANTS[variant]['name']} from Dirdx/FlashVSR_plus ...")
    try:
        # NB: vpipe's CLI takes `--stage-cfg KEY=VALUE` as two separate
        # argv entries — `--stage-cfg=KEY=VALUE` is rejected (exit 2).
        #
        # Dirdx/FlashVSR_plus is the complete repo (the original
        # ruchangyao/FlashVSR_plus is missing posi_prompt.pth, which vpipe
        # requires as the context file). Dirdx is not in vpipe's built-in
        # model catalogue, so model-fetch downloads the whole repo (no
        # per-variant handling) and registers it under the bare hf path.
        # base_path is set explicitly so model-fetch never blocks on its
        # interactive "Base download path" prompt.
        if not variant_ready(variant):
            rc = run_vpipe(job, [
                "--launch-stage", "model-fetch",
                "--stage-cfg", "model_path=Dirdx/FlashVSR_plus",
                "--stage-cfg", f"base_path={WORK_DIR / 'models'}",
                "--stage-cfg", "skip_existing_files=true",
            ])
            if rc != 0 and job.state != "cancelling":
                set_state(job, "failed", "fetch failed", error=f"vpipe exited with code {rc}")
                return
        # Register the selected variant's subdir under its key (no network).
        # vpipe resolves the context/VAE/decoder from the repo root (the
        # subdir's parent), so pointing at `<repo>/<variant>/` is enough.
        rc = run_vpipe(job, [
            "--launch-stage", "model-register",
            "--stage-cfg", f"model_dir={MODELS_DIR / variant}",
            "--stage-cfg", f"key=Dirdx/FlashVSR_plus-{variant}",
            "--stage-cfg", "model_type=flashvsr",
        ])
        if rc == 0 and variant_ready(variant):
            set_state(job, "done", "ready")
            with job.mu:
                job.pct = 100.0
            push_log(job, f"{VARIANTS[variant]['name']} ready.")
        elif job.state == "cancelling":
            set_state(job, "cancelled", "cancelled")
        else:
            set_state(job, "failed", "register failed", error=f"vpipe exited with code {rc}")
    except Exception as e:
        traceback.print_exc()
        if job.state != "cancelling":
            set_state(job, "failed", "fetch failed", error=str(e))


FFPROBE_BIN = shutil.which("ffprobe") or "ffprobe"
FFMPEG_BIN = shutil.which("ffmpeg") or "ffmpeg"


def _stream_duration(path: Path, kind: str):
    """Duration (seconds) of the first video ('v') or audio ('a') stream, or None."""
    sel = "v:0" if kind == "v" else "a:0"
    try:
        out = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-select_streams", sel,
             "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1",
             str(path)],
            capture_output=True, text=True, timeout=30)
        val = out.stdout.strip().splitlines()[0].strip()
        return float(val) if val and val != "N/A" else None
    except Exception:
        return None


def sync_av(job: Job, path: Path):
    """Keep the output's picture and sound the same length.

    vpipe can drop a handful of video frames during the temporal-compressed
    VAE decode ("...not whole runs of 2 to merge; the last 1 are dropped"),
    which varies with the frame count / group alignment. That leaves the
    picture shorter than the carried-over audio, so the sound keeps playing
    after the video ends. Trim the longer stream down to the shorter one
    (the picture is the content, so the audio is what gets cut when it's
    longer). No-op when they already match within a few frames.
    """
    try:
        v = _stream_duration(path, "v")
        a = _stream_duration(path, "a")
        if v is None or a is None:
            return
        if abs(v - a) <= 0.15:
            return  # in sync within ~4 frames at 24fps
        target = min(v, a)
        fixed = path.with_name(path.name + ".sync.mp4")
        cmd = [FFMPEG_BIN, "-y", "-i", str(path), "-t", f"{target:.3f}",
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(fixed)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode == 0 and fixed.exists() and fixed.stat().st_size > 0:
            shutil.move(str(fixed), str(path))
            push_log(job, f"Matched A/V length: trimmed to {target:.2f}s "
                         f"(was video {v:.2f}s / audio {a:.2f}s).")
        else:
            fixed.unlink(missing_ok=True)
            push_log(job, f"A/V trim failed (video {v:.2f}s / audio {a:.2f}s); "
                         "kept the original file.")
    except Exception as e:
        push_log(job, f"A/V sync check skipped: {e}")


def upscale_worker(job: Job):
    try:
        set_state(job, "running", "preparing")
        params = job.params
        W = OUTPUT_DIR / job.id
        W.mkdir(parents=True, exist_ok=True)

        pipe_path = WORK_DIR / f"pipeline_{job.id}.json"
        pipe_path.write_text(json.dumps(build_pipeline(job, params), indent=2))

        # Guarantee the selected variant is in vpipe's LMDB registry before
        # launch. model-fetch downloads the WHOLE repo (turbo/ AND base/ both
        # land on disk) but only registers the variant that was fetched, and
        # variant_ready() only checks the filesystem — so the other variant, a
        # manually-copied model, or a fresh data.mdb can look "ready" in the UI
        # yet be absent from the registry, and vpipe (no filesystem fallback)
        # would fail to load it. model-register is local-only and idempotent,
        # so it's cheap to run on every upscale.
        variant = params["variant"]
        reg_rc = run_vpipe(job, [
            "--launch-stage", "model-register",
            "--stage-cfg", f"model_dir={MODELS_DIR / variant}",
            "--stage-cfg", f"key=Dirdx/FlashVSR_plus-{variant}",
            "--stage-cfg", "model_type=flashvsr",
        ])
        if reg_rc != 0 and job.state != "cancelling":
            set_state(job, "failed", "register failed",
                      error=f"model-register exited with code {reg_rc}")
            return

        push_log(job, f"Launching vpipe (variant={params['variant']}, {params['width']}x{params['height']}) ...")
        rc = run_vpipe(job, ["--launch", str(pipe_path.relative_to(WORK_DIR))])

        out_tmp = WORK_DIR / f"out_{job.id}.mp4"
        if rc == 0 and out_tmp.exists() and out_tmp.stat().st_size > 0:
            final = W / "output.mp4"
            shutil.move(str(out_tmp), str(final))
            job.output_path = final
            # vpipe sometimes drops a few video frames (see sync_av); trim the
            # carried-over audio to the picture so they stay in sync.
            sync_av(job, final)
            gen_s = round(time.time() - job.created, 1)
            (W / "meta.json").write_text(json.dumps({
                "created": time.time(),
                "generation_time_s": gen_s,
                "generation_time": human_duration(gen_s),
                "in_ext": job.in_ext,
                "filename": job.filename,
                "params": job.params,
                "src": job.meta,
            }, indent=2))
            set_state(job, "done", "complete")
            with job.mu:
                job.pct = 100.0
            push_log(job, f"Done → {final.name}")
        elif job.state == "cancelling":
            out_tmp.unlink(missing_ok=True)
            set_state(job, "cancelled", "cancelled")
        else:
            out_tmp.unlink(missing_ok=True)
            err = job.error or f"vpipe exited with code {rc}"
            set_state(job, "failed", "failed", error=err)
    except Exception as e:
        traceback.print_exc()
        if job.state != "cancelling":
            set_state(job, "failed", "failed", error=str(e))


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def variant_ready(variant: str) -> bool:
    return all((MODELS_DIR / f).exists() for f in VARIANTS[variant]["files"])


app = FastAPI(title="FlashVSR+ Video Upscaler")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    for d in (WORK_DIR, UPLOAD_DIR, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)


@app.get("/api/system")
def api_system():
    ram_gb = None
    try:
        ram = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip()
        if ram:
            ram_gb = round(int(ram) / (1024 ** 3), 1)
    except Exception:
        pass
    chip = None
    try:
        chip = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True).stdout.strip() or None
    except Exception:
        pass
    return {
        "ok": True,
        "ram_gb": ram_gb,
        "chip": chip,
        "vpipe": {
            "found": VPIPE_BIN.exists(),
            "path": str(VPIPE_BIN),
        },
    }


@app.get("/api/models")
def api_models():
    out = []
    for vid, v in VARIANTS.items():
        files = []
        for f in v["files"]:
            p = MODELS_DIR / f
            files.append({
                "name": f,
                "exists": p.exists(),
                "size": p.stat().st_size if p.exists() else 0,
            })
        job = next((j for j in _jobs_of_kind("fetch") if j.params.get("variant") == vid
                    and j.state in ("running", "queued")), None)
        out.append({
            "id": vid,
            "name": v["name"],
            "desc": v["desc"],
            "ready": variant_ready(vid),
            "files": files,
            "fetching": bool(job),
            "fetch_job": job.id if job else None,
        })
    return {"ok": True, "models": out}


def _jobs_of_kind(kind: str) -> list[Job]:
    with JOBS_LOCK:
        return [j for j in JOBS.values() if j.kind == kind]


@app.post("/api/models/fetch")
def api_models_fetch(body: dict):
    variant = (body or {}).get("variant")
    if variant not in VARIANTS:
        raise HTTPException(400, f"unknown variant: {variant!r} (expected one of: {', '.join(VARIANTS)})")
    if variant_ready(variant):
        raise HTTPException(409, "already downloaded")
    existing = next((j for j in _jobs_of_kind("fetch")
                     if j.state in ("running", "queued") and j.params.get("variant") == variant), None)
    if existing:
        raise HTTPException(409, "fetch already in progress")
    job = add_job("fetch", uuid.uuid4().hex[:12])
    job.params = {"variant": variant}
    threading.Thread(target=fetch_worker, args=(job, variant), daemon=True).start()
    return {"ok": True, "job": job.id}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return {"ok": True, "job": job.snapshot()}


@app.get("/api/jobs")
def api_jobs():
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: j.created, reverse=True)
    return {"ok": True, "jobs": [j.snapshot() for j in jobs]}


@app.post("/api/jobs/{job_id}/cancel")
def api_job_cancel(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    if job.state not in ("running", "queued"):
        raise HTTPException(409, f"job is {job.state}")
    proc = job.proc
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
    with job.mu:
        job.state = "cancelling"
        job.phase = "cancelling"
    def _finalize():
        time.sleep(2.0)
        if job.proc and job.proc.poll() is None:
            try:
                os.killpg(os.getpgid(job.proc.pid), signal.SIGTERM)
            except Exception:
                pass
            try:
                job.proc.wait(timeout=5)
            except Exception:
                job.proc.kill()
        set_state(job, "cancelled", "cancelled")
    threading.Thread(target=_finalize, daemon=True).start()
    return {"ok": True}


@app.post("/api/video/upscale")
async def api_video_upscale(file: UploadFile = File(...), params: str = Form("{}")):
    if not VPIPE_BIN.exists():
        raise HTTPException(503, "vpipe CLI not found — run Install first")
    try:
        p = json.loads(params) if params else {}
    except json.JSONDecodeError:
        raise HTTPException(400, "params is not valid JSON")
    variant = p.get("variant")
    if variant not in VARIANTS:
        raise HTTPException(400, f"unknown variant: {variant!r} (expected one of: {', '.join(VARIANTS)})")
    if not variant_ready(variant):
        raise HTTPException(409, f"model '{variant}' not downloaded yet")
    scale_mode = (p.get("scale_mode") or "factor").strip().lower()
    if scale_mode not in ("factor", "target"):
        raise HTTPException(400, "scale_mode must be 'factor' or 'target'")
    scale_factor = float(p.get("scale_factor") or 2.0)
    target_px = int(p.get("target_px") or 1080)
    if not (1.0 <= scale_factor <= 4.0):
        raise HTTPException(400, "scale_factor out of range (1..4)")
    if not (64 <= target_px <= 16384):
        raise HTTPException(400, "target_px out of range (64..16384)")

    job = add_job("upscale", uuid.uuid4().hex[:12])
    job.filename = Path(file.filename or "input.mp4").name
    job.params = {
        "variant": variant,
        # Output W/H are computed from the scale mode + probed source dims
        # (after ffprobe below) so the aspect is preserved by construction.
        "scale_mode": scale_mode,
        "scale_factor": scale_factor,
        "target_px": target_px,
        "width": None,
        "height": None,
        "fps": p.get("fps"),
        "frames": p.get("frames", 49),
        "seed": p.get("seed", 42),
        "group_size": p.get("group_size", 24),
        "color_fix": p.get("color_fix", "wavelet"),
        # decoder: "tiny" (TAEHV conditioned decoder) | "vae" (full Wan2.1 VAE)
        # | "auto". Must be forwarded here — build_pipeline() only sees
        # job.params, and dropping it silently forces the tiny decoder no
        # matter what the UI sends (the "Full VAE" toggle never took effect).
        "decoder": (p.get("decoder") or "tiny").strip().lower(),
        "temporal_compression": p.get("temporal_compression", 4),
        "keep_audio": bool(p.get("keep_audio", True)),
    }

    up_dir = UPLOAD_DIR / job.id
    up_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(job.filename).suffix.lower() or ".mp4"
    if ext not in (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"):
        ext = ".mp4"
    job.in_ext = ext
    dest = up_dir / f"input{ext}"
    with dest.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)

    meta = ffprobe_video(dest)
    job.src_has_audio = has_audio(dest)
    job.meta = meta
    # Output resolution from the scale mode + real source dims. Both sides
    # scale by the same factor, so the aspect is preserved exactly (no
    # stretch); we then snap to the 128-px grid while keeping the ratio.
    in_w = int(meta.get("width") or 0)
    in_h = int(meta.get("height") or 0)
    if in_w < 16 or in_h < 16:
        raise HTTPException(400, "could not read source dimensions")
    target_res = float(target_px) if scale_mode == "target" else min(in_w, in_h) * scale_factor
    scale = target_res / min(in_w, in_h)
    out_w, out_h = _snap_to_grid_aspect(in_w * scale, in_h * scale)
    job.params["width"] = out_w
    job.params["height"] = out_h
    frames = int(meta.get("frames") or 0)
    if frames > 0:
        # Drive the pipeline from the REAL frame count, not the frontend's
        # placeholder 49. A frames/group_size that doesn't match the source
        # misaligns the src-encoder conditioning with generate-video and the
        # model emits the first frame repeated (static output).
        job.params["frames"] = frames
        # One lead chunk of 17, then 16-frame cadence per subsequent chunk.
        job.meta["est_clips"] = max(1, 1 + max(0, (frames - 17) + 15) // 16)
    if not p.get("fps"):
        job.params["fps"] = meta.get("fps") or 24.0

    threading.Thread(target=upscale_worker, args=(job,), daemon=True).start()
    return {"ok": True, "job": job.id, "meta": {**meta, "out_width": out_w, "out_height": out_h}}


@app.get("/api/outputs")
def api_outputs():
    items = []
    if OUTPUT_DIR.exists():
        for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            out = d / "output.mp4"
            meta_f = d / "meta.json"
            meta = {}
            if meta_f.exists():
                try:
                    meta = json.loads(meta_f.read_text())
                except Exception:
                    pass
            if out.exists():
                in_name = "input" + (meta.get("in_ext") or ".mp4")
                items.append({
                    "id": d.name,
                    "output": out.name,
                    # The original upload lives in uploads/<job>/, not outputs/<job>/
                    "input": in_name if (UPLOAD_DIR / d.name / in_name).exists() else None,
                    "size": out.stat().st_size,
                    "created": meta.get("created") or out.stat().st_mtime,
                    "meta": meta,
                })
    # Combined size of the outputs + their source inputs (Library header).
    def _dir_size(p):
        total = 0
        if p.exists():
            for f in p.rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
        return total

    total_size = _dir_size(OUTPUT_DIR) + _dir_size(UPLOAD_DIR)
    return {"ok": True, "outputs": items, "total_size": total_size}


@app.post("/api/outputs/{job_id}/meta")
def api_output_meta(job_id: str, body: dict):
    d = OUTPUT_DIR / job_id
    if not d.exists():
        raise HTTPException(404, "no such output")
    (d / "meta.json").write_text(json.dumps(body, indent=2))
    return {"ok": True}


@app.post("/api/outputs/clear")
def api_outputs_clear():
    n = 0
    if OUTPUT_DIR.exists():
        for d in OUTPUT_DIR.iterdir():
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                n += 1
    return {"ok": True, "removed": n}


@app.post("/api/outputs/{job_id}/delete")
def api_output_delete(job_id: str):
    if "/" in job_id or job_id.startswith(".") or ".." in job_id:
        raise HTTPException(400, "bad id")
    out = OUTPUT_DIR / job_id
    if not out.exists():
        raise HTTPException(404, "no such output")
    # Remove the output folder AND its associated source upload so the
    # generation is fully reclaimed (matches what the Library shows as size).
    removed = 0
    for d in (out, UPLOAD_DIR / job_id):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return {"ok": True, "removed": removed}


@app.get("/api/download/{job_id}")
def api_download(job_id: str):
    out = OUTPUT_DIR / job_id / "output.mp4"
    if not out.exists():
        raise HTTPException(404, "not found")
    return FileResponse(str(out), media_type="video/mp4", filename=f"flashvsr_{job_id}.mp4")


@app.get("/api/outputs/{job_id}/{name}")
def api_output_file(job_id: str, name: str):
    if "/" in name or name.startswith("."):
        raise HTTPException(400, "bad name")
    p = OUTPUT_DIR / job_id / name
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "not found")
    mt = "video/mp4" if p.suffix in (".mp4", ".m4v") else ("video/quicktime" if p.suffix == ".mov" else "application/octet-stream")
    return FileResponse(str(p), media_type=mt, filename=p.name)


@app.get("/api/uploads/{job_id}/{name}")
def api_upload_file(job_id: str, name: str):
    if "/" in name or name.startswith("."):
        raise HTTPException(400, "bad name")
    p = UPLOAD_DIR / job_id / name
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "not found")
    mt = "video/mp4" if p.suffix in (".mp4", ".m4v") else ("video/quicktime" if p.suffix == ".mov" else "application/octet-stream")
    return FileResponse(str(p), media_type=mt, filename=p.name)


# Never let the browser serve a stale app.js / index.html / style.css. A
# Pinokio reset refreshes the files on disk but NOT the browser cache, so
# without this the UI keeps running an old app.js after code changes.
@app.middleware("http")
async def no_cache(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path in ("/", "") or path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# Surface request-level errors (bad variant, missing model, bad params) in the
# Pinokio terminal. These are raised BEFORE a job exists, so they never reach
# push_log — without this handler the user sees the UI toast but nothing in the
# terminal, which made failures look "silent".
@app.exception_handler(HTTPException)
async def http_exc_handler(request, exc):
    print(f"[http {request.method} {request.url.path}] {exc.status_code}: {exc.detail}", flush=True)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


def main():
    global WORK_DIR, UPLOAD_DIR, OUTPUT_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7860)
    ap.add_argument("--host", type=str, default="127.0.0.1")
    a = ap.parse_args()
    import uvicorn
    print(f" http://{a.host}:{a.port}/")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
