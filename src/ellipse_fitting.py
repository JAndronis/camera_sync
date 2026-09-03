"""Droplet ellipse fitting: config, detection pipeline, and overlay drawing.

No Flask/threading/camera dependencies - only cv2/numpy. Safe to import from
a Jupyter notebook for offline re-fitting of a saved `recording_*.mp4` and/or
`raw_frames_*.h5` file, independent of the live camera_server.py process.
"""

import dataclasses
import math
import time
from collections.abc import Sequence

import cv2
import cv2.typing


@dataclasses.dataclass
class Config:
    camera_index: int = 0
    pixels_per_mm: float = 1.0
    # Ellipse detection params
    blur_kernel: int = 5  # Gaussian blur kernel size (must be odd)
    threshold: int = 127  # binary threshold (0-255); pixels below -> foreground
    min_contour_area: float = 200.0  # px^2
    max_contour_area: float = 100000.0  # px^2
    # Morphological closing kernel size (px). Fills bright specular reflections that
    # would otherwise leave a hole in the binary droplet blob. 0 = disabled.
    morph_close_size: int = 0
    # Optional crop applied before detection; set to [x, y, width, height] in pixels.
    # Strongly recommended: keeps the cylinder / transducers out of the search area.
    roi: list | None = dataclasses.field(default=None)
    # Threshold Sobel gradient magnitude instead of absolute intensity. More robust
    # to illumination drift and specular holes, more sensitive to other edges inside
    # the ROI. Uses the same `threshold` field, but on a very different scale -
    # re-tune via /debug_frame after switching.
    use_gradient: bool = False
    # Local scratch directory for per-recording raw-frame HDF5 backups (cropped to
    # `roi`, uncompressed). Feature is disabled unless both this and `roi` are set.
    raw_frame_dir: str | None = None
    # /start refuses to enable raw-frame saving for that recording if free space in
    # raw_frame_dir is below this threshold; the mp4 recording still proceeds.
    raw_frame_min_free_mb: float = 1024.0


def binarize(frame: cv2.typing.MatLike, config: Config) -> cv2.typing.MatLike:
    """Grayscale, blur, and threshold a frame into a binary foreground mask.

    Uses absolute intensity by default (THRESH_BINARY_INV: pixels below
    `threshold` become foreground). If config.use_gradient is set, thresholds
    Sobel gradient magnitude instead - more robust to illumination drift and
    to specular reflections splitting the blob, but more sensitive to any
    other sharp edge inside the ROI.
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    k = config.blur_kernel | 1  # ensure odd
    blurred = cv2.GaussianBlur(gray, (k, k), 0)

    if config.use_gradient:
        gx = cv2.Sobel(blurred, cv2.CV_32F, 1, 0)
        gy = cv2.Sobel(blurred, cv2.CV_32F, 0, 1)
        gradient_mag = cv2.magnitude(gx, gy)
        _, binary = cv2.threshold(
            gradient_mag, config.threshold, 255, cv2.THRESH_BINARY
        )
        binary = binary.astype("uint8")
    else:
        _, binary = cv2.threshold(blurred, config.threshold, 255, cv2.THRESH_BINARY_INV)

    if config.morph_close_size > 0:
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (config.morph_close_size, config.morph_close_size)
        )
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    return binary


def find_candidate_contours(
    contours: Sequence[cv2.typing.MatLike], config: Config
) -> list[cv2.typing.MatLike]:
    """Filter contours to those within the configured area bounds and with
    enough points for fitEllipse (>= 5)."""
    return [
        c
        for c in contours
        if config.min_contour_area <= cv2.contourArea(c) <= config.max_contour_area
        and len(c) >= 5
    ]


def detect_ellipse(
    frame: cv2.typing.MatLike, config: Config
) -> tuple[dict, tuple] | tuple[None, None]:
    """Fit an ellipse to the largest foreground contour within the configured ROI.

    Returns (measurement_dict, cv2_ellipse_in_full_frame) or (None, None).
    """
    roi_offset_x = roi_offset_y = 0
    if config.roi is not None:
        rx, ry, rw, rh = config.roi
        frame = frame[ry : ry + rh, rx : rx + rw]
        roi_offset_x, roi_offset_y = rx, ry

    binary = binarize(frame, config)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates = find_candidate_contours(contours, config)
    if not candidates:
        return None, None

    contour = max(candidates, key=cv2.contourArea)
    try:
        ellipse = cv2.fitEllipse(contour)
    except cv2.error:
        # Degenerate contour (e.g. near-collinear points) - skip this frame
        # rather than letting the exception kill the detection thread.
        return None, None
    (cx_px, cy_px), axes_px, angle = ellipse

    # Shift ellipse centre back to full-frame coordinates for drawing
    cx_full = cx_px + roi_offset_x
    cy_full = cy_px + roi_offset_y
    ellipse_full_frame = ((cx_full, cy_full), axes_px, angle)

    # Convert to mm; semi-axes (half the full axis lengths)
    axis1_px, axis2_px = axes_px
    a_mm = max(axis1_px, axis2_px) / 2.0 / config.pixels_per_mm  # equatorial semi-axis
    b_mm = min(axis1_px, axis2_px) / 2.0 / config.pixels_per_mm  # polar semi-axis

    # Oblate spheroid (axial symmetry around the polar axis): V = (4/3) * pi * a^2 * b
    volume_mm3 = (4.0 / 3.0) * math.pi * a_mm**2 * b_mm

    measurement = {
        "timestamp": time.time(),
        "cx_mm": cx_full / config.pixels_per_mm,
        "cy_mm": cy_full / config.pixels_per_mm,
        "volume_mm3": volume_mm3,
    }
    return measurement, ellipse_full_frame


def draw_overlay(
    frame: cv2.typing.MatLike, ellipse, roi: list | None = None
) -> cv2.typing.MatLike:
    """Draw the ROI box (orange) and fitted ellipse (green) on a copy of
    `frame`, in full-frame coordinates. Matches the server's live-preview
    and is handy for eyeballing a re-fit result on a saved frame."""
    annotated = frame.copy()
    if roi is not None:
        rx, ry, rw, rh = roi
        cv2.rectangle(annotated, (rx, ry), (rx + rw, ry + rh), (0, 200, 255), 1)
    if ellipse is not None:
        cv2.ellipse(annotated, ellipse, (0, 255, 0), 2)
    return annotated
