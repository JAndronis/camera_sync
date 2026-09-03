# Accessing the fitting API

`src/ellipse_fitting.py` contains the detection pipeline (`Config`, `binarize`,
`find_candidate_contours`, `detect_ellipse`, `draw_overlay`) as plain functions, with no Flask,
threading, or camera dependencies. It can be imported and run independently of
`camera_server.py` — e.g. from a Jupyter notebook — to re-fit a saved recording with different
parameters.

See [USAGE.md](USAGE.md#ellipse-detection-pipeline) for `Config` field meanings and the
detection pipeline steps.

## Installation

Only import is `cv2`, already in `pyproject.toml`.

```bash
uv run jupyter lab
```

```python
import sys
sys.path.insert(0, "path/to/microscope_online_analysis/src")

from ellipse_fitting import Config, detect_ellipse, draw_overlay
```

## API reference

### `Config`

Same dataclass `camera_server.py` builds from `--config`/CLI flags.

```python
import dataclasses, json
from ellipse_fitting import Config

with open("calibration_example.json") as f:
    raw = json.load(f)
field_names = {f.name for f in dataclasses.fields(Config)}
config = Config(**{k: v for k, v in raw.items() if k in field_names})
```

`camera_index`, `raw_frame_dir`, `raw_frame_min_free_mb` are server-only; leave at defaults.

### `detect_ellipse(frame, config) -> (measurement, ellipse)`

`frame`: BGR image, `(H, W, 3)`, `uint8`.

Returns `(None, None)` if no contour passes the area/point-count filter, or `fitEllipse` fails.

- `measurement`: `{"timestamp": time.time(), "cx_mm": float, "cy_mm": float, "volume_mm3": float}`.
  `timestamp` is the wall-clock time of the call, not read from the frame.
- `ellipse`: `cv2.fitEllipse` tuple `((cx, cy), (axis1, axis2), angle)`, in full-frame pixel
  coordinates.

If `config.roi` is set, `detect_ellipse` crops the frame itself — pass the full, uncropped frame.
For frames already cropped to the ROI (e.g. from `raw_frames_*.h5`), set `config.roi = None`;
`cx_mm`/`cy_mm` are then relative to the ROI origin, not the full frame.

### `binarize(frame, config) -> binary_mask`

Grayscale → blur → threshold → optional morphological closing.

```python
from ellipse_fitting import binarize
binary = binarize(frame, config)
```

### `find_candidate_contours(contours, config) -> [contour, ...]`

Filters a `cv2.findContours` result by `min_contour_area`/`max_contour_area` and point count
(>= 5). Used internally by `detect_ellipse`.

### `draw_overlay(frame, ellipse, roi=None) -> annotated_frame`

Draws the ROI box and/or fitted ellipse on a copy of `frame`. Either argument may be `None`.

```python
import cv2
import matplotlib.pyplot as plt
from ellipse_fitting import draw_overlay

annotated = draw_overlay(frame, ellipse, config.roi)
plt.imshow(cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB))
```

## Reading a saved `recording_*.mp4`

Video frames are the full camera frame, not ROI-cropped.

```python
import cv2
from ellipse_fitting import detect_ellipse

cap = cv2.VideoCapture("recording_20260902_141055.mp4")
results = []
while True:
    ret, frame = cap.read()
    if not ret:
        break
    measurement, ellipse = detect_ellipse(frame, config)
    results.append(measurement)
cap.release()
```

The mp4 has no embedded per-frame timestamps. Use the raw-frame HDF5 file for per-frame times.

## Reading a saved `raw_frames_*.h5`

Written per recording when `raw_frame_dir` is configured: `raw_frames_<timestamp>.h5`, alongside
the mp4.

| Item | Shape / type | Meaning |
|---|---|---|
| `frames` (dataset) | `(N, roi_h, roi_w, 3)`, `uint8` | One frame per row, cropped to `roi`, BGR |
| `timestamps` (dataset) | `(N,)`, `float64` | `time.time()` at capture, one per frame |
| `roi` (attr) | `[x, y, w, h]` | ROI these frames were cropped to |
| `pixels_per_mm` (attr) | `float` | Calibration in effect for this recording |
| `channel_order` (attr) | `"BGR"` | |

```python
import dataclasses
import h5py
from ellipse_fitting import detect_ellipse

with h5py.File("raw_frames_20260902_141055.h5", "r") as f:
    pixels_per_mm = float(f.attrs["pixels_per_mm"])
    frames = f["frames"][:]
    timestamps = f["timestamps"][:]

offline_config = dataclasses.replace(config, roi=None, pixels_per_mm=pixels_per_mm)

results = []
for frame, ts in zip(frames, timestamps):
    measurement, ellipse = detect_ellipse(frame, offline_config)
    if measurement is not None:
        measurement["timestamp"] = float(ts)
    results.append(measurement)
```

Timestamps are Unix epoch floats. Compare with `np.diff(timestamps)` rather than the raw printed
array, which truncates sub-second differences at default print precision.

## Comparing against the online fit

The scan's HDF5 file (or `/measurements` after a manual recording) holds the online fit's
`side_camera_volume_mm3` / `timestamp` arrays.

```python
import matplotlib.pyplot as plt

t0 = online_timestamps[0]
plt.plot([t - t0 for t in online_timestamps], online_volumes, label="online")
plt.plot([m["timestamp"] - t0 for m in results if m], [m["volume_mm3"] for m in results if m],
         label="offline")
plt.legend()
plt.xlabel("time (s)")
plt.ylabel("volume (mm^3)")
```
