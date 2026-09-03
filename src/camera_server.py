import argparse
import dataclasses
import io
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime

import cv2
import h5py
import matplotlib
import matplotlib.ticker as mticker
import numpy as np
from flask import Flask, Response, jsonify, send_file
from matplotlib.figure import Figure

from ellipse_fitting import (
    Config,
    detect_ellipse,
    draw_overlay,
    find_candidate_contours,
)
from ellipse_fitting import binarize as _binarize

matplotlib.use("Agg")  # non-interactive backend, required for server-side rendering


def _setup_logger(log_file: str = "camera_server.log") -> logging.Logger:
    logger = logging.getLogger("camera_server")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(threadName)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)

    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(fmt)

    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)

    # Silence werkzeug's default handler so Flask logs go through ours only
    logging.getLogger("werkzeug").handlers.clear()
    logging.getLogger("werkzeug").addHandler(file_handler)
    logging.getLogger("werkzeug").addHandler(stream_handler)

    return logger


logger = _setup_logger()

app = Flask(__name__)

PREVIEW_QUALITY = 50
H264_CRF = 28
RECORD_FPS = 30

# Raw-frame backup: batch writes to amortize h5py resize/write call overhead
# instead of one HDF5 call per frame. ~1s worth of frames at RECORD_FPS=30, or
# after 1s of wall time (so a low/idle frame rate doesn't stall a flush).
RAW_FRAME_FLUSH_BATCH_SIZE = 30
RAW_FRAME_FLUSH_INTERVAL_S = 1.0
RAW_FRAME_CLOSE_TIMEOUT_S = 10.0


def _parse_args():
    p = argparse.ArgumentParser(
        description="Microscope camera server with ellipse tracking"
    )
    p.add_argument("--config", metavar="FILE", help="JSON calibration config file")
    p.add_argument("--camera-index", type=int)
    p.add_argument("--pixels-per-mm", type=float)
    p.add_argument("--blur-kernel", type=int)
    p.add_argument("--threshold", type=int)
    p.add_argument("--min-contour-area", type=float)
    p.add_argument("--max-contour-area", type=float)
    p.add_argument(
        "--morph-close-size",
        type=int,
        help="Closing kernel size (px) to fill specular reflection holes; 0=off",
    )
    p.add_argument(
        "--roi",
        type=int,
        nargs=4,
        metavar=("X", "Y", "W", "H"),
        help="Detection region in pixels: x y width height",
    )
    p.add_argument(
        "--use-gradient",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Threshold gradient magnitude instead of absolute intensity "
        "(re-tune --threshold after switching; see docs/SETUP.md)",
    )
    p.add_argument(
        "--raw-frame-dir",
        metavar="DIR",
        help="Local scratch directory for raw-frame HDF5 backups; requires --roi",
    )
    p.add_argument(
        "--raw-frame-min-free-mb",
        type=float,
        help="Minimum free space (MB) in --raw-frame-dir required to enable raw-frame saving",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Enable /debug_frame endpoint (shows binary threshold image)",
    )
    return p.parse_args()


def _build_config(args) -> Config:
    cfg = Config()
    if args.config:
        with open(args.config) as f:
            raw = json.load(f)
        field_names = {field.name for field in dataclasses.fields(cfg)}
        for key, val in raw.items():
            if key in field_names:
                setattr(cfg, key, val)
    cli_overrides = {
        "camera_index": args.camera_index,
        "pixels_per_mm": args.pixels_per_mm,
        "blur_kernel": args.blur_kernel,
        "threshold": args.threshold,
        "min_contour_area": args.min_contour_area,
        "max_contour_area": args.max_contour_area,
        "morph_close_size": args.morph_close_size,
        "roi": args.roi,
        "use_gradient": args.use_gradient,
        "raw_frame_dir": args.raw_frame_dir,
        "raw_frame_min_free_mb": args.raw_frame_min_free_mb,
    }
    for attr, val in cli_overrides.items():
        if val is not None:
            setattr(cfg, attr, val)
    return cfg


