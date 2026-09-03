import logging
import os

import numpy as np
import requests
from sardana.macroserver.macro import Macro, Type

CAMERA_URL = "http://<hutch-laptop-ip>:8989"  # TODO: make this configurable via env var or macro arg
TIMEOUT = 10  # TODO: also make this configurable, and maybe add retry logic to handle transient failures better
# The raw-frame backup is streamed straight to disk (not held in memory), but
# a large recording can still take a while to transfer - this is deliberately
# separate from TIMEOUT, which stays sized for small JSON calls.
RAW_FRAME_FETCH_TIMEOUT = 300


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
        ["scan_macro", Type.String, None, "Scan macro to run (e.g. ascan)"],  # ty: ignore[unresolved-attribute]
        ["scan_args", [["arg", Type.String, None, "Argument"]], None, "Scan arguments"],  # ty: ignore[unresolved-attribute]
    ]

    result_def = [["video_file", Type.String, None, "Recorded video filename"]]  # ty: ignore[unresolved-attribute]

    def prepare(self, *args, **kwargs):
        self.output("Preparing...")
        self.setLogLevel(logging.DEBUG)
        self.session = requests.Session()
        self.session.trust_env = False
        return super().prepare(*args, **kwargs)

    def _check_server(self):
        """Verify the camera server is reachable and the camera is live."""
        try:
            r = self.session.get(f"{CAMERA_URL}/status", timeout=TIMEOUT)
            r.raise_for_status()
            status = r.json()
            # If latest_frame is None the capture_loop hasn't started yet
            if not status.get("camera_connected", True):
                raise RuntimeError("Camera server is up but camera is not open")
            self.info(f"Camera server OK - preview at {CAMERA_URL}/preview")
        except requests.exceptions.ConnectionError:
            raise RuntimeError(
                f"Camera server unreachable at {CAMERA_URL}. "
                "Is camera_server.py running on the hutch laptop?"
            )

    def _start_camera(self):
        """Returns (video_filename, raw_frame_filename). raw_frame_filename is
        None if raw-frame saving isn't configured/enabled on the server."""
        try:
            r = self.session.post(f"{CAMERA_URL}/start", timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            filename = data.get("filename")
            if not filename:
                raise RuntimeError("Server returned no filename")
            self.info(f"Recording started: {filename}")
            return filename, data.get("raw_frame_filename")
        except Exception as e:
            raise RuntimeError(f"Failed to start recording: {e}")

    def _stop_camera(self):
        """Returns (video_filename, error, raw_frame_filename, raw_frame_count)."""
        try:
            r = self.session.post(f"{CAMERA_URL}/stop", timeout=TIMEOUT)
            data = r.json()
            return (
                data.get("filename"),
                data.get("error"),
                data.get("raw_frame_filename"),
                data.get("raw_frame_count") or 0,
            )
        except Exception as e:
            self.warning(f"Failed to stop camera: {e}")
            return None, str(e), None, 0

    def _fetch_measurements(self) -> list[dict]:
        """Fetch the ellipse measurement time-series from the server."""
        try:
            r = self.session.get(f"{CAMERA_URL}/measurements", timeout=TIMEOUT)
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
            dh.addCustomData(timestamps, "side_camera_timestamp")
            dh.addCustomData(volumes, "side_camera_volume_mm3")
            self.info(
                f"Written {len(measurements)} ellipse measurements via addCustomData"
            )
        except Exception as e:
            self.warning(f"Could not write measurements: {e}")

    def _copy_recording_to_scandir(
        self, kind: str, filename: str, scan_dir: str
    ) -> bool:
        """Stream GET /{kind}/{filename} from the camera server straight to
        disk under scan_dir (never held fully in memory), then ack so the
        hutch-laptop copy gets cleaned up - but only once the copy on disk
        has actually succeeded. Never raises; a failure here must not abort
        the scan or block measurement writing, it just leaves the file on
        the hutch laptop for manual recovery.
        """
        dest = os.path.join(scan_dir, filename)
        try:
            r = self.session.get(
                f"{CAMERA_URL}/{kind}/{filename}",
                stream=True,
                timeout=RAW_FRAME_FETCH_TIMEOUT,
            )
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        except Exception as e:
            self.warning(
                f"BACKUP NOT ARCHIVED: failed to copy {kind} file {filename} to "
                f"{scan_dir}: {e}. Recover manually from the hutch laptop if needed."
            )
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except OSError:
                pass
            return False

        try:
            ack = self.session.post(
                f"{CAMERA_URL}/{kind}/{filename}/ack", timeout=TIMEOUT
            )
            ack.raise_for_status()
        except Exception as e:
            self.warning(
                f"Archived {kind} file to {dest} but failed to ack cleanup on "
                f"hutch laptop: {e}"
            )

        self.info(f"Archived {kind} file to {dest}")
        return True

    def _write_file_reference(self, scan_macro, key: str, filename: str):
        """Record a small string reference (e.g. an archived filename) via the
        scan data handler - the same addCustomData mechanism used for the
        ellipse measurement arrays, just with a tiny string payload instead
        of a large array.
        """
        try:
            dh = scan_macro._gScan._data_handler  # type: ignore[attr-defined]
            dh.addCustomData(filename, key)
        except Exception as e:
            self.warning(f"Could not write {key} reference: {e}")

    def run(self, scan_macro, scan_args):  # ty: ignore[invalid-method-override]
        video_filename = None

        # 1. Pre-flight: confirm server is up and camera is live
        try:
            self._check_server()
            self.info("Camera server is online and working fine.")
        except RuntimeError as e:
            self.error(str(e))
            self.warning(
                f"Camera server is not running, error is {str(e)}. Scan will not have video."
            )
            return None

        # 2. Start recording (also clears any previous measurements on the server)
        raw_frame_filename = None
        try:
            video_filename, raw_frame_filename = self._start_camera()
            self.info("Camera video started")
        except RuntimeError as e:
            self.error(str(e))
            self.warning("Scan will NOT run - camera recording could not start.")
            return None

        # 3. Run the scan - always stop camera afterwards
        inner_macro = None
        raw_frame_count = 0
        try:
            inner_macro = self.execMacro([scan_macro] + scan_args)
        except Exception as e:
            self.error(f"Scan failed: {e}")
        finally:
            stopped_filename, cam_error, raw_frame_filename, raw_frame_count = (
                self._stop_camera()
            )
            if cam_error:
                self.warning(f"Camera error during recording: {cam_error}")
                self.warning(f"File may be incomplete or corrupted: {stopped_filename}")
            else:
                self.info(f"Camera video saved: {video_filename}")

        # 4. Fetch ellipse measurements accumulated during the scan
        measurements = self._fetch_measurements()
        self.info(f"Fetched {len(measurements)} ellipse measurements")

        # 4b. Archive the raw-frame backup onto server storage, alongside the
        # scan's own HDF5 file. The mp4 stays on the hutch laptop only - once
        # raw_frames.h5 is archived it holds the same recording losslessly,
        # so archiving both would be redundant. Best-effort and strictly
        # after the scan has already completed - a failure here can never
        # affect whether the scan itself succeeded.
        raw_frames_archived = False
        try:
            scan_dir = self.getEnv("ScanDir")
        except Exception as e:
            scan_dir = None
            self.warning(
                f"ScanDir not available, cannot archive raw frames to server: {e}"
            )

        if scan_dir and raw_frame_filename and raw_frame_count:
            raw_frames_archived = self._copy_recording_to_scandir(
                "raw_frames", raw_frame_filename, scan_dir
            )

        # 5. Persist filename and measurements
        self.setEnv("LastVideoFile", video_filename)
        if inner_macro is not None:
            self._write_measurements(inner_macro, measurements)
            if raw_frames_archived:
                self._write_file_reference(
                    inner_macro, "side_camera_raw_frame_file", raw_frame_filename
                )

        return video_filename
