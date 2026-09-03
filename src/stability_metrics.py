"""Detrending and stability metrics for droplet/ellipse fitting results.

Operates on a `results` list as produced by looping `detect_ellipse` over a
recording (see docs/FITTING_API.md): one entry per frame, each either a
measurement dict (`{"timestamp", "cx_mm", "cy_mm", "volume_mm3"}`) or `None`
for a frame with no fit. No plotting here - call these from a notebook and
plot the returned arrays/values there.
"""

import numpy as np
from scipy.ndimage import uniform_filter1d


def _extract(results: list[dict | None], field: str) -> tuple[np.ndarray, np.ndarray]:
    """Return (timestamps, values) for `field`, dropping frames with no fit."""
    pairs = [(m["timestamp"], m[field]) for m in results if m is not None]
    if not pairs:
        return np.array([]), np.array([])
    timestamps, values = zip(*pairs)
    return np.asarray(timestamps, dtype=float), np.asarray(values, dtype=float)


def fit_rate(results: list[dict | None]) -> float:
    """Fraction of frames in `results` with a successful ellipse fit."""
    if not results:
        return float("nan")
    return sum(m is not None for m in results) / len(results)


def detrend(
    results: list[dict | None], field: str, window_s: float = 1.0
) -> tuple[np.ndarray, np.ndarray]:
    """Remove a rolling-mean trend from `field` (e.g. `"cx_mm"`, `"cy_mm"`,
    `"volume_mm3"`), isolating fast jitter from slow drift.

    Returns `(timestamps, residual)` for frames with a successful fit, where
    `residual = value - rolling_mean(value)`. The rolling window is
    `window_s` seconds, converted to samples using the median frame interval.
    """
    timestamps, values = _extract(results, field)
    if len(values) < 2:
        return timestamps, values - values.mean() if len(values) else values

    dt = np.median(np.diff(timestamps))
    window = max(1, round(window_s / dt))
    trend = uniform_filter1d(values, size=window, mode="nearest")
    return timestamps, values - trend


def rms_deviation(
    results: list[dict | None], field: str, detrend_window_s: float | None = None
) -> float:
    """RMS deviation of `field` from its mean, or from a rolling trend if
    `detrend_window_s` is given, in the field's native units."""
    if detrend_window_s is not None:
        _, residual = detrend(results, field, window_s=detrend_window_s)
    else:
        _, values = _extract(results, field)
        residual = values - values.mean() if len(values) else values
    return float(np.sqrt(np.mean(residual**2))) if len(residual) else float("nan")


def positional_stability(
    results: list[dict | None], detrend_window_s: float | None = None
) -> float:
    """Combined RMS positional deviation of (`cx_mm`, `cy_mm`), in mm."""
    rms_x = rms_deviation(results, "cx_mm", detrend_window_s)
    rms_y = rms_deviation(results, "cy_mm", detrend_window_s)
    return float(np.sqrt(rms_x**2 + rms_y**2))


def volume_stability(
    results: list[dict | None], detrend_window_s: float | None = None
) -> float:
    """Coefficient of variation of `volume_mm3` (std / mean), dimensionless."""
    _, values = _extract(results, "volume_mm3")
    if len(values) == 0:
        return float("nan")
    mean = values.mean()
    if detrend_window_s is not None:
        _, residual = detrend(results, "volume_mm3", window_s=detrend_window_s)
        std = residual.std()
    else:
        std = values.std()
    return float(std / mean) if mean else float("nan")


def rolling_mean(
    results: list[dict | None], field: str, window_s: float = 1.0
) -> tuple[np.ndarray, np.ndarray]:
    """Local rolling mean of `field`, as a time series - the trend
    `detrend` subtracts and `rolling_rms_deviation` measures deviation
    from, at each frame with a successful fit."""
    timestamps, values = _extract(results, field)
    if len(values) < 2:
        return timestamps, values

    dt = np.median(np.diff(timestamps))
    window = max(1, round(window_s / dt))
    mean = uniform_filter1d(values, size=window, mode="nearest")
    return timestamps, mean


def rolling_rms_deviation(
    results: list[dict | None], field: str, window_s: float = 1.0
) -> tuple[np.ndarray, np.ndarray]:
    """RMS deviation of `field` from its local rolling mean, as a time series
    rather than `rms_deviation`'s single number for the whole recording.

    Returns `(timestamps, values)` for frames with a successful fit. Each
    point is the local RMS deviation within a `window_s`-second window
    centered on that frame.
    """
    timestamps, values = _extract(results, field)
    if len(values) < 2:
        return timestamps, np.zeros_like(values)

    dt = np.median(np.diff(timestamps))
    window = max(1, round(window_s / dt))
    mean = uniform_filter1d(values, size=window, mode="nearest")
    mean_sq = uniform_filter1d(values**2, size=window, mode="nearest")
    variance = np.clip(mean_sq - mean**2, 0, None)
    return timestamps, np.sqrt(variance)


def rolling_positional_stability(
    results: list[dict | None], window_s: float = 1.0
) -> tuple[np.ndarray, np.ndarray]:
    """Combined rolling RMS positional deviation of (`cx_mm`, `cy_mm`), in
    mm, as a time series - see `rolling_rms_deviation`."""
    timestamps, rms_x = rolling_rms_deviation(results, "cx_mm", window_s)
    _, rms_y = rolling_rms_deviation(results, "cy_mm", window_s)
    return timestamps, np.sqrt(rms_x**2 + rms_y**2)


def rolling_volume_stability(
    results: list[dict | None], window_s: float = 1.0
) -> tuple[np.ndarray, np.ndarray]:
    """Rolling coefficient of variation of `volume_mm3` (local std / local
    mean), as a time series - see `rolling_rms_deviation`."""
    timestamps, values = _extract(results, "volume_mm3")
    if len(values) < 2:
        return timestamps, np.zeros_like(values)

    dt = np.median(np.diff(timestamps))
    window = max(1, round(window_s / dt))
    mean = uniform_filter1d(values, size=window, mode="nearest")
    mean_sq = uniform_filter1d(values**2, size=window, mode="nearest")
    variance = np.clip(mean_sq - mean**2, 0, None)
    std = np.sqrt(variance)
    with np.errstate(divide="ignore", invalid="ignore"):
        cv = np.where(mean != 0, std / mean, np.nan)
    return timestamps, cv


def stability_summary(
    results: list[dict | None], detrend_window_s: float | None = None
) -> dict:
    """Bundle of the metrics above, for a quick printout or log line."""
    timestamps, _ = _extract(results, "cx_mm")
    duration_s = float(timestamps[-1] - timestamps[0]) if len(timestamps) > 1 else 0.0
    return {
        "n_frames": len(results),
        "fit_rate": fit_rate(results),
        "duration_s": duration_s,
        "positional_rms_mm": positional_stability(results, detrend_window_s),
        "volume_cv": volume_stability(results, detrend_window_s),
    }
