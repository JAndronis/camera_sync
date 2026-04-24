import cv2
import threading
from flask import Response, Flask


app = Flask(__name__)
cap = cv2.VideoCapture(0)
frame_lock = threading.Lock()
latest_frame = None
recording = False
video_writer = None
record_thread = None
state = {
    "recording": False,
    "filename": None,
    "error": None
}


def capture_loop():
    global latest_frame, recording, video_writer, state
    while True:
        ret, frame = cap.read()
        if not ret:
            state["error"] = "Failed to capture frame"
            continue
        with frame_lock:
            latest_frame = frame.copy()
        if recording and video_writer is not None:
            video_writer.write(frame)
            

threading.Thread(target=capture_loop, daemon=True).start()


def generate_preview():
    while True:
        with frame_lock:
            if latest_frame is None:
                continue
            _, buffer = cv2.imencode('.jpg', latest_frame)
        yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n")

            
@app.route('/preview')
def preview():
    return Response(generate_preview(), mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=8989)