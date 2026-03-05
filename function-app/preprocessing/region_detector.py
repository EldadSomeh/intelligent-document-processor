"""OpenCV-based document region detection (zero external dependencies).

Detects different region types in scanned document images using only
classical computer-vision heuristics – no ML models, no GPU, no
training data required.

Region types
────────────
  TEXT       Dense horizontal clusters of dark contours (paragraphs, lines)
  STAMP      Coloured blobs – red, blue, or purple circles/ellipses
  SIGNATURE  Thin, high-curvature connected strokes with low density
  TABLE      Grid structures detected via horizontal/vertical line
             intersections

The primary output is a **protection mask** – a single-channel image
where white (255) marks regions that should *not* be thresholded or
aggressively denoised (stamps, signatures, tables).  The image
processor composites the threshold result with the gentle CLAHE result
using this mask.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)


# ── Region model ─────────────────────────────────────────────────────

class RegionType(str, Enum):
    TEXT = "text"
    STAMP = "stamp"
    SIGNATURE = "signature"
    TABLE = "table"


@dataclass
class Region:
    """A detected region in the document image."""

    type: RegionType
    bbox: Tuple[int, int, int, int]  # (x, y, w, h)
    confidence: float = 0.0  # 0-1 heuristic confidence
    contour: np.ndarray | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        x, y, w, h = self.bbox
        return {
            "type": self.type.value,
            "bbox": {"x": x, "y": y, "w": w, "h": h},
            "confidence": round(self.confidence, 2),
        }


@dataclass
class DetectionResult:
    """Aggregated detection output."""

    regions: List[Region] = field(default_factory=list)
    protection_mask: np.ndarray | None = field(default=None, repr=False)

    @property
    def stamp_count(self) -> int:
        return sum(1 for r in self.regions if r.type == RegionType.STAMP)

    @property
    def signature_count(self) -> int:
        return sum(1 for r in self.regions if r.type == RegionType.SIGNATURE)

    @property
    def table_count(self) -> int:
        return sum(1 for r in self.regions if r.type == RegionType.TABLE)

    @property
    def has_protected_regions(self) -> bool:
        return any(
            r.type in (RegionType.STAMP, RegionType.SIGNATURE, RegionType.TABLE)
            for r in self.regions
        )

    def summary(self) -> dict:
        return {
            "totalRegions": len(self.regions),
            "stamps": self.stamp_count,
            "signatures": self.signature_count,
            "tables": self.table_count,
            "hasProtectedRegions": self.has_protected_regions,
            "regions": [r.to_dict() for r in self.regions],
        }


# ── Detector ─────────────────────────────────────────────────────────

class RegionDetector:
    """Detect stamps, signatures, and tables in a document image.

    All detection is performed on the *original colour* image so that
    colour-based heuristics (stamp detection) work correctly.  The
    protection mask is built at the same resolution as the grayscale
    image used by the preprocessing pipeline.
    """

    # ── Stamp detection parameters ───────────────────────────────────
    # HSV ranges for common stamp colours (red, blue, purple)
    _STAMP_HSV_RANGES: list[tuple[np.ndarray, np.ndarray]] = [
        # Red (wraps around 0/180 in OpenCV HSV)
        (np.array([0, 70, 50]), np.array([10, 255, 255])),
        (np.array([170, 70, 50]), np.array([180, 255, 255])),
        # Blue
        (np.array([100, 70, 50]), np.array([130, 255, 255])),
        # Purple / magenta
        (np.array([130, 50, 50]), np.array([170, 255, 255])),
    ]
    _STAMP_MIN_AREA_RATIO: float = 0.001  # 0.1% of image area
    _STAMP_MAX_AREA_RATIO: float = 0.15   # 15% of image area
    _STAMP_MIN_CIRCULARITY: float = 0.25  # Stamps are roughly round/oval

    # ── Signature detection parameters ───────────────────────────────
    _SIG_MIN_AREA_RATIO: float = 0.002    # At least 0.2% of image
    _SIG_MAX_AREA_RATIO: float = 0.08     # At most 8% of image
    _SIG_MAX_SOLIDITY: float = 0.35       # Signatures are sparse/thin
    _SIG_MIN_ASPECT: float = 1.5          # Wider than tall

    # ── Table detection parameters ───────────────────────────────────
    _TABLE_MIN_INTERSECTIONS: int = 4     # At least 4 line crossings
    _TABLE_MIN_AREA_RATIO: float = 0.02   # At least 2% of image

    def detect(self, color_image: np.ndarray) -> DetectionResult:
        """Run all detectors and build the protection mask.

        Parameters
        ----------
        color_image : np.ndarray
            BGR image as loaded by ``cv2.imread()``.

        Returns
        -------
        DetectionResult
            Contains the list of regions and the protection mask.
        """
        h, w = color_image.shape[:2]
        regions: list[Region] = []
        protection_mask = np.zeros((h, w), dtype=np.uint8)

        # Convert to useful colour spaces
        if len(color_image.shape) == 3 and color_image.shape[2] >= 3:
            hsv = cv2.cvtColor(color_image, cv2.COLOR_BGR2HSV)
            gray = cv2.cvtColor(color_image, cv2.COLOR_BGR2GRAY)
        else:
            hsv = None
            gray = color_image if len(color_image.shape) == 2 else color_image[:, :, 0]

        # Run detectors
        if hsv is not None:
            stamps = self._detect_stamps(hsv, h, w)
            regions.extend(stamps)
            for r in stamps:
                self._draw_region_on_mask(protection_mask, r, padding=15)

        signatures = self._detect_signatures(gray, h, w)
        regions.extend(signatures)
        for r in signatures:
            self._draw_region_on_mask(protection_mask, r, padding=10)

        tables = self._detect_tables(gray, h, w)
        regions.extend(tables)
        for r in tables:
            self._draw_region_on_mask(protection_mask, r, padding=5)

        logger.info(
            "Region detection: %d stamps, %d signatures, %d tables",
            sum(1 for r in regions if r.type == RegionType.STAMP),
            sum(1 for r in regions if r.type == RegionType.SIGNATURE),
            sum(1 for r in regions if r.type == RegionType.TABLE),
        )

        return DetectionResult(regions=regions, protection_mask=protection_mask)

    # ── Stamp detection ──────────────────────────────────────────────

    def _detect_stamps(
        self, hsv: np.ndarray, img_h: int, img_w: int,
    ) -> list[Region]:
        """Detect coloured stamp/seal regions via HSV colour segmentation."""
        total_area = img_h * img_w
        combined_mask = np.zeros((img_h, img_w), dtype=np.uint8)

        for lower, upper in self._STAMP_HSV_RANGES:
            mask = cv2.inRange(hsv, lower, upper)
            combined_mask = cv2.bitwise_or(combined_mask, mask)

        # Morphological close to merge nearby coloured pixels
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
        combined_mask = cv2.morphologyEx(combined_mask, cv2.MORPH_CLOSE, kernel)

        # Dilate a bit to capture edges
        combined_mask = cv2.dilate(
            combined_mask,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
            iterations=1,
        )

        contours, _ = cv2.findContours(
            combined_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
        )

        stamps: list[Region] = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            ratio = area / total_area

            if ratio < self._STAMP_MIN_AREA_RATIO:
                continue
            if ratio > self._STAMP_MAX_AREA_RATIO:
                continue

            # Circularity check: 4π × area / perimeter²
            perimeter = cv2.arcLength(cnt, True)
            if perimeter == 0:
                continue
            circularity = (4 * np.pi * area) / (perimeter * perimeter)

            if circularity < self._STAMP_MIN_CIRCULARITY:
                continue

            x, y, w, h = cv2.boundingRect(cnt)
            confidence = min(1.0, circularity * 1.5)  # Higher circularity → higher confidence

            stamps.append(Region(
                type=RegionType.STAMP,
                bbox=(x, y, w, h),
                confidence=confidence,
                contour=cnt,
            ))
            logger.debug(
                "Stamp detected at (%d,%d %dx%d) circ=%.2f area_ratio=%.4f",
                x, y, w, h, circularity, ratio,
            )

        return stamps

    # ── Signature detection ──────────────────────────────────────────

    def _detect_signatures(
        self, gray: np.ndarray, img_h: int, img_w: int,
    ) -> list[Region]:
        """Detect signature-like regions: thin, curved, sparse strokes.

        Signatures are characterised by:
          - Low solidity (area / convex-hull-area < 0.35) – they're wispy
          - Wider than tall (aspect ratio > 1.5)
          - Located in the lower portion of the page (typically)
          - Medium size (not too small, not too large)
        """
        total_area = img_h * img_w

        # Edge detection to find thin strokes
        blurred = cv2.GaussianBlur(gray, (3, 3), 0)
        edges = cv2.Canny(blurred, 50, 150)

        # Dilate edges to connect nearby strokes
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 5))
        dilated = cv2.dilate(edges, kernel, iterations=2)

        # Close small gaps
        dilated = cv2.morphologyEx(
            dilated,
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_RECT, (20, 10)),
        )

        contours, _ = cv2.findContours(
            dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
        )

        signatures: list[Region] = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            ratio = area / total_area

            if ratio < self._SIG_MIN_AREA_RATIO or ratio > self._SIG_MAX_AREA_RATIO:
                continue

            x, y, w, h = cv2.boundingRect(cnt)

            # Aspect ratio: signatures are wider than tall
            if h == 0:
                continue
            aspect = w / h
            if aspect < self._SIG_MIN_ASPECT:
                continue

            # Solidity: signatures are sparse
            hull = cv2.convexHull(cnt)
            hull_area = cv2.contourArea(hull)
            if hull_area == 0:
                continue
            solidity = area / hull_area

            if solidity > self._SIG_MAX_SOLIDITY:
                continue

            # Position bonus: signatures are often in the lower half
            center_y = y + h / 2
            position_score = center_y / img_h  # Higher = lower on page

            confidence = min(1.0, (1 - solidity) * 0.5 + position_score * 0.3 + 0.2)

            signatures.append(Region(
                type=RegionType.SIGNATURE,
                bbox=(x, y, w, h),
                confidence=confidence,
                contour=cnt,
            ))
            logger.debug(
                "Signature detected at (%d,%d %dx%d) solidity=%.2f aspect=%.1f",
                x, y, w, h, solidity, aspect,
            )

        return signatures

    # ── Table detection ──────────────────────────────────────────────

    def _detect_tables(
        self, gray: np.ndarray, img_h: int, img_w: int,
    ) -> list[Region]:
        """Detect table structures via horizontal/vertical line intersection.

        Uses morphological operations to isolate horizontal and vertical
        lines, then finds their intersections.  Clusters of intersections
        indicate a table grid.
        """
        total_area = img_h * img_w

        # Threshold for line detection
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

        # Horizontal lines
        h_kernel_len = max(img_w // 30, 15)
        h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (h_kernel_len, 1))
        h_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel, iterations=2)

        # Vertical lines
        v_kernel_len = max(img_h // 30, 15)
        v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, v_kernel_len))
        v_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kernel, iterations=2)

        # Intersections
        intersections = cv2.bitwise_and(h_lines, v_lines)

        # Dilate intersections to merge nearby points
        intersections = cv2.dilate(
            intersections,
            cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)),
            iterations=3,
        )

        # Find clusters of intersections
        int_contours, _ = cv2.findContours(
            intersections, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
        )

        if len(int_contours) < self._TABLE_MIN_INTERSECTIONS:
            return []

        # Merge horizontal and vertical lines into a combined mask
        combined_lines = cv2.bitwise_or(h_lines, v_lines)
        combined_lines = cv2.dilate(
            combined_lines,
            cv2.getStructuringElement(cv2.MORPH_RECT, (10, 10)),
            iterations=2,
        )

        # Find table regions from the combined line mask
        table_contours, _ = cv2.findContours(
            combined_lines, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
        )

        tables: list[Region] = []
        for cnt in table_contours:
            x, y, w, h = cv2.boundingRect(cnt)
            area = w * h
            ratio = area / total_area

            if ratio < self._TABLE_MIN_AREA_RATIO:
                continue

            # Count intersections inside this bounding box
            local_ints = 0
            for ic in int_contours:
                ix, iy, iw, ih = cv2.boundingRect(ic)
                cx, cy = ix + iw // 2, iy + ih // 2
                if x <= cx <= x + w and y <= cy <= y + h:
                    local_ints += 1

            if local_ints < self._TABLE_MIN_INTERSECTIONS:
                continue

            confidence = min(1.0, local_ints / 12.0)

            tables.append(Region(
                type=RegionType.TABLE,
                bbox=(x, y, w, h),
                confidence=confidence,
                contour=cnt,
            ))
            logger.debug(
                "Table detected at (%d,%d %dx%d) intersections=%d",
                x, y, w, h, local_ints,
            )

        return tables

    # ── Mask helpers ─────────────────────────────────────────────────

    @staticmethod
    def _draw_region_on_mask(
        mask: np.ndarray,
        region: Region,
        padding: int = 10,
    ) -> None:
        """Draw a region onto the protection mask with optional padding."""
        x, y, w, h = region.bbox
        pad = padding
        x1 = max(0, x - pad)
        y1 = max(0, y - pad)
        x2 = min(mask.shape[1], x + w + pad)
        y2 = min(mask.shape[0], y + h + pad)

        if region.contour is not None:
            # Draw filled contour with padding via dilated contour
            temp = np.zeros_like(mask)
            cv2.drawContours(temp, [region.contour], -1, 255, cv2.FILLED)
            if padding > 0:
                dilate_kernel = cv2.getStructuringElement(
                    cv2.MORPH_ELLIPSE, (padding * 2, padding * 2),
                )
                temp = cv2.dilate(temp, dilate_kernel, iterations=1)
            mask[:] = cv2.bitwise_or(mask, temp)
        else:
            # Fallback: rectangle
            cv2.rectangle(mask, (x1, y1), (x2, y2), 255, cv2.FILLED)