_args = _parse_args()
config = _build_config(_args)
DEBUG_MODE: bool = _args.debug

cap = cv2.VideoCapture(config.camera_index)
frame_lock = threading.Lock()
latest_frame = None
annotated_frame_lock = threading.Lock()
latest_annotated_frame = None
ffmpeg_process = None
encode_queue = queue.Queue()
state = {"recording": False, "filename": None, "error": None}

measurements: list[dict] = []
measurements_lock = threading.Lock()

# Raw-frame backup pipeline: fully independent of encode_queue/frame_lock beyond
# the crop-and-copy performed inside capture_loop's existing frame_lock section.
# raw_frame_writer_loop is the sole owner of raw_frame_file; /start, /stop, and
# _shutdown only ever signal it via raw_frame_queue + raw_frame_closed_event.
raw_frame_queue = queue.Queue()  # deliberately unbounded, like encode_queue - see docs
raw_frame_lock = threading.Lock()
raw_frame_file = None
raw_frame_filename = None
raw_frame_frame_count = 0
raw_frame_error = None
raw_frame_active = False
raw_frame_closed_event = threading.Event()
_RAW_FRAME_CLOSE_SENTINEL = object()


def capture_loop():
    global latest_frame
    while True:
        ret, frame = cap.read()
        if not ret:
            msg = "Failed to capture frame"
            if state["error"] != msg:
                logger.error(msg)
            state["error"] = msg  # ty: ignore[invalid-assignment]
            time.sleep(0.05)
            continue
        with frame_lock:
            latest_frame = frame.copy()
            if state["recording"]:
                try:
                    encode_queue.put(frame.copy())
                except queue.Full:
                    msg = "Encoding queue is full, dropping frame"
                    if state["error"] != msg:
                        logger.warning(msg)
                    state["error"] = msg  # ty: ignore[invalid-assignment]
            if raw_frame_active and config.roi is not None:
                rx, ry, rw, rh = config.roi
                raw_frame_queue.put(
                    (frame[ry : ry + rh, rx : rx + rw].copy(), time.time())
                )


def encoding_loop():
    while True:
        try:
            frame = encode_queue.get()
        except queue.Empty:
            continue
        with frame_lock:
            proc = ffmpeg_process
            if proc is not None:
                try:
                    proc.stdin.write(frame)  # ty: ignore[unresolved-attribute]
                except BrokenPipeError:
                    msg = "FFmpeg process has terminated unexpectedly"
                    logger.error(msg)
                    state["error"] = msg  # ty: ignore[invalid-assignment]
                    state["recording"] = False


def raw_frame_writer_loop():
    """Sole owner of raw_frame_file. Batches frames to amortize per-call HDF5
    overhead, flushing every RAW_FRAME_FLUSH_BATCH_SIZE frames or
    RAW_FRAME_FLUSH_INTERVAL_S seconds, whichever comes first.

    /start, /stop and _shutdown never touch raw_frame_file directly - they only
    ever put a frame or _RAW_FRAME_CLOSE_SENTINEL on raw_frame_queue, since HDF5
    is not safe for concurrent writers.
    """
    global raw_frame_frame_count, raw_frame_error, raw_frame_file

    frames_buf: list = []
    timestamps_buf: list = []
    last_flush = time.monotonic()

    def flush():
        nonlocal last_flush
        global raw_frame_frame_count, raw_frame_error
        if frames_buf and raw_frame_file is not None:
            try:
                with raw_frame_lock:
                    n = raw_frame_file["frames"].shape[0]
                    batch = len(frames_buf)
                    raw_frame_file["frames"].resize(n + batch, axis=0)
                    raw_frame_file["frames"][n : n + batch] = np.stack(frames_buf)
                    raw_frame_file["timestamps"].resize(n + batch, axis=0)
                    raw_frame_file["timestamps"][n : n + batch] = timestamps_buf
                    raw_frame_frame_count = n + batch
            except Exception as e:
                msg = f"Failed to write raw frame batch: {e}"
                if raw_frame_error != msg:
                    logger.error(msg)
                raw_frame_error = msg
        frames_buf.clear()
        timestamps_buf.clear()
        last_flush = time.monotonic()

    while True:
        try:
            item = raw_frame_queue.get(timeout=0.5)
        except queue.Empty:
            if (
                frames_buf
                and (time.monotonic() - last_flush) > RAW_FRAME_FLUSH_INTERVAL_S
            ):
                flush()
            continue

        if item is _RAW_FRAME_CLOSE_SENTINEL:
            flush()
            with raw_frame_lock:
                if raw_frame_file is not None:
                    raw_frame_file.flush()
                    raw_frame_file.close()
                    raw_frame_file = None
            raw_frame_closed_event.set()
            continue

        frame, ts = item
        frames_buf.append(frame)
        timestamps_buf.append(ts)
        if len(frames_buf) >= RAW_FRAME_FLUSH_BATCH_SIZE:
            flush()


