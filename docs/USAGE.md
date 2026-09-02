# Usage reference

Full reference for the config file, CLI flags, HTTP API, and Sardana macro. For installation see [SETUP.md](SETUP.md); for a fast path to a working scan see [QUICKSTART.md](QUICKSTART.md).

## Configuration

`camera_server.py` builds its config in two layers: a JSON file (`--config`), then any CLI flags on top of it. Fields not present in the JSON file, and flags not passed, fall back to the defaults below.

| Field | CLI flag | Default | Meaning |
|---|---|---|---|
| `camera_index` | `--camera-index` | `0` | OpenCV `VideoCapture` device index |
| `pixels_per_mm` | `--pixels-per-mm` | `1.0` | Scale factor from pixels to millimetres; see [SETUP.md](SETUP.md#21-pixels_per_mm) |
| `blur_kernel` | `--blur-kernel` | `5` | Gaussian blur kernel size in px; even values are rounded up to odd |
| `threshold` | `--threshold` | `127` | Binary threshold (0–255); pixels **darker** than this become foreground |
| `min_contour_area` | `--min-contour-area` | `200.0` | Minimum contour area (px²) to be considered a candidate |
| `max_contour_area` | `--max-contour-area` | `100000.0` | Maximum contour area (px²) to be considered a candidate |
| `morph_close_size` | `--morph-close-size` | `0` | Morphological closing kernel size (px) to fill specular holes; `0` disables it |
| `roi` | `--roi X Y W H` | `None` (full frame) | Crop applied before detection: `[x, y, width, height]` in pixels |
| `use_gradient` | `--use-gradient` / `--no-use-gradient` | `False` | Threshold Sobel gradient magnitude instead of absolute intensity — see below |

Other flags:

| Flag | Meaning |
|---|---|
| `--config FILE` | JSON file providing the base config (see `calibration_example.json`) |
| `--debug` | Registers the `/debug_frame` endpoint |

Things that are **not** configurable via file or flag — edit the constants near the top of `camera_server.py` if you need to change them:

| Constant | Value | Meaning |
|---|---|---|
| Server port | `8989` | Set in the `app.run(...)` call |
| `PREVIEW_QUALITY` | `50` | JPEG quality for `/preview` |
| `H264_CRF` | `28` | Constant Rate Factor for the recorded H.264 video (lower = higher quality, bigger file) |
| `RECORD_FPS` | `30` | Framerate passed to ffmpeg for the recording container |

## Ellipse detection pipeline

For each frame, in order:

1. Crop to `roi` if configured.
2. Convert to grayscale, Gaussian blur with `blur_kernel`.
3. Threshold into a binary foreground mask (see "Absolute vs. gradient thresholding" below).
4. Optional morphological closing (`morph_close_size`) to bridge specular reflection holes (or gaps in the gradient ring, in gradient mode).
5. `findContours`, filtered to `min_contour_area <= area <= max_contour_area` and at least 5 points (`fitEllipse`'s minimum). The largest surviving contour is used — if more than one object matches, only one is tracked, whichever is bigger.
6. `fitEllipse` on that contour, in ROI-local pixel coordinates. If the fit fails on a degenerate contour (e.g. near-collinear points), the frame is skipped rather than crashing the detection thread.
7. Convert to full-frame coordinates (add the ROI offset back), then to mm using `pixels_per_mm`.
8. Volume is computed treating the droplet as an **oblate spheroid**, symmetric about the shorter (polar) axis: `V = (4/3) * pi * a^2 * b`, where `a` is the semi-major (equatorial) axis and `b` is the semi-minor (polar) axis, both in mm.

If no contour survives the filters, no ellipse is drawn and no measurement is recorded for that frame — the pipeline does not interpolate or hold the last value.

### Absolute vs. gradient thresholding

By default (`use_gradient: false`), step 3 is `THRESH_BINARY_INV` at `threshold` — pixels **darker** than `threshold` become foreground. This requires the droplet to sit at a fairly stable absolute brightness relative to the background.

Setting `use_gradient: true` (or `--use-gradient`) switches step 3 to thresholding the **Sobel gradient magnitude** of the blurred image instead — pixels sitting on a strong edge become foreground, regardless of which side is brighter. This trades one failure mode for another:

- **More robust to**: illumination drift over a beamtime (it reacts to local contrast, not absolute brightness), and specular reflections splitting the blob (there's no interior to fill, so no hole to patch).
- **Less robust to**: any other sharp edge inside the ROI (a transducer edge, a scratch on the cell window) — there's no "pick the biggest blob" equivalent at the edge-detection stage, only at the final contour-area filter.

`threshold` means something completely different in this mode (gradient magnitude, roughly `0`–`1400` for an 8-bit image, instead of `0`–`255` intensity) — **re-tune it from scratch via `/debug_frame`** after switching, don't just flip the flag and expect the same numeric threshold to work.

This was ported from a similar per-column edge-detection approach used at the MID beamline for the same acoustic-levitator droplet-tracking problem, adapted here to plug into the existing contour/`fitEllipse` pipeline rather than MID's direct point-cloud ellipse fit.

## HTTP API

Base URL: `http://<hutch-laptop-ip>:8989`. All responses are JSON unless noted.

| Method | Path | Purpose | Notes |
|---|---|---|---|
| GET | `/preview` | MJPEG stream (`multipart/x-mixed-replace`) with ROI box + ellipse overlay | Open directly in a browser or `<img>` tag |
| POST | `/start` | Clears measurements, starts ffmpeg recording | `400` with `{"status": "already recording"}` if already recording |
| POST | `/stop` | Flushes remaining frames, closes ffmpeg, stops recording | `400` with `{"status": "not recording", "error": ...}` if not recording |
| GET | `/measurements` | Full measurement list for the current/last recording | `[]` before the first `/start` |
| GET | `/measurements/latest` | Most recent measurement | `404` `{"error": "no measurements yet"}` if none |
| GET | `/volume_plot` | Auto-refreshing HTML page embedding `/volume_plot.png` | Refreshes every 1000ms client-side |
| GET | `/volume_plot.png` | Matplotlib PNG of volume vs. time for the current measurement set | Placeholder image if fewer than 2 points |
| GET | `/calibration` | Dumps the running `Config` as JSON | Useful to confirm CLI overrides took effect |
| GET | `/status` | Recording/camera state | See below |
| GET | `/debug_frame` | Binary threshold image with contour overlays | Only registered when the server was started with `--debug`; `503` plain-text if no frame yet |

### `/start` response

```json
{"message": "Recording started", "filename": "recording_20260902_141055.mp4"}
```

### `/stop` response

```json
{
  "message": "Recording stopped",
  "filename": "recording_20260902_141055.mp4",
  "error": null,
  "measurement_count": 842
}
```

`error` is non-null if, e.g., the ffmpeg process died mid-recording (`BrokenPipeError`) — the video file may be incomplete in that case.

### `/measurements` item shape

```json
{"timestamp": 1767356123.412, "cx_mm": 3.21, "cy_mm": 1.08, "volume_mm3": 0.0142}
```

`timestamp` is a Unix epoch float (`time.time()`), not relative to recording start.

### `/status` response

```json
{
  "recording": false,
  "filename": "recording_20260902_141055.mp4",
  "error": null,
  "camera_connected": true,
  "measurement_count": 842
}
```

`camera_macro.py`'s pre-flight check reads `camera_connected` from this response and aborts the whole macro (scan never runs) if it's `false`.

## Sardana macro: `camera_scan`

```
camera_scan <scan_macro> <scan_arg> [<scan_arg> ...]
```

Example:

```
camera_scan ascan mot01 0 10 50 0.1
```

Wraps `scan_macro` (run via `execMacro`) with:

1. **Pre-flight** — `GET /status`; aborts the whole macro (scan never runs) if the server is unreachable.
2. **`POST /start`** — clears server-side measurements and begins recording. Aborts the macro (scan never runs) if this fails.
3. **Runs the scan** — `execMacro([scan_macro] + scan_args)`.
4. **`POST /stop`** — in a `finally` block, so this always runs even if the scan raised.
5. **`GET /measurements`** — fetches the full per-frame time series recorded during the scan.
6. **Writes results**:
   - The video filename is stored in the Sardana environment variable `LastVideoFile` (`self.setEnv(...)`) — it is *not* written into the HDF5 file itself.
   - `timestamp` and `volume_mm3` are written as `side_camera_timestamp` and `side_camera_volume_mm3` via the scan's data handler (`dh.addCustomData(...)`).

> **Known gap:** `cx_mm` / `cy_mm` (the droplet centroid track) are fetched from the server but currently **not** written anywhere — only volume and timestamp survive into the scan record.

### HDF5 output

`addCustomData` is a Sardana `MacroServer` API — where exactly `side_camera_timestamp` and `side_camera_volume_mm3` land inside the HDF5 file depends on which scan recorder your MacroServer is configured with (typically `NXscanH5_FileRecorder`). To find them after a scan:

```bash
h5dump -n <scan_file>.h5
```

or in Python:

```python
import h5py
f = h5py.File("<scan_file>.h5", "r")
f.visit(print)
```

and look for `side_camera_timestamp` / `side_camera_volume_mm3` under the last `entry` group.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/preview` is black / doesn't load | Wrong `camera_index` | Try `--camera-index 1`, `2`, ... |
| Log repeats `Failed to capture frame` | Camera disconnected, or already opened by another process | Check the USB connection; close any other app using the camera |
| No ellipse drawn, ever | `threshold` doesn't separate droplet from background, or ROI is off | Use `--debug` + `/debug_frame` to inspect the binary image; see [SETUP.md](SETUP.md#23-threshold-blur-and-morphological-closing) |
| Ellipse flickers on/off | Droplet contour area crosses `min_contour_area`/`max_contour_area` boundary, or the specular highlight sometimes splits the blob | Widen the area bounds slightly; add/tune `morph_close_size` |
| Ellipse jitters even when droplet is stationary | Blur kernel too small (noisy edge) or too large (edge lag) | Adjust `blur_kernel` |
| `camera_scan` says server unreachable | `CAMERA_URL` in `camera_macro.py` doesn't match the hutch laptop's real address, network path blocked, or server not running | Re-check [SETUP.md §3](SETUP.md#3-control-system-sardana-macro) and §4 |
| `camera_scan` aborts before the scan runs, camera error reported | `/status` reported `camera_connected: false` | Check the camera's USB connection on the hutch laptop, and `camera_server.log` there |
| Scan runs but no `side_camera_*` data in the HDF5 file | `addCustomData` isn't wired to your recorder | Check MacroServer recorder config |
| `POST /start` returns 400 `already recording` | A previous recording was never stopped (e.g. a crashed macro) | `POST /stop` manually, then retry |
| Video file is corrupted / short | ffmpeg process died mid-recording (`error` field non-null in `/stop` response) | Check `camera_server.log` on the hutch laptop for the ffmpeg/encoding error |
| Volume numbers look off by a constant factor | `pixels_per_mm` miscalibrated | Redo the calibration in [SETUP.md §2.1](SETUP.md#21-pixels_per_mm) |

## Logs

`camera_server.py` logs to both stdout and `camera_server.log` (written in the working directory it was launched from). Flask/werkzeug request logs are routed through the same handlers.
