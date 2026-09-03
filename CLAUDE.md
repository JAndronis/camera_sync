# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Camera server and Sardana macro for the CoSAXS acoustic levitator at MAX IV. A microscope camera observes droplets inside the levitator; the server continuously fits an ellipse to the droplet and accumulates measurements. A Sardana macro triggers recording around a scan and writes the ellipse time-series into the scan's HDF5 file.

## Commands

This project uses `uv` for environment and dependency management.

```bash
# Install dependencies
uv sync

# Run the camera server
uv run python src/camera_server.py --config calibration_example.json

# Run with debug endpoint enabled (exposes /debug_frame for threshold tuning)
uv run python src/camera_server.py --config calibration_example.json --debug

# Lint
uv run ruff check src/
uv run ruff format src/

# There are no automated tests
```

Key CLI flags for `camera_server.py`:
- `--config FILE` - JSON calibration file (see `calibration_example.json`)
- `--roi X Y W H` - constrain ellipse detection to a pixel sub-region
- `--threshold INT` - binary threshold (pixels below -> foreground)
- `--morph-close-size INT` - morphological closing kernel to fill specular holes; 0 = off
- `--debug` - gate the `/debug_frame` endpoint (shows binary image + contour overlays)

## Architecture

### Two-process design

`src/camera_server.py` runs on the **hutch laptop** next to the camera. `src/camera_macro.py` runs inside **Sardana** on the beamline control machine and talks to the server over HTTP (default port 8989).

### Threading model in `camera_server.py`

Three daemon threads run concurrently:

| Thread | Function | Shared state written |
|---|---|---|
| Capture | `capture_loop` | `latest_frame`, `encode_queue` |
| Encoding | `encoding_loop` | writes raw BGR frames to ffmpeg stdin |
| Detection | `ellipse_detection_loop` | `latest_annotated_frame`, `measurements` |

All shared mutable state is protected by explicit `threading.Lock` objects (`frame_lock`, `annotated_frame_lock`, `measurements_lock`).

### Ellipse detection pipeline (`src/ellipse_fitting.py`)

`Config`, `binarize`, `find_candidate_contours`, `detect_ellipse`, and `draw_overlay` live in
`src/ellipse_fitting.py`, not `camera_server.py`, with no Flask/threading/camera dependencies —
importable standalone to re-fit a saved recording offline.
`camera_server.py`'s `ellipse_detection_loop` thread calls `detect_ellipse(frame, config)`.
See [docs/FITTING_API.md](docs/FITTING_API.md) for the API reference.

`detect_ellipse`:
1. Crop to ROI (if configured)
2. Grayscale -> Gaussian blur -> `THRESH_BINARY_INV` (droplet darker than background)
3. Optional morphological closing to fill specular reflection holes
4. `findContours` -> filter by area bounds -> `fitEllipse` on the largest candidate
5. Convert pixel axes to mm using `pixels_per_mm`; compute oblate spheroid volume: `V = (4/3)*pi*a^2*b`
6. Return measurement dict + ellipse in full-frame coordinates (ROI offset added back)

### HTTP API (Flask, port 8989)

| Endpoint | Method | Purpose |
|---|---|---|
| `/preview` | GET | MJPEG stream with ellipse overlay |
| `/start` | POST | Start recording + clear measurements |
| `/stop` | POST | Flush queue, close ffmpeg, return stats |
| `/measurements` | GET | Full measurement list (JSON array) |
| `/measurements/latest` | GET | Most recent measurement |
| `/volume_plot` | GET | Auto-refreshing HTML page |
| `/volume_plot.png` | GET | Matplotlib PNG of volume vs time |
| `/calibration` | GET | Current `Config` as JSON |
| `/status` | GET | Recording state + camera open flag |
| `/debug_frame` | GET | Binary threshold image (only with `--debug`) |

### Sardana macro (`camera_macro.py`)

`camera_scan` wraps any Sardana scan macro:
1. Pre-flight: `GET /status`
2. `POST /start` -> clears server-side measurements, begins video recording
3. `execMacro(scan_macro, scan_args)` - actual scan
4. `POST /stop` (in `finally` block, always runs)
5. `GET /measurements` -> fetch ellipse time-series
6. Writes video filename + ellipse arrays to the last `entry` group in the scan's HDF5 file under `custom_data/ellipse_tracking/`

### Configuration (`Config` dataclass, defined in `src/ellipse_fitting.py`)

Config is populated from a JSON file (`--config`) first, then overridden by CLI flags. The `calibration_example.json` shows a real working configuration. The `roi` field (`[x, y, width, height]` in pixels) is strongly recommended to exclude the transducers from the detection area.