def ellipse_detection_loop():
    global latest_annotated_frame
    while True:
        with frame_lock:
            frame = latest_frame.copy() if latest_frame is not None else None

        if frame is None:
            time.sleep(0.02)
            continue

        measurement, ellipse = detect_ellipse(frame, config)

        annotated = frame
        if ellipse is not None or config.roi is not None:
            annotated = draw_overlay(frame, ellipse, config.roi)

        with annotated_frame_lock:
            latest_annotated_frame = annotated

        if state["recording"] and measurement is not None:
            with measurements_lock:
                measurements.append(measurement)


threading.Thread(target=capture_loop, daemon=True).start()
threading.Thread(target=encoding_loop, daemon=True).start()
threading.Thread(target=ellipse_detection_loop, daemon=True).start()
threading.Thread(target=raw_frame_writer_loop, daemon=True).start()


def generate_preview():
    cv2_params = [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_QUALITY]
    while True:
        with annotated_frame_lock:
            frame = latest_annotated_frame
        if frame is None:
            with frame_lock:
                frame = latest_frame
        if frame is None:
            continue
        _, buffer = cv2.imencode(".jpg", frame, cv2_params)
        yield (
            b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"
        )


@app.route("/preview")
def preview():
    return Response(
        generate_preview(), mimetype="multipart/x-mixed-replace; boundary=frame"
    )


def _start_raw_frame_saving(timestamp: str) -> None:
    """Preflight-check disk space and open a new per-recording raw-frame HDF5
    file if raw-frame saving is configured (requires both raw_frame_dir and
    roi). Never raises - on any problem it leaves raw-frame saving disabled
    for this recording, surfaced via raw_frame_error, and the mp4 recording
    proceeds unaffected either way.
    """
    global raw_frame_file, raw_frame_filename, raw_frame_frame_count
    global raw_frame_error, raw_frame_active

    raw_frame_filename = None
    raw_frame_frame_count = 0
    raw_frame_error = None
    raw_frame_active = False

    if config.raw_frame_dir is None:
        return
    if config.roi is None:
        msg = "raw_frame_dir is set but roi is not - raw frame saving disabled"
        logger.warning(msg)
        raw_frame_error = msg
        return

    try:
        os.makedirs(config.raw_frame_dir, exist_ok=True)
        free_mb = shutil.disk_usage(config.raw_frame_dir).free / (1024 * 1024)
        if free_mb < config.raw_frame_min_free_mb:
            msg = (
                f"Insufficient disk space for raw frame saving: "
                f"{free_mb:.0f} MB free < {config.raw_frame_min_free_mb:.0f} MB required"
            )
            logger.warning(msg)
            raw_frame_error = msg
            return

        while not raw_frame_queue.empty():
            raw_frame_queue.get_nowait()

        rx, ry, rw, rh = config.roi
        filename = f"raw_frames_{timestamp}.h5"
        path = os.path.join(config.raw_frame_dir, filename)
        f = h5py.File(path, "w")
        f.attrs["roi"] = config.roi
        f.attrs["pixels_per_mm"] = config.pixels_per_mm
        f.attrs["channel_order"] = "BGR"
        f.create_dataset(
            "frames",
            shape=(0, rh, rw, 3),
            maxshape=(None, rh, rw, 3),
            dtype="uint8",
            chunks=(1, rh, rw, 3),
        )
        f.create_dataset("timestamps", shape=(0,), maxshape=(None,), dtype="float64")
    except Exception as e:
        msg = f"Failed to open raw frame file: {e}"
        logger.error(msg)
        raw_frame_error = msg
        return

    with raw_frame_lock:
        raw_frame_file = f
    raw_frame_filename = filename
    raw_frame_active = True


