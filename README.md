# Camera server and macro for CoSAXS levitator

Camera server and Sardana macro for the CoSAXS acoustic levitator at MAX IV. A microscope camera observes an acoustically-levitated droplet; a server running next to the camera continuously fits an ellipse to the droplet, converts it to a physical volume, and can record video on demand. A Sardana macro starts/stops that recording around a scan and writes the volume time-series into the scan's HDF5 file.

## Documentation

- **[Quick start](docs/QUICKSTART.md)** — get a scan running in a few minutes, assuming setup is already done
- **[Setup](docs/SETUP.md)** — install both halves of the system and calibrate the detector
- **[Usage reference](docs/USAGE.md)** — full config/CLI/HTTP API/macro reference, plus troubleshooting
- **[Accessing the fitting API](docs/FITTING_API.md)** — re-fit a saved recording offline, independent of the live server
- **[CLAUDE.md](CLAUDE.md)** — architecture and internals, for anyone modifying the code

## How it fits together

```
┌──────────────────────────┐                        ┌──────────────────────────┐
│       Hutch laptop       │   HTTP: /start /stop   │  Sardana control system  │
│     camera_server.py     │◄───/measurements───────│     camera_macro.py      │
│                          │◄───/status /preview─────│   (camera_scan macro)    │
│  USB microscope camera   │                        │                          │
└────────────┬─────────────┘                        └─────────────┬────────────┘
             │ writes                                              │ writes
             ▼                                                     ▼
    recording_<ts>.mp4                                    scan's HDF5 file
   (on the hutch laptop)                    (side_camera_timestamp / side_camera_volume_mm3)
```

The camera server runs continuously and independently — it serves a live preview at all times. Recording and the HDF5 write-back only happen when a scan is wrapped with `camera_scan`.

## Repository layout

```
.
├── src/
│   ├── camera_server.py   # Flask server: capture, recording, HTTP API (hutch laptop)
│   ├── ellipse_fitting.py # Fitting pipeline (Config, detect_ellipse, draw_overlay) - no
│   │                      # Flask/camera deps, importable standalone for offline re-fitting
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

## Development

```bash
uv sync
uv run ruff check src/
uv run ruff format src/
```

There are no automated tests in this repository.
