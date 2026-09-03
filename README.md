# Camera server and macro for CoSAXS levitator

Camera server and Sardana macro for the CoSAXS acoustic levitator at MAX IV. A microscope camera observes an acoustically-levitated droplet; a server running next to the camera continuously fits an ellipse to the droplet, converts it to a physical volume, and can record video on demand. A Sardana macro starts/stops that recording around a scan and writes the volume time-series into the scan's HDF5 file.

## Documentation

- **[Quick start](docs/QUICKSTART.md)** — get a scan running in a few minutes, assuming setup is already done
- **[Setup](docs/SETUP.md)** — install both halves of the system and calibrate the detector
- **[Usage reference](docs/USAGE.md)** — full config/CLI/HTTP API/macro reference, plus troubleshooting
- **[Accessing the fitting API](docs/FITTING_API.md)** — re-fit a saved recording offline, independent of the live server
- **[CLAUDE.md](CLAUDE.md)** — architecture and internals, for anyone modifying the code using claude code

## How it fits together

```
┌──────────────────────────┐                         ┌──────────────────────────┐
│       Hutch laptop       │   HTTP: /start /stop    │  Sardana control system  │
│     camera_server.py     │◄───/measurements────────│     camera_macro.py      │
│                          │◄───/status /preview─────│   (camera_scan macro)    │
│                          │◄───/video /raw_frames───│                          │
│  USB microscope camera   │                         │                          │
└────────────┬─────────────┘                         └─────────────┬────────────┘
             │ writes                                              │ pulls + writes
             ▼                                                     ▼
          recording_<ts>.mp4                                       scan's HDF5 file (in ScanDir)
          raw_frames_<ts>.h5 (optional)                            side_camera_timestamp / volume_mm3 / file refs
          (hutch laptop, until copied)                             + copies of both files above
```

The camera server runs continuously and independently — it serves a live preview at all times.
Recording, the optional raw-frame backup (`raw_frame_dir`), and the HDF5 write-back only happen
when a scan is wrapped with `camera_scan`; the macro then pulls the video (and raw-frame file, if
saved) off the hutch laptop into `ScanDir` alongside the scan's own HDF5 file. See
[docs/FITTING_API.md](docs/FITTING_API.md) for what's in `raw_frames_<ts>.h5`.

## Repository layout

```
.
├── src/
│   ├── camera_server.py   # Flask server: capture, recording, HTTP API (hutch laptop)
│   ├── ellipse_fitting.py # Fitting pipeline (Config, detect_ellipse, draw_overlay) - no
│   │                      # Flask/camera deps, importable standalone for offline re-fitting
│   ├── stability_metrics.py # Stability metrics from detect_ellipse output (no plotting)
│   └── camera_macro.py    # Sardana macro: camera_scan (control system)
├── calibration_example.json  # Example detector calibration
├── docs/                  # Setup / quick start / usage docs
├── pyproject.toml
└── CLAUDE.md              # Architecture notes
```

## Requirements

- Python 3.14, managed with [`uv`](https://docs.astral.sh/uv/)
- `ffmpeg` on the hutch laptop
- A Sardana MacroServer for the `camera_scan` macro

See [docs/SETUP.md](docs/SETUP.md) for full installation and calibration steps.

## Testing the fitting pipeline

`src/ellipse_fitting.py` has no camera or server dependency, so the detection code can be run
against any video file — not just live camera input — without starting `camera_server.py`. Use
this to check a fit against a specific recording or try out parameter changes before applying
them to a live setup. See [Accessing the fitting API](docs/FITTING_API.md).

## Development

```bash
uv sync
uv run ruff check src/
uv run ruff format src/
```

There is no automated test suite; see [Testing the fitting pipeline](#testing-the-fitting-pipeline) above for exercising the detection code directly.