@app.route("/start", methods=["POST"])
def start_recording():
    global ffmpeg_process
    if state["recording"]:
        return jsonify({"status": "already recording"}), 400

    while not encode_queue.empty():
        encode_queue.get_nowait()

    with measurements_lock:
        measurements.clear()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"recording_{timestamp}.mp4"
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    _start_raw_frame_saving(timestamp)

    proc = subprocess.Popen(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-s",
            f"{w}x{h}",
            "-r",
            str(RECORD_FPS),
            "-i",
            "pipe:0",
            "-vcodec",
            "libx264",
            "-crf",
            str(H264_CRF),
            "-preset",
            "fast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            filename,
        ],
        stdin=subprocess.PIPE,
    )

    with frame_lock:
        ffmpeg_process = proc

    state.update({"recording": True, "filename": filename, "error": None})  # ty: ignore[no-matching-overload]
    logger.info("Recording started: %s (%dx%d @ %d fps)", filename, w, h, RECORD_FPS)
    return jsonify(
        {
            "message": "Recording started",
            "filename": filename,
            "raw_frame_filename": raw_frame_filename,
            "raw_frame_saving_enabled": raw_frame_active,
        }
    )


@app.route("/stop", methods=["POST"])
def stop_recording():
    global ffmpeg_process, raw_frame_active
    if not state["recording"]:
        return jsonify({"status": "not recording", "error": state["error"]}), 400

    while not encode_queue.empty():
        try:
            frame = encode_queue.get_nowait()
            if ffmpeg_process:
                ffmpeg_process.stdin.write(frame)  # ty: ignore[unresolved-attribute]
        except queue.Empty:
            break

    with frame_lock:
        proc = ffmpeg_process
        ffmpeg_process = None

    if proc:
        proc.stdin.close()  # ty: ignore[unresolved-attribute]
        proc.wait()

    state["recording"] = False
    raw_frame_active = False

    # Route the final flush+close through raw_frame_writer_loop (the sole owner
    # of raw_frame_file) rather than touching it here - HDF5 is not safe for
    # concurrent writers. Harmless no-op if raw-frame saving wasn't active.
    raw_frame_closed_event.clear()
    raw_frame_queue.put(_RAW_FRAME_CLOSE_SENTINEL)
    if not raw_frame_closed_event.wait(timeout=RAW_FRAME_CLOSE_TIMEOUT_S):
        logger.error("Timed out waiting for raw frame file to close")

    with measurements_lock:
        n = len(measurements)

    with raw_frame_lock:
        raw_count = raw_frame_frame_count

    logger.info(
        "Recording stopped: %s (%d measurements, %d raw frames, error=%s)",
        state["filename"],
        n,
        raw_count,
        state["error"],
    )
    return jsonify(
        {
            "message": "Recording stopped",
            "filename": state["filename"],
            "error": state["error"],
            "measurement_count": n,
            "raw_frame_filename": raw_frame_filename,
            "raw_frame_count": raw_count,
            "raw_frame_error": raw_frame_error,
        }
    )


def _resolve_recording_path(base_dir: str, filename: str) -> str | None:
    """Resolve `filename` to an absolute path under `base_dir`, rejecting any
    path traversal. Returns None if invalid or outside base_dir."""
    if (
        not filename
        or filename in (".", "..")
        or os.path.basename(filename) != filename
    ):
        return None
    base_dir = os.path.abspath(base_dir)
    path = os.path.abspath(os.path.join(base_dir, filename))
    if os.path.commonpath([base_dir, path]) != base_dir:
        return None
    return path


