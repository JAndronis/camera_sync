import queue
import subprocess
import threading
from datetime import datetime

import cv2
from flask import Flask, Response, jsonify

app = Flask(__name__)
cap = cv2.VideoCapture(0)
frame_lock = threading.Lock()
latest_frame = None
ffmpeg_process = None
encode_queue = queue.Queue()
state = {"recording": False, "filename": None, "error": None}


PREVIEW_QUALITY = 50
H264_CRF = 28
RECORD_FPS = 30


def capture_loop():
    global latest_frame
    while True:
        ret, frame = cap.read()
        if not ret:
            state["error"] = "Failed to capture frame"
            continue
        with frame_lock:
            latest_frame = frame.copy()
            if state["recording"]:
                try:
                    encode_queue.put(frame.copy())
                except queue.Full:
                    state["error"] = "Encoding queue is full, dropping frame"


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
                    state["error"] = "FFmpeg process has terminated unexpectedly"
                    state["recording"] = False


threading.Thread(target=capture_loop, daemon=True).start()
threading.Thread(target=encoding_loop, daemon=True).start()


def generate_preview():
    cv2_params = [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_QUALITY]
    while True:
        with frame_lock:
            if latest_frame is None:
                continue
            frame_copy = latest_frame.copy()
        _, buffer = cv2.imencode(".jpg", frame_copy, cv2_params)
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
    return jsonify(
        {
            "message": "Recording stopped",
            "filename": state["filename"],
            "error": state["error"],
        }
    )


@app.route("/status")
def status():
    return jsonify({**state, "camera_connected": cap.isOpened()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8989)
