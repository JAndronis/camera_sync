# Quick start

This assumes both halves of the system are already installed and calibrated once — see [SETUP.md](SETUP.md) if not.

## 1. Start the camera server (hutch laptop)

```bash
uv run python src/camera_server.py --config calibration_example.json
```

Swap in your own calibration file once you have one. Leave this running for the whole beamtime — it's independent of any particular scan.

## 2. Check the live preview

Open in a browser, from any machine on the network:

```
http://<hutch-laptop-ip>:8989/preview
```

You should see the camera feed with a green ellipse tracking the droplet. If not, revisit calibration in [SETUP.md](SETUP.md#2-calibration).

## 3. Run a scan with camera recording (Sardana / Spock)

`camera_scan` wraps any existing scan macro — pass the macro name and its normal arguments:

```
Spock> camera_scan ascan mot01 0 10 50 0.1
```

This will:

1. Check the camera server is reachable.
2. Start video recording and clear any previous measurements on the server.
3. Run `ascan mot01 0 10 50 0.1` exactly as it would run on its own.
4. Stop recording (always, even if the scan fails).
5. Fetch the accumulated droplet measurements and attach `side_camera_timestamp` / `side_camera_volume_mm3` arrays to the scan record via `addCustomData`.

The recorded video filename is stored in the `LastVideoFile` Sardana environment variable:

```
Spock> senv LastVideoFile
```

## 4. Watch volume live during the scan

```
http://<hutch-laptop-ip>:8989/volume_plot
```

Auto-refreshes once a second with a plot of droplet volume vs. time since recording started.

## 5. After the scan

- Raw per-frame measurements for the last recording are still available at `http://<hutch-laptop-ip>:8989/measurements` until the next `/start` clears them.
- The video file (`recording_<timestamp>.mp4`) is written to the working directory `camera_server.py` was launched from, on the hutch laptop.
- The timestamp/volume arrays are in the scan's HDF5 file, written via Sardana's custom-data mechanism — see [USAGE.md](USAGE.md#hdf5-output) for how to find them.
- To re-fit a recording offline with different parameters, see [FITTING_API.md](FITTING_API.md).

## Something not working?

See the troubleshooting table in [USAGE.md](USAGE.md#troubleshooting).
