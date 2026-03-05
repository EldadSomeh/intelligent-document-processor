"""OpenCV-based image preprocessing pipeline for OCR enhancement.

The pipeline is **diagnosis-driven**: it analyses the original image
first and only applies fixes for problems that actually exist.  Clean
images pass through virtually untouched.

Diagnosis thresholds (all configurable via PreprocessOptions)
─────────────────────────────────────────────────────────────
  Dark image:         mean brightness < 140 → gamma + CLAHE + offset
  Noisy image:        noise estimation > 8  → gentle denoise (h=3)
  Low contrast:       pixel stddev < 50     → CLAHE + brightness match
  Skewed image:       angle 0.5°–15°        → rotation correction
  Low DPI:            max dimension < 2400  → 2× upscale
  Border/margin:      auto-crop removes black scanner borders

If none of these trigger, the image is converted to grayscale and
passed through with only size-limit enforcement.

**Region-aware processing** – Before enhancement, the detector scans
the original colour image for stamps, signatures, and tables.  A
protection mask prevents those regions from being binarised or
aggressively denoised, preserving handwriting and coloured elements.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import cv2
import numpy as np

from .region_detector import DetectionResult, RegionDetector

logger = logging.getLogger(__name__)


class ImageProcessor:
    """Configurable preprocessing pipeline.

    All parameters can be overridden via the ``options`` dict passed
    from the UI / Logic App.  The constructor accepts an ``options``
    object (``PreprocessOptions``) that exposes every knob.
    """

    # Doc Intelligence limit is 4 MB; keep a margin
    MAX_FILE_BYTES: int = 3_500_000          # 3.5 MB safety threshold
    MAX_DIMENSION: int = 4000                # px – cap any side
    SHRINK_STEP: float = 0.85                # scale factor per iteration
    SHRINK_MAX_ITERS: int = 5                # avoid infinite loops

    def __init__(
        self,
        aggressive: bool = False,
        *,
        denoise_h: int | None = None,
        clahe_clip: float | None = None,
        force_threshold: bool | None = None,
        force_upscale: bool | None = None,
        enable_autocrop: bool = True,
        autocrop_margin: int = 10,
        dark_threshold: float = 140.0,
        noise_threshold: float = 8.0,
        contrast_threshold: float = 50.0,
        skew_min_angle: float = 0.5,
        skew_max_angle: float = 15.0,
        upscale_below: int = 2400,
        enable_denoise: bool = True,
        enable_deskew: bool = True,
        enable_upscale: bool = True,
        enable_brightness_fix: bool = True,
        enable_contrast_fix: bool = True,
    ) -> None:
        self.aggressive = aggressive

        # Per-step knobs (caller override > aggressive preset > gentle default)
        self._denoise_h = denoise_h if denoise_h is not None else (10 if aggressive else 3)
        self._clahe_clip = clahe_clip if clahe_clip is not None else (2.5 if aggressive else 1.5)
        self._force_threshold = force_threshold if force_threshold is not None else aggressive
        self._force_upscale = force_upscale if force_upscale is not None else aggressive

        # Auto-crop toggle & margin
        self._enable_autocrop = enable_autocrop
        self._autocrop_margin = autocrop_margin

        # Diagnosis thresholds
        self._dark_threshold = dark_threshold
        self._noise_threshold = noise_threshold
        self._contrast_threshold = contrast_threshold
        self._skew_min_angle = skew_min_angle
        self._skew_max_angle = skew_max_angle
        self._upscale_below = upscale_below

        # Step toggles
        self._enable_denoise = enable_denoise
        self._enable_deskew = enable_deskew
        self._enable_upscale = enable_upscale
        self._enable_brightness_fix = enable_brightness_fix
        self._enable_contrast_fix = enable_contrast_fix

        # ── Sharpening & cleanup knobs ────────────────────────────
        self._sharpen_amount = 0.7 if not aggressive else 0.9  # unsharp mask weight
        self._sharpen_radius = 1.2  # Gaussian blur sigma for unsharp mask
        self._enable_sharpen = True  # always sharpen for OCR quality
        self._enable_final_sharpen = True  # final sharpening before output
        self._enable_morph_cleanup = True  # morphological noise speck removal

        self._region_detector = RegionDetector()
        self._last_detection: Optional[DetectionResult] = None

    # ── Public entry point ───────────────────────────────────────────

    def process(
        self,
        input_path: str,
        output_path: str,
        precomputed_detection: Optional[DetectionResult] = None,
    ) -> dict:
        """Run a diagnosis-driven preprocessing pipeline.

        Returns a dict describing every enhancement that was applied,
        including before/after dimensions, diagnosis results, and which
        steps actually fired.

        If *precomputed_detection* is supplied (a ``DetectionResult``
        from a previous ``RegionDetector.detect()`` call on the same
        image), region detection is skipped and the cached result is
        reused.  This avoids redundant work when the auto-tuner tries
        multiple presets on the same page.
        """
        img = cv2.imread(input_path)
        if img is None:
            raise ValueError(f"Cannot decode image file: {input_path}")

        original_h, original_w = img.shape[:2]
        steps_applied: list[str] = []
        report: dict = {
            "originalDimensions": {"w": original_w, "h": original_h},
            "stepsApplied": steps_applied,
            "aggressive": self.aggressive,
        }

        # ── Region detection (on original colour image) ──────────
        if precomputed_detection is not None:
            self._last_detection = precomputed_detection
        else:
            self._last_detection = self._region_detector.detect(img)
        protection_mask = self._last_detection.protection_mask

        gray = self._to_grayscale(img)
        steps_applied.append("grayscale")

        # ── Auto-crop (remove scanner borders) ───────────────────
        pre_crop_h, pre_crop_w = gray.shape[:2]
        if self._enable_autocrop:
            gray = self._auto_crop(gray)
        post_crop_h, post_crop_w = gray.shape[:2]

        if (pre_crop_w, pre_crop_h) != (post_crop_w, post_crop_h):
            steps_applied.append("auto_crop")
            report["autoCrop"] = {
                "before": {"w": pre_crop_w, "h": pre_crop_h},
                "after": {"w": post_crop_w, "h": post_crop_h},
                "removedPct": round((1 - (post_crop_w * post_crop_h) / (pre_crop_w * pre_crop_h)) * 100, 1),
            }

        # ── Diagnose the image ───────────────────────────────────
        diagnosis = self._diagnose(gray)
        report["diagnosis"] = {
            "isDark": diagnosis["is_dark"],
            "isNoisy": diagnosis["is_noisy"],
            "isLowContrast": diagnosis["is_low_contrast"],
            "isSkewed": diagnosis["is_skewed"],
            "isLowDpi": diagnosis["is_low_dpi"],
            "meanBrightness": round(diagnosis["mean_brightness"], 1),
            "noiseEstimate": round(diagnosis["noise_est"], 1),
            "contrast": round(diagnosis["contrast"], 1),
            "skewAngle": round(diagnosis["skew_angle"], 2),
        }
        logger.info(
            "Diagnosis for %s: dark=%s, noisy=%s, low_contrast=%s, skewed=%s, low_dpi=%s, "
            "mean_brightness=%.0f, noise_est=%.1f, contrast=%.1f, skew=%.2f°",
            input_path,
            diagnosis["is_dark"], diagnosis["is_noisy"],
            diagnosis["is_low_contrast"], diagnosis["is_skewed"],
            diagnosis["is_low_dpi"],
            diagnosis["mean_brightness"], diagnosis["noise_est"],
            diagnosis["contrast"], diagnosis["skew_angle"],
        )

        result = gray

        # ── Fix only what's broken ───────────────────────────────

        # 1. Denoise – only if noisy (edge-preserving bilateral filter)
        denoised = False
        if diagnosis["is_noisy"] or self.aggressive:
            result = self._smart_denoise(result)
            steps_applied.append("denoise")
            denoised = True

        # 1b. Sharpening pass – always applied when enabled.
        #     For denoised images this compensates for smoothing;
        #     for clean images it further crisps text edges for OCR.
        if self._enable_sharpen:
            result = self._unsharp_mask(result)
            steps_applied.append("sharpen")

        # 2. Dark image → strong brightness boost
        if diagnosis["is_dark"]:
            result = self._fix_dark_image(result, diagnosis["mean_brightness"])
            steps_applied.append("brightness_fix")
        elif diagnosis["is_low_contrast"]:
            # 3. Low contrast (but not dark) → gentle CLAHE
            result = self._clahe(result)
            result = self._match_brightness(result, gray)
            steps_applied.append("contrast_fix")

        # 3b. Morphological noise cleanup – remove small noise specks
        #     while preserving text strokes. Uses opening (erosion + dilation)
        #     with a tiny kernel that only affects isolated pixels.
        if self._enable_morph_cleanup:
            result = self._morph_cleanup(result)
            steps_applied.append("morph_cleanup")

        # 4. Threshold – only for extremely washed-out scans
        result = self._auto_threshold_with_mask(result, protection_mask)

        # 5. Deskew – only if skewed
        if diagnosis["is_skewed"] or self.aggressive:
            result = self._deskew(result)
            steps_applied.append("deskew")

        # 6. Upscale – only if low DPI
        if diagnosis["is_low_dpi"] or self.aggressive:
            result = self._conditional_upscale(result)
            steps_applied.append("upscale")

        # 6b. Final light sharpening – ensure text edges are maximally
        #     crisp for OCR.  Applied after upscale because upscaling
        #     with INTER_CUBIC can introduce slight softness.
        if self._enable_final_sharpen:
            result = self._final_sharpen(result)
            steps_applied.append("final_sharpen")

        # 7. Dimension cap (always – safety)
        pre_cap_h, pre_cap_w = result.shape[:2]
        result = self._cap_dimensions(result)
        post_cap_h, post_cap_w = result.shape[:2]
        if (pre_cap_w, pre_cap_h) != (post_cap_w, post_cap_h):
            steps_applied.append("dimension_cap")

        cv2.imwrite(output_path, result, [cv2.IMWRITE_PNG_COMPRESSION, 6])

        # 8. File-size safety net
        self._shrink_if_too_large(output_path)

        final_h, final_w = result.shape[:2]
        report["enhancedDimensions"] = {"w": final_w, "h": final_h}
        report["fileSizeKB"] = round(os.path.getsize(output_path) / 1024, 1)

        logger.info(
            "Processed %s → %s  (shape=%s, filesize=%.1fKB, aggressive=%s, protected=%s, steps=%s)",
            input_path, output_path, result.shape,
            os.path.getsize(output_path) / 1024,
            self.aggressive,
            self._last_detection.has_protected_regions,
            steps_applied,
        )

        return report

    def ensure_size_limit(self, image_path: str) -> None:
        """Apply dimension cap + file-size safety net to an existing image.

        This is used for skip-if-clean images that bypass the full
        pipeline but still need to comply with Doc Intelligence limits.
        """
        img = cv2.imread(image_path, cv2.IMREAD_UNCHANGED)
        if img is None:
            return

        h, w = img.shape[:2]
        max_dim = max(h, w)

        if max_dim > self.MAX_DIMENSION:
            scale = self.MAX_DIMENSION / max_dim
            new_w = int(w * scale)
            new_h = int(h * scale)
            img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
            cv2.imwrite(image_path, img, [cv2.IMWRITE_PNG_COMPRESSION, 6])
            logger.info(
                "Size-limit (skipped page): capped %dx%d → %dx%d",
                w, h, new_w, new_h,
            )

        self._shrink_if_too_large(image_path)

    @property
    def last_detection(self) -> Optional[DetectionResult]:
        """Return the detection result from the most recent ``process()`` call."""
        return self._last_detection

    # ── Step 0: Auto-crop (border removal) ───────────────────────────

    def _auto_crop(self, gray: np.ndarray) -> np.ndarray:
        """Detect and remove scanner borders, excess margins, and shadow edges.

        Uses a three-pass approach:
          Pass 1 – Dark border removal: threshold at 40 to remove black scanner borders
          Pass 2 – White margin trimmer: scan from each edge inward, find where
                   content starts by checking row/column mean intensity
          Pass 3 – Edge-based content detection (fallback): Canny + morphology

        Safety checks:
          • Only crops if it would remove ≥ 1% of area on any side
          • Never crops to less than 10% of the original area (handles docs
            with small content on large pages)
          • Adds a configurable margin around detected content
        """
        h, w = gray.shape[:2]
        margin = self._autocrop_margin

        # ── Pass 1: Remove dark borders (near-black, threshold=40) ───
        _, dark_binary = cv2.threshold(gray, 40, 255, cv2.THRESH_BINARY)
        dark_contours, _ = cv2.findContours(
            dark_binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
        )

        result = gray
        if dark_contours:
            all_pts = np.vstack(dark_contours)
            x, y, cw, ch = cv2.boundingRect(all_pts)
            x1 = max(0, x - margin)
            y1 = max(0, y - margin)
            x2 = min(w, x + cw + margin)
            y2 = min(h, y + ch + margin)

            left_pct = x1 / w * 100
            top_pct = y1 / h * 100
            right_pct = (w - x2) / w * 100
            bottom_pct = (h - y2) / h * 100

            crop_area = (x2 - x1) * (y2 - y1)
            if (max(left_pct, top_pct, right_pct, bottom_pct) >= 1.0
                    and crop_area >= 0.10 * h * w):
                result = gray[y1:y2, x1:x2]
                logger.info(
                    "Auto-crop pass 1 (dark borders): %dx%d → %dx%d "
                    "(removed L=%.1f%%, T=%.1f%%, R=%.1f%%, B=%.1f%%)",
                    w, h, x2 - x1, y2 - y1,
                    left_pct, top_pct, right_pct, bottom_pct,
                )

        # ── Pass 2: White margin trimmer ─────────────────────────────
        # Scan from each edge inward.  A row/column is "empty" when its
        # mean intensity is above the white threshold AND its std-dev
        # is low (uniform colour, not text at the edge).
        h2, w2 = result.shape[:2]
        white_mean_thresh = 245    # row/col mean above this → "empty"
        white_std_thresh = 20      # row/col std below this  → "uniform"

        # Top: consecutive white rows from top
        top_trim = 0
        for r in range(h2):
            row = result[r, :].astype(float)
            if row.mean() > white_mean_thresh and row.std() < white_std_thresh:
                top_trim += 1
            else:
                break

        # Bottom: consecutive white rows from bottom
        bot_trim = 0
        for r in range(h2 - 1, -1, -1):
            row = result[r, :].astype(float)
            if row.mean() > white_mean_thresh and row.std() < white_std_thresh:
                bot_trim += 1
            else:
                break

        # Left: consecutive white columns from left
        lft_trim = 0
        for c in range(w2):
            col = result[:, c].astype(float)
            if col.mean() > white_mean_thresh and col.std() < white_std_thresh:
                lft_trim += 1
            else:
                break

        # Right: consecutive white columns from right
        rgt_trim = 0
        for c in range(w2 - 1, -1, -1):
            col = result[:, c].astype(float)
            if col.mean() > white_mean_thresh and col.std() < white_std_thresh:
                rgt_trim += 1
            else:
                break

        # Apply white-margin crop if any side has significant margin (≥ 1%)
        top_pct2 = top_trim / h2 * 100
        bot_pct2 = bot_trim / h2 * 100
        lft_pct2 = lft_trim / w2 * 100
        rgt_pct2 = rgt_trim / w2 * 100

        if max(top_pct2, bot_pct2, lft_pct2, rgt_pct2) >= 1.0:
            # Apply margins with safety buffer
            y1_w = max(0, top_trim - margin)
            y2_w = min(h2, h2 - bot_trim + margin)
            x1_w = max(0, lft_trim - margin)
            x2_w = min(w2, w2 - rgt_trim + margin)

            # Safety: ensure content area is at least 10% of original
            new_area = (x2_w - x1_w) * (y2_w - y1_w)
            if new_area >= 0.10 * h2 * w2 and (x2_w - x1_w) > 50 and (y2_w - y1_w) > 50:
                result = result[y1_w:y2_w, x1_w:x2_w]
                logger.info(
                    "Auto-crop pass 2 (white margins): %dx%d → %dx%d "
                    "(removed L=%.1f%%, T=%.1f%%, R=%.1f%%, B=%.1f%%)",
                    w2, h2, x2_w - x1_w, y2_w - y1_w,
                    lft_pct2, top_pct2, rgt_pct2, bot_pct2,
                )
            else:
                logger.debug(
                    "Auto-crop pass 2 SKIPPED – content area too small "
                    "(%.1f%% of image, need ≥10%%)",
                    new_area / (h2 * w2) * 100,
                )
        else:
            logger.debug(
                "Auto-crop pass 2 SKIPPED – no significant white margins "
                "(L=%.1f%%, T=%.1f%%, R=%.1f%%, B=%.1f%%)",
                lft_pct2, top_pct2, rgt_pct2, bot_pct2,
            )

        # ── Pass 3: Edge-based content detection (fallback) ─────────
        # Only runs if pass 2 didn't trim much. Catches gray borders,
        # scanner shadows, and non-uniform margins.
        h3, w3 = result.shape[:2]

        # Skip if pass 2 already trimmed significantly
        pass2_trimmed = (h3 * w3) < (h2 * w2 * 0.90)
        if pass2_trimmed:
            return result

        blurred = cv2.GaussianBlur(result, (5, 5), 0)
        edges = cv2.Canny(blurred, 30, 100)

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (20, 20))
        dilated = cv2.dilate(edges, kernel, iterations=3)

        contours3, _ = cv2.findContours(
            dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
        )

        if not contours3:
            return result

        min_area = 0.005 * h3 * w3
        big_contours = [c for c in contours3 if cv2.contourArea(c) >= min_area]

        if not big_contours:
            return result

        all_pts3 = np.vstack(big_contours)
        x, y, cw, ch = cv2.boundingRect(all_pts3)

        edge_margin = max(margin, 15)
        x1 = max(0, x - edge_margin)
        y1 = max(0, y - edge_margin)
        x2 = min(w3, x + cw + edge_margin)
        y2 = min(h3, y + ch + edge_margin)

        left_pct = x1 / w3 * 100
        top_pct = y1 / h3 * 100
        right_pct = (w3 - x2) / w3 * 100
        bottom_pct = (h3 - y2) / h3 * 100

        if max(left_pct, top_pct, right_pct, bottom_pct) < 1.0:
            return result

        crop_area = (x2 - x1) * (y2 - y1)
        if crop_area < 0.10 * h3 * w3:
            logger.warning(
                "Auto-crop pass 3 SKIPPED – would remove > 90%% of image "
                "(crop=%dx%d, original=%dx%d)",
                x2 - x1, y2 - y1, w3, h3,
            )
            return result

        cropped = result[y1:y2, x1:x2]
        logger.info(
            "Auto-crop pass 3 (edge-based): %dx%d → %dx%d "
            "(removed L=%.1f%%, T=%.1f%%, R=%.1f%%, B=%.1f%%)",
            w3, h3, x2 - x1, y2 - y1,
            left_pct, top_pct, right_pct, bottom_pct,
        )
        return cropped

    # ── Step 1: Grayscale ────────────────────────────────────────────

    @staticmethod
    def _to_grayscale(img: np.ndarray) -> np.ndarray:
        if len(img.shape) == 3 and img.shape[2] >= 3:
            return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return img

    # ── Diagnosis ────────────────────────────────────────────────────

    def _diagnose(self, gray: np.ndarray) -> dict:
        """Analyse the grayscale image and return a diagnosis dict.

        This drives the entire pipeline: each step only runs when
        the diagnosis says the image has that specific problem.

        Thresholds (tuned for medical document scans):
          - Dark:         mean brightness < 140
          - Noisy:        noise estimation > 8.0
          - Low contrast: pixel stddev < 50
          - Skewed:       detected angle between 0.5° and 15°
          - Low DPI:      max dimension < 2400 px
        """
        mean_brightness = float(np.mean(gray))
        contrast = float(np.std(gray))

        # Noise estimation
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        noise_est = float(np.std(gray.astype(float) - blurred.astype(float)))

        # Skew detection
        skew_angle = self._detect_skew_angle(gray)

        # DPI check
        h, w = gray.shape[:2]
        max_dim = max(h, w)

        return {
            "mean_brightness": mean_brightness,
            "contrast": contrast,
            "noise_est": noise_est,
            "skew_angle": skew_angle,
            "max_dim": max_dim,
            "is_dark": mean_brightness < self._dark_threshold,
            "is_noisy": noise_est > self._noise_threshold,
            "is_low_contrast": contrast < self._contrast_threshold,
            "is_skewed": self._skew_min_angle < abs(skew_angle) < self._skew_max_angle,
            "is_low_dpi": max_dim < self._upscale_below,
        }

    @staticmethod
    def _detect_skew_angle(gray: np.ndarray) -> float:
        """Return the dominant skew angle in degrees (0 if undetectable).

        Uses Hough line transform to find near-horizontal text lines,
        then takes the median angle.  This is far more robust than
        ``minAreaRect`` on all dark pixels, which can be thrown off by
        stamps, diagonal elements, or tables.

        Returns 0.0 when fewer than 5 lines are found or the detected
        angles are inconsistent (high spread), which prevents false
        rotations on clean pages.
        """
        # Edge detection → find lines
        edges = cv2.Canny(gray, 50, 150, apertureSize=3)
        lines = cv2.HoughLinesP(
            edges, 1, np.pi / 180,
            threshold=100,
            minLineLength=gray.shape[1] // 8,   # at least 1/8 page width
            maxLineGap=20,
        )

        if lines is None or len(lines) < 5:
            return 0.0

        # Compute angle of each line segment (degrees from horizontal)
        angles = []
        for line in lines:
            x1, y1, x2, y2 = line[0]
            dx = x2 - x1
            dy = y2 - y1
            if abs(dx) < 1:
                continue  # skip vertical lines
            angle_deg = np.degrees(np.arctan2(dy, dx))
            # Only consider near-horizontal lines (within ±20°)
            if abs(angle_deg) <= 20:
                angles.append(angle_deg)

        if len(angles) < 5:
            return 0.0

        # Use median for robustness against outliers
        median_angle = float(np.median(angles))

        # Consistency check: if angles are too spread out, the
        # detection is unreliable → don't deskew.
        iqr = float(np.percentile(angles, 75) - np.percentile(angles, 25))
        if iqr > 2.0:
            logger.debug(
                "Skew detection REJECTED: IQR=%.2f° too high "
                "(median=%.2f°, %d lines)",
                iqr, median_angle, len(angles),
            )
            return 0.0

        logger.debug(
            "Skew detection: median=%.2f°, IQR=%.2f°, %d lines",
            median_angle, iqr, len(angles),
        )
        return median_angle

    # ── Fix: Dark image brightening ──────────────────────────────────

    @staticmethod
    def _fix_dark_image(img: np.ndarray, mean_brightness: float) -> np.ndarray:
        """Aggressively brighten a dark image.

        Uses a combination of:
          1. Gamma correction (gamma < 1 brightens dark pixels
             disproportionately, which is ideal for dark scans)
          2. CLAHE for local contrast
          3. Final brightness offset if still too dark

        The darker the image, the stronger the correction.
        """
        # Gamma: darker images get smaller gamma (stronger brightening)
        # mean=50 → gamma≈0.4,  mean=100 → gamma≈0.6,  mean=130 → gamma≈0.75
        gamma = max(0.3, min(0.85, mean_brightness / 180.0))

        # Build lookup table for gamma correction
        inv_gamma = 1.0 / gamma
        lut = np.array([
            np.clip(pow(i / 255.0, inv_gamma) * 255.0, 0, 255)
            for i in range(256)
        ], dtype=np.uint8)
        brightened = cv2.LUT(img, lut)

        # Apply gentle CLAHE to boost local contrast after brightening
        clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8))
        brightened = clahe.apply(brightened)

        # Final check: if still darker than target (170), add offset
        current_mean = float(np.mean(brightened))
        target_mean = 170.0
        if current_mean < target_mean:
            delta = target_mean - current_mean
            brightened = np.clip(
                brightened.astype(np.float32) + delta, 0, 255,
            ).astype(np.uint8)

        new_mean = float(np.mean(brightened))
        logger.info(
            "Dark image fix: gamma=%.2f, brightness %.0f → %.0f",
            gamma, mean_brightness, new_mean,
        )
        return brightened

    # ── Step 2: Smart denoise ────────────────────────────────────────

    def _smart_denoise(self, img: np.ndarray) -> np.ndarray:
        """Denoise only when the image is actually noisy.

        Clean PDF renders (high Laplacian variance, low high-frequency
        noise) are passed through untouched because denoising would only
        smear crisp text edges.

        Uses a two-stage approach:
          Stage 1 – Bilateral filter (edge-preserving): smooths flat
                    areas while preserving sharp text edges.  This is
                    superior to Gaussian blur for documents.
          Stage 2 – fastNlMeansDenoising only for high-noise images
                    (noise_est > 15) or aggressive mode.

        This preserves blur scores much better than the old approach
        of applying fastNlMeansDenoising unconditionally.
        """
        if self.aggressive:
            return cv2.fastNlMeansDenoising(
                img, None, h=self._denoise_h,
                templateWindowSize=7, searchWindowSize=21,
            )

        # Estimate noise: standard deviation of a Laplacian (edge) image.
        laplacian = cv2.Laplacian(img, cv2.CV_64F)
        sharpness = laplacian.var()

        blurred = cv2.GaussianBlur(img, (5, 5), 0)
        noise_est = float(np.std(img.astype(float) - blurred.astype(float)))

        logger.debug("Noise estimation: sharpness=%.1f  noise_est=%.1f", sharpness, noise_est)

        if noise_est <= 8.0:
            logger.debug("Denoising SKIPPED – image is already clean (noise_est=%.1f)", noise_est)
            return img

        # Stage 1: Bilateral filter – edge-preserving denoising
        # d=5: neighbourhood diameter
        # sigmaColor=40: similarity in colour space (lower = preserve more edges)
        # sigmaSpace=40: spatial closeness influence
        result = cv2.bilateralFilter(img, d=5, sigmaColor=40, sigmaSpace=40)
        logger.info(
            "Bilateral denoise applied (noise_est=%.1f, d=5, sigmaColor=40)",
            noise_est,
        )

        # Stage 2: For very noisy images, add gentle NLMeans on top
        if noise_est > 15.0:
            result = cv2.fastNlMeansDenoising(
                result, None, h=self._denoise_h,
                templateWindowSize=7, searchWindowSize=21,
            )
            logger.info(
                "Additional NLMeans denoise applied (noise_est=%.1f, h=%d)",
                noise_est, self._denoise_h,
            )

        return result

    # ── Step 3: CLAHE ────────────────────────────────────────────────

    def _clahe(self, img: np.ndarray) -> np.ndarray:
        clahe = cv2.createCLAHE(
            clipLimit=self._clahe_clip,
            tileGridSize=(8, 8),
        )
        return clahe.apply(img)
    # ── Step 3b: Brightness matching ─────────────────────────────────

    @staticmethod
    def _match_brightness(
        enhanced: np.ndarray,
        original: np.ndarray,
    ) -> np.ndarray:
        """Ensure the enhanced image is not darker than the original.

        CLAHE and grayscale conversion can shift the mean brightness
        downward, making the output look muddy.  This step computes
        a per-pixel gamma/offset correction so the enhanced image's
        mean brightness matches (or slightly exceeds) the original.
        """
        orig_mean = float(np.mean(original))
        enh_mean = float(np.mean(enhanced))

        if enh_mean >= orig_mean:
            # Already as bright or brighter – nothing to do
            return enhanced

        # Compute a brightness offset to match original + small boost
        # Using additive shift is simple and preserves contrast
        brightness_boost = 5.0  # slight extra brightness
        delta = (orig_mean - enh_mean) + brightness_boost

        result = np.clip(
            enhanced.astype(np.float32) + delta, 0, 255,
        ).astype(np.uint8)

        logger.info(
            "Brightness matched: enhanced=%.0f → %.0f (original=%.0f, delta=+%.0f)",
            enh_mean, float(np.mean(result)), orig_mean, delta,
        )
        return result
    # ── Step 1b: Unsharp Mask Sharpening ─────────────────────────────

    def _unsharp_mask(
        self,
        img: np.ndarray,
        amount: float | None = None,
        sigma: float | None = None,
    ) -> np.ndarray:
        """Apply unsharp-mask sharpening to restore edges after denoising.

        Unsharp masking works by:
          1. Blurring the image (Gaussian)
          2. Subtracting the blur from the original (= high-frequency detail)
          3. Adding the detail back, amplified by `amount`

        This is the gold standard for OCR preprocessing sharpening because
        it enhances text edges without amplifying noise (the preceding
        denoising step already removed noise).

        Formula:  sharpened = original + amount × (original − blurred)
        """
        amt = amount if amount is not None else self._sharpen_amount
        sig = sigma if sigma is not None else self._sharpen_radius

        # Kernel size must be odd; derive from sigma
        ksize = int(2 * round(2 * sig) + 1)
        if ksize < 3:
            ksize = 3

        blurred = cv2.GaussianBlur(img, (ksize, ksize), sig)

        # Compute in float to avoid clipping during subtraction
        sharpened = cv2.addWeighted(
            img, 1.0 + amt,
            blurred, -amt,
            0,
        )

        logger.info(
            "Unsharp mask applied (amount=%.2f, sigma=%.1f, ksize=%d)",
            amt, sig, ksize,
        )
        return sharpened

    # ── Step 3b: Morphological noise cleanup ─────────────────────────

    @staticmethod
    def _morph_cleanup(img: np.ndarray) -> np.ndarray:
        """Remove small noise specks using morphological opening.

        Opening = erosion followed by dilation.  With a tiny 2×2 kernel,
        it removes isolated 1–2 pixel dots (scanner noise, dust) while
        leaving text strokes (which are much thicker) intact.

        This improves OCR because noise specks can be misread as
        punctuation marks, diacritics, or character fragments.
        """
        # Use a small kernel — just enough to clean isolated pixels
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2))
        cleaned = cv2.morphologyEx(img, cv2.MORPH_OPEN, kernel)

        # Count how many pixels changed
        diff = cv2.absdiff(img, cleaned)
        changed = np.count_nonzero(diff)
        total = img.shape[0] * img.shape[1]
        pct = changed / total * 100

        if pct > 0.01:  # Only log if meaningful cleanup happened
            logger.info(
                "Morphological cleanup: removed %.2f%% noise pixels (%d/%d)",
                pct, changed, total,
            )
        else:
            logger.debug("Morphological cleanup: no significant noise detected")

        return cleaned

    # ── Step 6b: Final sharpening ────────────────────────────────────

    @staticmethod
    def _final_sharpen(img: np.ndarray) -> np.ndarray:
        """Apply a light Laplacian-based sharpening as the final step.

        This is gentler than unsharp masking and is specifically tuned
        to make text edges maximally crisp for OCR.  Applied after
        upscaling (which can soften edges via interpolation).

        Uses a 3×3 sharpening kernel:
            [ 0, -1,  0]
            [-1,  5, -1]
            [ 0, -1,  0]

        The centre weight of 5 (= 1 + 4×1) preserves the original
        pixel while subtracting the average of its 4 neighbours,
        effectively enhancing edges.
        """
        kernel = np.array(
            [[0, -1, 0],
             [-1, 5, -1],
             [0, -1, 0]],
            dtype=np.float32,
        )
        sharpened = cv2.filter2D(img, -1, kernel)
        logger.info("Final sharpening applied (3×3 Laplacian kernel, centre=5)")
        return sharpened

    # ── Step 4: Adaptive threshold (region-aware) ────────────────────

    def _auto_threshold_with_mask(
        self,
        img: np.ndarray,
        protection_mask: np.ndarray | None = None,
    ) -> np.ndarray:
        """Apply adaptive threshold with region-aware protection.

        Protected regions (stamps, signatures, tables) keep the gentle
        CLAHE result while text regions get the full threshold treatment.
        This prevents destroying handwriting, coloured stamps, and table
        structures while still improving text contrast for OCR.

        In *aggressive* mode thresholding is always applied everywhere.
        In default mode it is applied only when:
          – The image has extremely low contrast (stddev < 35), AND
          – The histogram is strongly bimodal (most pixels at extremes)
        """
        should_threshold = self._should_threshold(img)

        if not should_threshold:
            return img

        thresholded = cv2.adaptiveThreshold(
            img, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            blockSize=15,
            C=11,
        )

        # If there are protected regions, composite: use threshold for
        # text areas and the gentle (CLAHE) image for protected areas
        if (
            protection_mask is not None
            and protection_mask.shape == img.shape
            and np.any(protection_mask)
        ):
            protected_px = np.count_nonzero(protection_mask)
            total_px = protection_mask.shape[0] * protection_mask.shape[1]
            pct = protected_px / total_px * 100
            logger.info(
                "Region-aware threshold: %.1f%% of image protected from binarisation",
                pct,
            )
            # Where mask is white (255) → keep original CLAHE result
            # Where mask is black (0)   → use thresholded result
            mask_bool = protection_mask > 127
            result = thresholded.copy()
            result[mask_bool] = img[mask_bool]
            return result

        return thresholded

    def _should_threshold(self, img: np.ndarray) -> bool:
        """Decide whether thresholding should be applied at all."""
        if self._force_threshold:
            return True

        stddev = float(np.std(img))
        hist = cv2.calcHist([img], [0], None, [256], [0, 256]).flatten()
        hist /= hist.sum()

        dark_ratio = hist[:50].sum()
        light_ratio = hist[200:].sum()
        extreme_ratio = dark_ratio + light_ratio

        if stddev < 35 and extreme_ratio > 0.80:
            logger.info(
                "Auto-threshold APPLIED (stddev=%.1f, extreme=%.2f)",
                stddev, extreme_ratio,
            )
            return True

        logger.debug(
            "Auto-threshold SKIPPED (stddev=%.1f, extreme=%.2f) – image has enough contrast",
            stddev, extreme_ratio,
        )
        return False

    # ── Step 5: Deskew ───────────────────────────────────────────────

    @staticmethod
    def _deskew(self, img: np.ndarray) -> np.ndarray:
        """Rotate the image to correct the skew angle detected during
        diagnosis.  Uses the angle already computed by
        ``_detect_skew_angle`` (Hough-line based) rather than
        re-computing with the less reliable ``minAreaRect`` approach.

        Skew < 0.5° or > 15° is ignored (noise or intentional rotation).
        """
        # Re-detect on the current (possibly modified) image
        angle = self._detect_skew_angle(img)

        if abs(angle) < 0.5 or abs(angle) > 15:
            logger.debug("Deskew skipped: angle=%.2f° out of range", angle)
            return img

        h, w = img.shape[:2]
        centre = (w // 2, h // 2)
        rot_mat = cv2.getRotationMatrix2D(centre, angle, 1.0)
        rotated = cv2.warpAffine(
            img, rot_mat, (w, h),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
        logger.info("Deskewed by %.2f°", angle)
        return rotated

    # ── Step 6: Conditional upscale ──────────────────────────────────

    def _conditional_upscale(self, img: np.ndarray) -> np.ndarray:
        """Double the resolution when the largest dimension is below
        2400 px (common for low-DPI scans at ~150 DPI).  In *aggressive*
        mode the upscale is always applied.

        300 DPI letter-size pages are ~2550×3300 px and should NOT be
        upscaled — they are already high-quality.
        """
        h, w = img.shape[:2]
        max_dim = max(h, w)

        if self._force_upscale or max_dim < 2400:
            upscaled = cv2.resize(
                img, (w * 2, h * 2),
                interpolation=cv2.INTER_CUBIC,
            )
            logger.info("Upscaled %dx%d → %dx%d", w, h, w * 2, h * 2)
            return upscaled

        return img

    # ── Step 7: Max-dimension cap ────────────────────────────────────

    def _cap_dimensions(self, img: np.ndarray) -> np.ndarray:
        """Downscale proportionally if any side exceeds MAX_DIMENSION.

        This prevents oversized images from exceeding the 4 MB limit
        of Azure AI Document Intelligence.
        """
        h, w = img.shape[:2]
        max_dim = max(h, w)

        if max_dim <= self.MAX_DIMENSION:
            return img

        scale = self.MAX_DIMENSION / max_dim
        new_w = int(w * scale)
        new_h = int(h * scale)
        resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
        logger.info(
            "Dimension cap: %dx%d → %dx%d (scale=%.2f, max=%dpx)",
            w, h, new_w, new_h, scale, self.MAX_DIMENSION,
        )
        return resized

    # ── Step 8: File-size safety net ─────────────────────────────────

    def _shrink_if_too_large(self, path: str) -> None:
        """Re-encode the image at progressively smaller sizes until
        the file fits within MAX_FILE_BYTES.

        Each iteration scales the image by SHRINK_STEP (default 0.85)
        and re-writes the PNG.  Gives up after SHRINK_MAX_ITERS to
        avoid an infinite loop.
        """
        for attempt in range(self.SHRINK_MAX_ITERS):
            file_size = os.path.getsize(path)
            if file_size <= self.MAX_FILE_BYTES:
                if attempt > 0:
                    logger.info(
                        "File size OK after %d shrink(s): %.1f KB",
                        attempt, file_size / 1024,
                    )
                return

            logger.warning(
                "File too large (%.1f KB > %.1f KB limit) – shrinking attempt %d",
                file_size / 1024, self.MAX_FILE_BYTES / 1024, attempt + 1,
            )

            img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
            if img is None:
                logger.error("Cannot re-read %s for shrinking", path)
                return

            h, w = img.shape[:2]
            new_w = int(w * self.SHRINK_STEP)
            new_h = int(h * self.SHRINK_STEP)
            img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
            cv2.imwrite(path, img, [cv2.IMWRITE_PNG_COMPRESSION, 9])

        # Final check
        final_size = os.path.getsize(path)
        if final_size > self.MAX_FILE_BYTES:
            logger.error(
                "Still too large after %d shrinks (%.1f KB). Proceeding anyway.",
                self.SHRINK_MAX_ITERS, final_size / 1024,
            )
