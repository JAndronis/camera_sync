import numpy as np
import requests
from sardana.macroserver.macro import Macro, Type

CAMERA_URL = "http://<hutch-laptop-ip>:8989"  # TODO: make this configurable via env var or macro arg
TIMEOUT = 10  # TODO: also make this configurable, and maybe add retry logic to handle transient failures better
session = requests.Session()
session.trust_env = False

class camera_scan(Macro):
    """
    Wraps any Sardana scan macro with synchronized camera recording.
    The camera server runs continuously, serving a live preview stream
    at /preview independent of scan state. Recording is started and
    stopped around the scan via /start and /stop.

    During recording the server continuously fits an ellipse to the droplet
    and stores (timestamp, cx_mm, cy_mm, volume_mm3) per frame. These
    measurements are fetched after the scan and written to the HDF5 file.

    Usage:  camera_scan ascan motor1 0 100 50 0.1
    Preview: http://<hutch-laptop-ip>:8989/preview  (open anytime in browser)
    """

    param_def = [
        ["scan_macro", Type.String, None, "Scan macro to run (e.g. ascan)"],
        ["scan_args", [["arg", Type.String, None, "Argument"]], None, "Scan arguments"],
    ]

    result_def = [["video_file", Type.String, None, "Recorded video filename"]]

    def _check_server(self):
        """Verify the camera server is reachable and the camera is live."""
        try:
            r = session.get(f"{CAMERA_URL}/status", timeout=TIMEOUT)
            r.raise_for_status()
            status = r.json()
            # If latest_frame is None the capture_loop hasn't started yet
            if not status.get("camera_open", True):
                raise RuntimeError("Camera server is up but camera is not open")
            self.info(f"Camera server OK — preview at {CAMERA_URL}/preview")
        except requests.exceptions.ConnectionError:
            raise RuntimeError(
                f"Camera server unreachable at {CAMERA_URL}. "
                "Is camera_server.py running on the hutch laptop?"
            )

    def _start_camera(self):
        try:
            r = session.post(f"{CAMERA_URL}/start", timeout=TIMEOUT)
            r.raise_for_status()
            filename = r.json().get("filename")
            if not filename:
                raise RuntimeError("Server returned no filename")
            self.info(f"Recording started: {filename}")
            return filename
        except Exception as e:
            raise RuntimeError(f"Failed to start recording: {e}")

    def _stop_camera(self):
        try:
            r = session.post(f"{CAMERA_URL}/stop", timeout=TIMEOUT)
            data = r.json()
            return data.get("filename"), data.get("error")
        except Exception as e:
            self.warning(f"Failed to stop camera: {e}")
            return None, str(e)

    def _fetch_measurements(self) -> list[dict]:
        """Fetch the ellipse measurement time-series from the server."""
        try:
            r = session.get(f"{CAMERA_URL}/measurements", timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            self.warning(f"Failed to fetch measurements: {e}")
            return []

    def _write_measurements(self, scan_macro, measurements: list[dict]):
        """Write ellipse timestamp and volume arrays via the scan data handler.

        Called after the scan has finished so all other devices have already
        flushed their data.
        """
        if not measurements:
            self.warning("No ellipse measurements to write")
            return
        try:
            dh = scan_macro._gScan._data_handler  # type: ignore[attr-defined]
            timestamps = np.array([m["timestamp"] for m in measurements], dtype=float)
            volumes = np.array([m["volume_mm3"] for m in measurements], dtype=float)
            dh.addCustomData(timestamps, "side_camera_timestamp", dtype=float)
            dh.addCustomData(volumes, "side_camera_volume_mm3", dtype=float)
            self.info(f"Written {len(measurements)} ellipse measurements via addCustomData")
        except Exception as e:
            self.warning(f"Could not write measurements: {e}")

    def run(self, scan_macro, scan_args):
        video_filename = None

        # 1. Pre-flight: confirm server is up and camera is live
        try:
            self._check_server()
            self.info("Camera server is online and working fine.")
        except RuntimeError as e:
            self.error(str(e))
            self.warning(f"Camera server is not running, error is {str(e)}. Scan will not have video.")
            return None

        # 2. Start recording (also clears any previous measurements on the server)
        try:
            video_filename = self._start_camera()
            self.info("Camera video started")
        except RuntimeError as e:
            self.error(str(e))
            self.warning("Scan will NOT run — camera recording could not start.")
            return None

        # 3. Run the scan — always stop camera afterwards
        inner_macro = None
        try:
            inner_macro = self.execMacro([scan_macro] + scan_args)
        except Exception as e:
            self.error(f"Scan failed: {e}")
        finally:
            stopped_filename, cam_error = self._stop_camera()
            if cam_error:
                self.warning(f"Camera error during recording: {cam_error}")
                self.warning(f"File may be incomplete or corrupted: {stopped_filename}")
            else:
                self.infor(f"Camera video saved: {video_filename}")

        # 4. Fetch ellipse measurements accumulated during the scan
        measurements = self._fetch_measurements()
        self.info(f"Fetched {len(measurements)} ellipse measurements")

        # 5. Persist filename and measurements
        self.setEnv("LastVideoFile", video_filename)
        if inner_macro is not None:
            self._write_measurements(inner_macro, measurements)

        return video_filename
