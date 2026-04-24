import argparse
import dataclasses
import io
import json
import logging
import math
import queue
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

import cv2
import matplotlib
import matplotlib.ticker as mticker
from flask import Flask, Response, jsonify
from matplotlib.figure import Figure

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


@dataclasses.dataclass
class Config:
    camera_index: int = 0
    pixels_per_mm: float = 1.0
    # Ellipse detection params
    blur_kernel: int = 5  # Gaussian blur kernel size (must be odd)
    threshold: int = 127  # binary threshold (0-255); pixels below → foreground
    min_contour_area: float = 200.0  # px²
    max_contour_area: float = 100000.0  # px²
    # Optional crop applied before detection; set to [x, y, width, height] in pixels.
    # Strongly recommended: keeps the cylinder / transducers out of the search area.
    roi: list | None = dataclasses.field(default=None)


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
    p.add_argument("--roi", type=int, nargs=4, metavar=("X", "Y", "W", "H"),
                   help="Detection region in pixels: x y width height")
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
        "roi": args.roi,
    }
    for attr, val in cli_overrides.items():
        if val is not None:
            setattr(cfg, attr, val)
    return cfg


config = _build_config(_parse_args())

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


def capture_loop():
    global latest_frame
    while True:
        ret, frame = cap.read()
        if not ret:
            msg = "Failed to capture frame"
            if state["error"] != msg:
                logger.error(msg)
            state["error"] = msg
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
                    state["error"] = msg


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
                    proc.stdin.write(frame)
                except BrokenPipeError:
                    msg = "FFmpeg process has terminated unexpectedly"
                    logger.error(msg)
                    state["error"] = msg
                    state["recording"] = False


def _detect_ellipse(frame) -> tuple[dict, tuple] | tuple[None, None]:
    """Fit an ellipse to the largest foreground contour within the configured ROI.

    Returns (measurement_dict, cv2_ellipse_in_full_frame) or (None, None).
    """
    roi_offset_x = roi_offset_y = 0
    if config.roi is not None:
        rx, ry, rw, rh = config.roi
        frame = frame[ry : ry + rh, rx : rx + rw]
        roi_offset_x, roi_offset_y = rx, ry

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    k = config.blur_kernel | 1  # ensure odd
    blurred = cv2.GaussianBlur(gray, (k, k), 0)
    # THRESH_BINARY_INV: pixels below threshold become foreground (white)
    _, binary = cv2.threshold(blurred, config.threshold, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates = [
        c
        for c in contours
        if config.min_contour_area <= cv2.contourArea(c) <= config.max_contour_area
        and len(c) >= 5  # fitEllipse requires at least 5 points
    ]
    if not candidates:
        return None, None

    contour = max(candidates, key=cv2.contourArea)
    ellipse = cv2.fitEllipse(contour)
    (cx_px, cy_px), axes_px, angle = ellipse

    # Shift ellipse centre back to full-frame coordinates for drawing
    cx_full = cx_px + roi_offset_x
    cy_full = cy_px + roi_offset_y
    ellipse_full_frame = ((cx_full, cy_full), axes_px, angle)

    # Convert to mm; semi-axes (half the full axis lengths)
    axis1_px, axis2_px = axes_px
    a_mm = max(axis1_px, axis2_px) / 2.0 / config.pixels_per_mm  # equatorial semi-axis
    b_mm = min(axis1_px, axis2_px) / 2.0 / config.pixels_per_mm  # polar semi-axis

    # Oblate spheroid (axial symmetry around the polar axis): V = (4/3) * pi * a^2 * b
    volume_mm3 = (4.0 / 3.0) * math.pi * a_mm**2 * b_mm

    measurement = {
        "timestamp": time.time(),
        "cx_mm": cx_full / config.pixels_per_mm,
        "cy_mm": cy_full / config.pixels_per_mm,
        "volume_mm3": volume_mm3,
    }
    return measurement, ellipse_full_frame


def ellipse_detection_loop():
    global latest_annotated_frame
    while True:
        with frame_lock:
            frame = latest_frame.copy() if latest_frame is not None else None

        if frame is None:
            time.sleep(0.02)
            continue

        measurement, ellipse = _detect_ellipse(frame)

        annotated = frame
        if ellipse is not None or config.roi is not None:
            annotated = frame.copy()
            if config.roi is not None:
                rx, ry, rw, rh = config.roi
                cv2.rectangle(annotated, (rx, ry), (rx + rw, ry + rh), (0, 200, 255), 1)
            if ellipse is not None:
                cv2.ellipse(annotated, ellipse, (0, 255, 0), 2)

        with annotated_frame_lock:
            latest_annotated_frame = annotated

        if state["recording"] and measurement is not None:
            with measurements_lock:
                measurements.append(measurement)


threading.Thread(target=capture_loop, daemon=True).start()
threading.Thread(target=encoding_loop, daemon=True).start()
threading.Thread(target=ellipse_detection_loop, daemon=True).start()


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

    state.update({"recording": True, "filename": filename, "error": None})
    logger.info("Recording started: %s (%dx%d @ %d fps)", filename, w, h, RECORD_FPS)
    return jsonify({"message": "Recording started", "filename": filename})


@app.route("/stop", methods=["POST"])
def stop_recording():
    global ffmpeg_process
    if not state["recording"]:
        return jsonify({"status": "not recording", "error": state["error"]}), 400

    while not encode_queue.empty():
        try:
            frame = encode_queue.get_nowait()
            if ffmpeg_process:
                ffmpeg_process.stdin.write(frame)
        except queue.Empty:
            break

    with frame_lock:
        proc = ffmpeg_process
        ffmpeg_process = None

    if proc:
        proc.stdin.close()
        proc.wait()

    state["recording"] = False

    with measurements_lock:
        n = len(measurements)

    logger.info(
        "Recording stopped: %s (%d measurements, error=%s)",
        state["filename"],
        n,
        state["error"],
    )
    return jsonify(
        {
            "message": "Recording stopped",
            "filename": state["filename"],
            "error": state["error"],
            "measurement_count": n,
        }
    )


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
            "No data yet — start a recording",
            transform=ax.transAxes,
            ha="center",
            va="center",
            color="#888",
        )

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Volume (mm³)")
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
    return jsonify(
        {**state, "camera_connected": cap.isOpened(), "measurement_count": n}
    )


def _shutdown(sig, frame):
    global ffmpeg_process
    logger.info("Shutting down (signal %s)", sig)
    if ffmpeg_process is not None:
        ffmpeg_process.kill()
    cap.release()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    logger.info("Starting with config: %s", config)
    app.run(host="0.0.0.0", port=8989, threaded=True)
