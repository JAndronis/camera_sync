# Setup

This project has two halves that install differently:

| Component | Runs on | Needs |
|---|---|---|
| `src/camera_server.py` | The hutch laptop next to the microscope camera | Python 3.14, `uv`, `ffmpeg`, a USB camera |
| `src/camera_macro.py` | The Sardana MacroServer on the beamline control system | Sardana's own Python environment, `requests`, `numpy` |

Do the hutch laptop setup and the control-system setup independently — they don't share an environment.

## 1. Hutch laptop: camera server

### Prerequisites

- Python 3.14 (pinned in `.python-version`)
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- `ffmpeg` on `PATH` — the server shells out to it to encode recordings as H.264 MP4. Check with:

  ```bash
  ffmpeg -version
  ```

- A camera visible to OpenCV. On Linux, list devices with:

  ```bash
  v4l2-ctl --list-devices
  ```

  Note the index of the microscope camera (usually `0` unless another camera/webcam is also attached). On macOS use `system_profiler SPCameraDataType` or just try index `0`, `1`, ...

### Install

```bash
git clone <repo-url>
cd microscope_online_analysis
uv sync
```

`uv sync` installs everything in `pyproject.toml`, including `sardana` and `pandablocks`. Those two are not imported by `camera_server.py` — only `opencv-python`, `flask`, `numpy`, `matplotlib` are actually used on this side. You can ignore that they installed; there's just one dependency set for the whole repo.

### Sanity check

```bash
uv run python src/camera_server.py --config calibration_example.json --debug
```

Then from a browser (on the laptop, or any machine on the same network) open:

```
http://<hutch-laptop-ip>:8989/preview
```

You should see the live camera feed. If the frame is black or the process logs `Failed to capture frame` repeatedly, the `--camera-index` is probably wrong — try `--camera-index 1`, `2`, etc.

The server listens on all interfaces (`0.0.0.0:8989`); this is not configurable via a flag. If you need a different port, edit the `app.run(...)` call at the bottom of `camera_server.py`.

## 2. Calibration

Everything below feeds into a JSON file like `calibration_example.json`, which you pass via `--config`. Do this once per camera setup (camera position, lens, zoom) — redo it if any of those change.

### 2.1 `pixels_per_mm`

Place an object of known size (a ruler, a reference sphere, calipers on a fixed jig) in the camera's focal plane where the droplet will sit. Take a snapshot from `/preview`, measure the object in pixels (e.g. with any image viewer that shows pixel coordinates, or `/debug_frame` described below), and divide pixel length by the known physical length in mm:

```
pixels_per_mm = measured_length_px / known_length_mm
```

This value directly scales every downstream measurement (`cx_mm`, `cy_mm`, `volume_mm3`), so get it right before a beamtime.

### 2.2 `roi` — region of interest

`roi` is `[x, y, width, height]` in pixels, cropped out of the full frame *before* detection runs. Set it to a box around where the droplet actually moves, excluding the acoustic transducers and cylinder walls — those are common sources of false-positive contours.

To find the coordinates:

1. Run the server with `--debug`.
2. Open `http://<hutch-laptop-ip>:8989/preview` and visually estimate a bounding box around the droplet's range of motion.
3. Set `roi` in the config (or pass `--roi X Y W H`) and restart the server.
4. Reopen `/preview` — a yellow rectangle now shows the active ROI. Adjust until it tightly frames the droplet's travel range and nothing else.

### 2.3 Threshold, blur, and morphological closing

The detector works on a grayscale, blurred, inverted-binary image: pixels *darker* than `threshold` become foreground. Tune this with the `/debug_frame` endpoint (only available when the server is started with `--debug`):

```
http://<hutch-laptop-ip>:8989/debug_frame
```

This shows the binary image the detector actually sees, with all contours in grey, contours passing the area filter in white, and the one `fitEllipse` will use outlined in red. Current parameter values are burned into the top-left corner of the image so a screenshot is self-documenting. Reload the page to get a fresh frame (it does not auto-refresh).

Tune in this order:

1. **`threshold`** — raise or lower until the droplet is a single solid white blob with a clean edge, and the background is black. If the droplet has a bright specular highlight from illumination, it may show up as a black hole inside the white blob.
2. **`morph_close_size`** — if there's a specular hole, set this to a small odd-ish kernel size (e.g. `9`–`15`) to fill it via morphological closing. Leave at `0` if there's no hole — closing can merge nearby noise blobs together.
3. **`blur_kernel`** — Gaussian blur kernel size (must be odd; even values are silently rounded up by 1). Higher values smooth out sensor noise but round off the droplet edge, which biases the fitted ellipse axes. Keep it as small as possible while still suppressing noise contours.
4. **`min_contour_area` / `max_contour_area`** — bounds (in px²) used to reject noise specks and oversized blobs (e.g. a shadow merged with the droplet). Set `min_contour_area` comfortably below the droplet's expected pixel area and `max_contour_area` comfortably above it.

Iterate: change a value in the JSON config (or pass the matching CLI flag), restart the server, reload `/debug_frame`.

### 2.4 Confirm calibration end-to-end

With the server running against your finished config:

- `/preview` shows a clean green ellipse tracking the droplet.
- `/calibration` (`GET`) echoes back the config the server is actually running with — useful to confirm a CLI flag override took effect.
- Start a manual recording (`POST /start`), let it run a few seconds, `POST /stop`, then check `/measurements` for a sane `volume_mm3` (compare against a rough hand calculation for a droplet of the expected size).

## 3. Control system: Sardana macro

`camera_macro.py` needs to be discoverable by the MacroServer as a macro module, and needs `requests` and `numpy` importable in Sardana's own Python environment (not the `uv` environment from step 1 — the macro runs inside Sardana, not via `uv run`).

1. Copy or symlink `src/camera_macro.py` into a directory on the MacroServer's `MacroPath` (check current paths with `Pool`/`MacroServer` config, e.g. via `Astor` or the Sardana config file `MacroServerPath` property).
2. Confirm `python -c "import requests, numpy"` succeeds inside Sardana's environment; `pip install` them there if not.
3. **Edit `CAMERA_URL` at the top of `camera_macro.py`** — it ships as a placeholder:

   ```python
   CAMERA_URL = "http://<hutch-laptop-ip>:8989"
   ```

   Set it to the real hostname or IP of the hutch laptop from step 1.
4. Reload macros in Spock (`relmac camera_macro` or restart the MacroServer, depending on your Sardana setup) and confirm it appears:

   ```
   Spock> camera_scan ?
   ```

## 4. Network

The control system needs to reach the hutch laptop on TCP port `8989`. Confirm before a beamtime:

```bash
curl http://<hutch-laptop-ip>:8989/status
```

run from the control-system machine (or anywhere on the same network as the beamline). If this hangs or is refused, check the hutch laptop's firewall and that both machines are on a network segment that permits this traffic — some beamline network zones intentionally block cross-segment traffic.

## Next

Once both halves are installed and calibrated, see [QUICKSTART.md](QUICKSTART.md) to run an actual scan, and [USAGE.md](USAGE.md) for the full config/API/macro reference.