# camera_macro.py pulls both the video and the raw-frame backup through this
# same shape of endpoint pair after /stop, then copies the bytes into the
# scan's ScanDir and acks so the local hutch-laptop copy can be cleaned up.
@dataclasses.dataclass(frozen=True)
class _RecordingKind:
    base_dir: Callable[[], str | None]
    is_active: Callable[[str], bool]
    mimetype: str


_RECORDING_KINDS: dict[str, _RecordingKind] = {
    "video": _RecordingKind(
        base_dir=lambda: os.getcwd(),
        is_active=lambda filename: bool(
            state["recording"] and filename == state["filename"]
        ),
        mimetype="video/mp4",
    ),
    "raw_frames": _RecordingKind(
        base_dir=lambda: config.raw_frame_dir,
        is_active=lambda filename: bool(
            raw_frame_active and filename == raw_frame_filename
        ),
        mimetype="application/x-hdf5",
    ),
}


def _get_recording_file(kind: str, filename: str):
    spec = _RECORDING_KINDS[kind]
    if spec.is_active(filename):
        return jsonify({"error": "recording in progress, stop first"}), 409
    base_dir = spec.base_dir()
    path = _resolve_recording_path(base_dir, filename) if base_dir else None
    if path is None or not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    return send_file(
        path, mimetype=spec.mimetype, as_attachment=True, download_name=filename
    )


def _ack_recording_file(kind: str, filename: str):
    spec = _RECORDING_KINDS[kind]
    if spec.is_active(filename):
        return jsonify({"error": "recording in progress, stop first"}), 409
    base_dir = spec.base_dir()
    path = _resolve_recording_path(base_dir, filename) if base_dir else None
    if path is None:
        return jsonify({"error": "invalid filename"}), 400
    if not os.path.isfile(path):
        return jsonify({"deleted": False, "already_gone": True})
    os.remove(path)
    return jsonify({"deleted": True, "already_gone": False})


app.add_url_rule(
    "/video/<filename>",
    "get_video_file",
    lambda filename: _get_recording_file("video", filename),
)
app.add_url_rule(
    "/video/<filename>/ack",
    "ack_video_file",
    lambda filename: _ack_recording_file("video", filename),
    methods=["POST"],
)
app.add_url_rule(
    "/raw_frames/<filename>",
    "get_raw_frame_file",
    lambda filename: _get_recording_file("raw_frames", filename),
)
app.add_url_rule(
    "/raw_frames/<filename>/ack",
    "ack_raw_frame_file",
    lambda filename: _ack_recording_file("raw_frames", filename),
    methods=["POST"],
)


