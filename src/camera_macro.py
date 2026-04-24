import os

import h5py
import numpy as np
import requests
from sardana.macroserver.macro import Macro, Type

CAMERA_URL = "http://<hutch-laptop-ip>:8989"  # TODO: make this configurable via env var or macro arg
TIMEOUT = 10  # TODO: also make this configurable, and maybe add retry logic to handle transient failures better


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
            r = requests.get(f"{CAMERA_URL}/status", timeout=TIMEOUT)
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
            r = requests.post(f"{CAMERA_URL}/start", timeout=TIMEOUT)
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
            r = requests.post(f"{CAMERA_URL}/stop", timeout=TIMEOUT)
            data = r.json()
            return data.get("filename"), data.get("error")
        except Exception as e:
            self.warning(f"Failed to stop camera: {e}")
            return None, str(e)

    def _fetch_measurements(self) -> list[dict]:
        """Fetch the ellipse measurement time-series from the server."""
        try:
            r = requests.get(f"{CAMERA_URL}/measurements", timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            self.warning(f"Failed to fetch measurements: {e}")
            return []

    def _write_to_hdf5(self, video_filename, measurements: list[dict]):
        scan_dir = self.getEnv("ScanDir")
        scan_file = self.getEnv("ScanFile")
        if isinstance(scan_file, (list, tuple)):
            scan_file = next((f for f in scan_file if f.endswith(".h5")), None)
        if not scan_file:
            self.warning("No HDF5 scan file found, skipping metadata write")
            return
        h5_path = os.path.join(scan_dir, scan_file)
        if not os.path.exists(h5_path):
            self.warning(f"HDF5 file not found at {h5_path}")
            return
        try:
            with h5py.File(h5_path, "a") as f:
                entries = sorted(k for k in f.keys() if k.startswith("entry"))
                grp = f[entries[-1]].require_group("custom_data")
                grp.attrs["NX_class"] = "NXcollection"

                for name in ("video_file", "camera_preview_url"):
                    if name in grp:
                        del grp[name]
                grp.create_dataset("video_file", data=video_filename)
                grp.create_dataset("camera_preview_url", data=f"{CAMERA_URL}/preview")

                if measurements:
                    mgrp = grp.require_group("ellipse_tracking")
                    mgrp.attrs["NX_class"] = "NXcollection"
                    arrays = {
                        "timestamp": np.array([m["timestamp"] for m in measurements]),
                        "cx_mm": np.array([m["cx_mm"] for m in measurements]),
                        "cy_mm": np.array([m["cy_mm"] for m in measurements]),
                        "volume_mm3": np.array([m["volume_mm3"] for m in measurements]),
                    }
                    for dset_name, data in arrays.items():
                        if dset_name in mgrp:
                            del mgrp[dset_name]
                        mgrp.create_dataset(dset_name, data=data)
                    mgrp.attrs["volume_model"] = "oblate_spheroid"
                    mgrp.attrs["description"] = (
                        "Ellipse fit per frame: cx/cy are droplet center in mm, "
                        "volume assumes oblate spheroid V=(4/3)*pi*a^2*b."
                    )
                    self.info(
                        f"Written {len(measurements)} ellipse measurements to {h5_path}"
                    )
                else:
                    self.warning("No ellipse measurements to write")

            self.info(f"Camera metadata written to {h5_path}")
        except Exception as e:
            self.warning(f"Could not write to HDF5: {e}")

    def run(self, scan_macro, scan_args):
        video_filename = None

        # 1. Pre-flight: confirm server is up and camera is live
        try:
            self._check_server()
        except RuntimeError as e:
            self.error(str(e))
            return None

        # 2. Start recording (also clears any previous measurements on the server)
        try:
            video_filename = self._start_camera()
        except RuntimeError as e:
            self.error(str(e))
            self.warning("Scan will NOT run — camera recording could not start.")
            return None

        # 3. Run the scan — always stop camera afterwards
        try:
            self.execMacro([scan_macro] + scan_args)
        except Exception as e:
            self.error(f"Scan failed: {e}")
        finally:
            stopped_filename, cam_error = self._stop_camera()
            if cam_error:
                self.warning(f"Camera error during recording: {cam_error}")
                self.warning(f"File may be incomplete or corrupted: {stopped_filename}")

        # 4. Fetch ellipse measurements accumulated during the scan
        measurements = self._fetch_measurements()
        self.info(f"Fetched {len(measurements)} ellipse measurements")

        # 5. Persist filename and measurements
        self.setEnv("LastVideoFile", video_filename)
        self._write_to_hdf5(video_filename, measurements)

        return video_filename