def debug_frame():
    """Return the binary threshold image (within ROI) that the ellipse detector sees.

    Only registered when --debug is passed. Detected contour candidates are
    outlined in white; the chosen contour in red.
    """
    with frame_lock:
        frame = latest_frame.copy() if latest_frame is not None else None
    if frame is None:
        return Response("No frame available yet", status=503, mimetype="text/plain")

    if config.roi is not None:
        rx, ry, rw, rh = config.roi
        frame = frame[ry : ry + rh, rx : rx + rw]

    binary = _binarize(frame, config)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # Render binary as BGR so we can draw coloured overlays
    debug = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)

    candidates = find_candidate_contours(contours, config)
    # All contours in the frame (grey), candidates in white, winner in red
    cv2.drawContours(debug, contours, -1, (100, 100, 100), 1)
    cv2.drawContours(debug, candidates, -1, (255, 255, 255), 1)
    if candidates:
        winner = max(candidates, key=cv2.contourArea)
        cv2.drawContours(debug, [winner], -1, (0, 0, 255), 2)

    # Annotate with current param values so a screenshot is self-documenting
    lines = [
        f"threshold={config.threshold}  blur={config.blur_kernel}  close={config.morph_close_size}  gradient={config.use_gradient}",
        f"min_area={config.min_contour_area:.0f}  max_area={config.max_contour_area:.0f}",
        f"candidates={len(candidates)}  all_contours={len(contours)}",
    ]
    for i, txt in enumerate(lines):
        cv2.putText(
            debug,
            txt,
            (6, 18 + i * 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 220, 255),
            1,
            cv2.LINE_AA,
        )

    _, buf = cv2.imencode(".jpg", debug, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return Response(
        buf.tobytes(), mimetype="image/jpeg", headers={"Cache-Control": "no-store"}
    )


if DEBUG_MODE:
    app.add_url_rule("/debug_frame", "debug_frame", debug_frame)


@app.route("/measurements")
def get_measurements():
    with measurements_lock:
        snapshot = list(measurements)
    return jsonify(snapshot)


@app.route("/measurements/latest")
def get_latest_measurement():
    with measurements_lock:
        m = measurements[-1] if measurements else None
    if m is None:
        return jsonify({"error": "no measurements yet"}), 404
    return jsonify(m)


@app.route("/volume_plot")
def volume_plot_page():
    return """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Droplet volume</title>
  <style>
    body { margin: 0; background: #111; display: flex; flex-direction: column;
           align-items: center; justify-content: center; min-height: 100vh;
           font-family: sans-serif; color: #ccc; }
    img  { max-width: 95vw; border-radius: 6px; }
  </style>
</head>
<body>
  <img id="plot" src="/volume_plot.png" alt="Volume plot">
  <script>
    setInterval(function() {
      document.getElementById("plot").src = "/volume_plot.png?t=" + Date.now();
    }, 1000);
  </script>
</body>
</html>"""


@app.route("/volume_plot.png")
def volume_plot_png():
    with measurements_lock:
        snapshot = list(measurements)

    fig = Figure(figsize=(8, 4), facecolor="#1a1a1a")
    ax = fig.add_subplot(111, facecolor="#1a1a1a")

    for spine in ax.spines.values():
        spine.set_edgecolor("#555")
    ax.tick_params(colors="#ccc")
    ax.xaxis.label.set_color("#ccc")
    ax.yaxis.label.set_color("#ccc")
    ax.title.set_color("#eee")

    if len(snapshot) >= 2:
        t0 = snapshot[0]["timestamp"]
        times = [m["timestamp"] - t0 for m in snapshot]
        volumes = [m["volume_mm3"] for m in snapshot]
        ax.plot(times, volumes, color="#4da6ff", linewidth=1.5)
        ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.4f"))
    else:
        ax.text(
            0.5,
            0.5,
            "No data yet - start a recording",
            transform=ax.transAxes,
            ha="center",
            va="center",
            color="#888",
        )

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Volume (mm^3)")
    ax.set_title("Droplet volume over time")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    buf.seek(0)
    return Response(
        buf.getvalue(), mimetype="image/png", headers={"Cache-Control": "no-store"}
    )


@app.route("/calibration")
def get_calibration():
    return jsonify(dataclasses.asdict(config))


@app.route("/status")
def status():
    with measurements_lock:
        n = len(measurements)
    with raw_frame_lock:
        raw_count = raw_frame_frame_count
    return jsonify(
        {
            **state,
            "camera_connected": cap.isOpened(),
            "measurement_count": n,
            "raw_frame_saving_enabled": raw_frame_active,
            "raw_frame_filename": raw_frame_filename,
            "raw_frame_count": raw_count,
            "raw_frame_error": raw_frame_error,
        }
    )


def _shutdown(sig, frame):
    global ffmpeg_process
    logger.info("Shutting down (signal %s)", sig)
    if ffmpeg_process is not None:
        ffmpeg_process.kill()
    # Harmless no-op if raw-frame saving wasn't active; bounded wait so
    # shutdown can't hang indefinitely.
    raw_frame_closed_event.clear()
    raw_frame_queue.put(_RAW_FRAME_CLOSE_SENTINEL)
    raw_frame_closed_event.wait(timeout=RAW_FRAME_CLOSE_TIMEOUT_S)
    cap.release()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    logger.info("Starting with config: %s", config)
    app.run(host="0.0.0.0", port=8989, threaded=True)
