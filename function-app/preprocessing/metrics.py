"""Quality metrics used to decide whether an enhanced image is ready for OCR.

Thresholds (configurable via module-level constants)
────────────────────────────────────────────────────
  BLUR_FAIL_THRESHOLD   = 50    Variance-of-Laplacian < 50 → F02 Blurry
  REDACTION_FAIL_THRESH = 70    > 70% large-black-rect area → F03 Mostly redacted
  OCR_FAIL_THRESHOLD    = 0.30  readiness score < 0.30      → F01 Low OCR confidence risk
  OCR_RETRY_THRESHOLD   = 0.50  0.30 ≤ score < 0.50        → retry_stronger

Decision cascade (evaluated top-to-bottom, first match wins):
  1. All pages corrupt (F07)  → fail
  2. avgBlurScore < 50        → fail  F02
  3. avgRedactionPercent > 70  → fail  F03
  4. avgOcrReadiness < 0.30   → fail  F01
  5. avgOcrReadiness < 0.50   → retry_stronger
  6. Otherwise                → run_doc_intel

ocrReadinessScore formula (per page):
  (0.4 × blur_norm + 0.3 × contrast_norm + 0.3 × redaction_penalty) × faded_penalty
  where
    blur_norm      = min(blurScore / 500, 1.0)
    contrast_norm  = min(stddev(pixels) / 80, 1.0)
    redaction_pen  = max(0, 1 − redactionPercent / 100)
    faded_penalty  = 1.0 (normal) or 0.5–1.0 (if mean > 200 and dark pixels < 5%)
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# ── Threshold constants ──────────────────────────────────────────────
BLUR_FAIL_THRESHOLD: float = 50.0
REDACTION_FAIL_THRESHOLD: float = 70.0
OCR_FAIL_THRESHOLD: float = 0.30
OCR_RETRY_THRESHOLD: float = 0.50

# Warning thresholds – passed but marginal quality
BLUR_WARN_THRESHOLD: float = 120.0       # blur < 120 → warn (50–120 is marginal)
REDACTION_WARN_THRESHOLD: float = 40.0   # redaction > 40% → warn
OCR_WARN_THRESHOLD: float = 0.60         # readiness < 0.60 → warn
DPI_WARN_THRESHOLD: int = 150            # DPI < 150 → warn
FADED_BRIGHTNESS_THRESHOLD: float = 200.0  # mean > 200 → faded/washed-out
FADED_DARK_PIXEL_THRESHOLD: float = 5.0    # dark pixels (<30) < 5% → faded text

# ── Failure code descriptions ────────────────────────────────────────
FAILURE_DESCRIPTIONS: dict[str, dict] = {
    "F01": {
        "title": "Low OCR Confidence",
        "description": "The overall OCR-readiness score is critically low (below 0.30). "
                       "The document is unlikely to produce accurate text extraction.",
        "cause": "Very poor image quality combining multiple issues — blurry text, "
                 "low contrast, and/or significant redaction.",
        "suggestion": "Re-scan the document at a higher resolution (300+ DPI), ensure "
                      "good lighting, and use a flatbed scanner if possible.",
    },
    "F02": {
        "title": "Image Too Blurry",
        "description": "The sharpness score (Laplacian variance) is below 50, indicating "
                       "the text is too blurred for reliable character recognition.",
        "cause": "Camera shake during capture, out-of-focus lens, excessive motion blur, "
                 "or a very low-resolution source image.",
        "suggestion": "Re-capture the document using a steady camera or scanner. Ensure "
                      "the text is in sharp focus. Avoid photographing at angles.",
    },
    "F03": {
        "title": "Document Mostly Redacted",
        "description": "More than 70% of the document area is covered by large black "
                       "rectangles (redaction blocks), leaving too little readable content.",
        "cause": "The document has heavy redaction/censoring applied, or was scanned "
                 "with a mostly black cover page.",
        "suggestion": "If the redaction is unintentional, re-scan without the covering material. "
                      "If intentional, the document has limited extractable text.",
    },
    "F04": {
        "title": "Table/Graph-Heavy Document",
        "description": "The document is predominantly composed of tables, charts, or "
                       "graphical elements with very little running text.",
        "cause": "The document is a lab report form, chart printout, or structured "
                 "table that contains minimal free-text narrative.",
        "suggestion": "Ensure Document Intelligence 'prebuilt-layout' model is used for "
                      "table extraction. The summary may rely heavily on structured data.",
    },
    "F05": {
        "title": "Non-Medical / Administrative Document",
        "description": "The document appears to be administrative or non-medical in nature. "
                       "No clinically relevant content was identified.",
        "cause": "The uploaded file is a cover page, consent form, insurance document, "
                 "or other non-clinical material.",
        "suggestion": "Verify that the correct document was uploaded. If this is part of a "
                      "multi-page bundle, the medical content may be on other pages.",
    },
    "F06": {
        "title": "Empty or Near-Empty Document",
        "description": "The document contains very little or no extractable text content "
                       "(fewer than 20 meaningful characters).",
        "cause": "Blank page, mostly white/empty scan, or a page with only headers/footers "
                 "and no body content.",
        "suggestion": "Check if the document was scanned correctly. The page may be blank "
                      "or the scanner failed to capture the content.",
    },
    "F07": {
        "title": "Corrupt or Unreadable File",
        "description": "The image file could not be decoded. It may be corrupted, "
                       "truncated, or in an unsupported format.",
        "cause": "File corruption during upload, unsupported image codec, "
                 "or a zero-byte / truncated file.",
        "suggestion": "Re-upload the file. Ensure it is a valid JPEG, PNG, TIFF, or BMP. "
                      "Check that the file is not zero-bytes or damaged.",
    },
    "F08": {
        "title": "Summary Generation Failed",
        "description": "OCR succeeded but the AI model could not produce a useful clinical "
                       "summary from the extracted text.",
        "cause": "The OCR output may be too fragmented, the text may be in an unsupported "
                 "language, or the content is not medically relevant.",
        "suggestion": "Review the raw OCR output. If the text is readable, try adjusting "
                      "the prompt or re-running summarization.",
    },
}

# Standard US letter dimensions for DPI estimation fallback
_LETTER_W_IN = 8.5
_LETTER_H_IN = 11.0


class MetricsCalculator:
    """Compute and aggregate per-page quality metrics."""

    # ── Thresholds for "already clean enough" ─────────────────────────
    # If the original image exceeds ALL of these, preprocessing is skipped.
    SKIP_BLUR_MIN: float = 200.0       # Laplacian variance – sharp enough
    SKIP_CONTRAST_MIN: float = 45.0    # Pixel stddev – good contrast
    SKIP_REDACTION_MAX: float = 10.0   # Less than 10% redacted
    SKIP_READINESS_MIN: float = 0.65   # Composite readiness high enough

    # ── Quick quality check on original ───────────────────────────────

    def is_already_clean(self, image_path: str) -> tuple[bool, dict]:
        """Evaluate the *original* image and decide whether preprocessing
        can be skipped entirely.

        Returns ``(skip, metrics)`` where *skip* is True when the image
        is already OCR-ready and *metrics* is the quality dict.
        """
        img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return False, self._corrupt_metrics()

        blur = self._blur_score(img)
        dpi = self._estimated_dpi(img)
        redact = self._redaction_percent(img)
        readiness = self._ocr_readiness(blur, redact, img)
        contrast = float(np.std(img))

        metrics = {
            "blurScore": blur,
            "estimatedDpi": dpi,
            "redactionPercent": redact,
            "ocrReadinessScore": readiness,
        }

        skip = (
            blur >= self.SKIP_BLUR_MIN
            and contrast >= self.SKIP_CONTRAST_MIN
            and redact <= self.SKIP_REDACTION_MAX
            and readiness >= self.SKIP_READINESS_MIN
        )

        if skip:
            logger.info(
                "Image ALREADY CLEAN – skipping preprocessing "
                "(blur=%.0f, contrast=%.1f, redact=%.1f%%, readiness=%.3f)",
                blur, contrast, redact, readiness,
            )
        else:
            logger.info(
                "Image NEEDS preprocessing "
                "(blur=%.0f, contrast=%.1f, redact=%.1f%%, readiness=%.3f)",
                blur, contrast, redact, readiness,
            )

        return skip, metrics

    # ── Per-page ─────────────────────────────────────────────────────

    def calculate(self, enhanced_path: str, original_path: str) -> dict:
        """Return a dict of quality metrics for *enhanced_path*."""
        img = cv2.imread(enhanced_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            logger.warning("Cannot read enhanced image %s – marking F07", enhanced_path)
            return self._corrupt_metrics()

        blur = self._blur_score(img)
        dpi = self._estimated_dpi(img)
        redact = self._redaction_percent(img)
        readiness = self._ocr_readiness(blur, redact, img)

        # Faded-text metrics for quality warnings
        mean_brightness = float(np.mean(img))
        total_pixels = img.size
        dark_pixel_pct = float(np.sum(img < 30)) / total_pixels * 100.0

        return {
            "blurScore": blur,
            "estimatedDpi": dpi,
            "redactionPercent": redact,
            "ocrReadinessScore": readiness,
            "meanBrightness": round(mean_brightness, 1),
            "darkPixelPercent": round(dark_pixel_pct, 1),
        }

    # ── Aggregate across pages ───────────────────────────────────────

    def aggregate(self, page_results: list[dict]) -> dict:
        """Produce an aggregated summary with ``recommendedNextAction``."""
        if not page_results:
            return self._fail_summary(code="F07")

        corrupt = [p for p in page_results if p.get("failureCode") == "F07"]
        if len(corrupt) == len(page_results):
            return self._fail_summary(code="F07")

        valid = [p for p in page_results if p.get("failureCode") != "F07"]
        avg_blur = float(np.mean([p["blurScore"] for p in valid]))
        avg_ocr = float(np.mean([p["ocrReadinessScore"] for p in valid]))
        avg_redact = float(np.mean([p["redactionPercent"] for p in valid]))

        action, code = self._decide(avg_blur, avg_ocr, avg_redact)

        # Collect quality warnings for documents that pass but are marginal
        warnings = self._quality_warnings(avg_blur, avg_ocr, avg_redact, valid)

        # Add granular failure description if applicable
        failure_info = FAILURE_DESCRIPTIONS.get(code) if code else None

        result = {
            "avgBlurScore": round(avg_blur, 2),
            "avgOcrReadinessScore": round(avg_ocr, 3),
            "avgRedactionPercent": round(avg_redact, 2),
            "recommendedNextAction": action,
            "failureCode": code,
        }

        if failure_info:
            result["failureDescription"] = failure_info

        if warnings:
            result["qualityWarnings"] = warnings

        return result

    # ── Individual metric helpers ────────────────────────────────────

    @staticmethod
    def _blur_score(img: np.ndarray) -> float:
        """Variance of the Laplacian – higher means sharper."""
        lap = cv2.Laplacian(img, cv2.CV_64F)
        return round(float(lap.var()), 2)

    @staticmethod
    def _estimated_dpi(img: np.ndarray) -> int:
        """Estimate DPI assuming a US-letter page if real metadata is absent."""
        h, w = img.shape[:2]
        dpi_by_w = w / _LETTER_W_IN
        dpi_by_h = h / _LETTER_H_IN
        return round(max(dpi_by_w, dpi_by_h))

    @staticmethod
    def _redaction_percent(img: np.ndarray) -> float:
        """Estimate percentage of the image covered by intentional redaction bars.

        A contour is considered a *redaction block* when ALL of:
          • intensity threshold ≤ 20 (near-black — not just dark)
          • bounding-box area > 1 % of the total image area
          • fill ratio ≥ 0.85 (very solid rectangle)
          • aspect ratio between 1.5:1 and 60:1 (bar-shaped, not a full
            page-width border or a square logo)
          • neither dimension spans > 90 % of the image edge (excludes
            scanner borders / margins)

        Previous settings (threshold=30, area>0.5%, fill>0.70) produced
        false positives on dark headers, scanner shadows, and logos.
        """
        h, w = img.shape[:2]
        _, binary = cv2.threshold(img, 20, 255, cv2.THRESH_BINARY_INV)
        contours, _ = cv2.findContours(
            binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE,
        )

        total_px = h * w
        redacted_px = 0

        for cnt in contours:
            cnt_area = cv2.contourArea(cnt)
            _, _, bw, bh = cv2.boundingRect(cnt)
            box_area = bw * bh
            if box_area == 0:
                continue

            # Must be large enough to be an intentional bar
            if box_area < total_px * 0.01:
                continue

            # Must be solidly filled (real redaction is a solid rectangle)
            if cnt_area / box_area < 0.85:
                continue

            # Aspect ratio filter — redaction bars are elongated, not square
            aspect = max(bw, bh) / max(min(bw, bh), 1)
            if aspect < 1.5 or aspect > 60:
                continue

            # Exclude full-width / full-height borders
            if bw > w * 0.9 or bh > h * 0.9:
                continue

            redacted_px += cnt_area

        return round(redacted_px / total_px * 100, 2)

    @staticmethod
    def _ocr_readiness(blur: float, redact_pct: float, img: np.ndarray) -> float:
        """Weighted score in [0, 1] combining sharpness, contrast, redaction, and faded text.

        The faded-text penalty detects washed-out scans where text is light gray
        on white background.  These score high on blur (sharp edges exist) but
        are actually hard-to-read.  Uses two signals:
          - Dark pixel ratio: fewer dark pixels = more faded
          - White pixel dominance: more white pixels = more washed out
        Penalty range is 0.35 (severely faded) to 1.0 (normal).
        """
        blur_norm = min(blur / 500.0, 1.0)
        contrast_norm = min(float(np.std(img)) / 80.0, 1.0)
        redact_penalty = max(0.0, 1.0 - redact_pct / 100.0)

        # Faded-text penalty: washed-out scans with very few dark pixels
        # Uses two signals: high brightness AND low dark-pixel ratio
        mean_brightness = float(np.mean(img))
        total_pixels = img.size
        dark_pixel_pct = float(np.sum(img < 30)) / total_pixels * 100.0
        white_pixel_pct = float(np.sum(img > 230)) / total_pixels * 100.0

        if mean_brightness > FADED_BRIGHTNESS_THRESHOLD and dark_pixel_pct < FADED_DARK_PIXEL_THRESHOLD:
            # Primary signal: how few dark (text) pixels exist
            # 5% dark = mild fade, 1% dark = severe fade, 0% = blank
            dark_severity = 1.0 - min(dark_pixel_pct / FADED_DARK_PIXEL_THRESHOLD, 1.0)
            # Secondary signal: how white-dominated the page is
            white_severity = min(white_pixel_pct / 60.0, 1.0)  # 60%+ white = max
            # Combined: use the stronger of the two signals
            fade_severity = max(dark_severity, white_severity)
            # Penalty range: 0.35 (severely faded) to 0.90 (mildly faded)
            faded_penalty = max(0.35, 1.0 - fade_severity * 0.65)
        else:
            faded_penalty = 1.0

        score = (0.4 * blur_norm + 0.3 * contrast_norm + 0.3 * redact_penalty) * faded_penalty
        return round(min(max(score, 0.0), 1.0), 3)

    # ── Decision logic ───────────────────────────────────────────────

    @staticmethod
    def _decide(
        avg_blur: float,
        avg_ocr: float,
        avg_redact: float,
    ) -> tuple[str, str | None]:
        """Apply the threshold cascade and return (action, failureCode|None).

        Changed from hard-fail to try-anyway: documents below the old fail
        thresholds now get ``run_doc_intel_low_confidence`` instead of
        ``fail``, allowing the pipeline to attempt OCR and flag the result.
        Only truly unreadable images (blur < 15 or redaction > 90%) hard-fail.
        """
        # Hard fail: truly unreadable
        if avg_blur < 15:
            return "fail", "F02"
        if avg_redact > 90:
            return "fail", "F03"

        # Below old thresholds → try anyway with low-confidence flag
        if avg_blur < BLUR_FAIL_THRESHOLD:
            return "run_doc_intel_low_confidence", "F02"
        if avg_redact > REDACTION_FAIL_THRESHOLD:
            return "run_doc_intel_low_confidence", "F03"
        if avg_ocr < OCR_FAIL_THRESHOLD:
            return "run_doc_intel_low_confidence", "F01"

        # Marginal → aggressive retry
        if avg_ocr < OCR_RETRY_THRESHOLD:
            return "retry_stronger", None

        return "run_doc_intel", None

    # ── Quality warnings ─────────────────────────────────────────────

    @staticmethod
    def _quality_warnings(
        avg_blur: float,
        avg_ocr: float,
        avg_redact: float,
        valid_pages: list[dict],
    ) -> list[dict]:
        """Return a list of quality warning dicts for marginal-quality documents
        that pass the failure thresholds but have concerning metrics."""
        warnings: list[dict] = []

        # Only warn for documents that did NOT fail
        if avg_blur < BLUR_FAIL_THRESHOLD or avg_redact > REDACTION_FAIL_THRESHOLD or avg_ocr < OCR_FAIL_THRESHOLD:
            return warnings  # failures handled by failureDescription

        if avg_blur < BLUR_WARN_THRESHOLD:
            warnings.append({
                "code": "W01",
                "severity": "high" if avg_blur < 80 else "medium",
                "title": "Low Sharpness",
                "message": f"Blur score is {avg_blur:.0f} (recommended: ≥{BLUR_WARN_THRESHOLD:.0f}). "
                           "Text may be partially unreadable. Consider re-scanning at higher resolution.",
                "metric": "blurScore",
                "value": round(avg_blur, 2),
                "threshold": BLUR_WARN_THRESHOLD,
            })

        if avg_redact > REDACTION_WARN_THRESHOLD:
            warnings.append({
                "code": "W02",
                "severity": "medium",
                "title": "Significant Redaction",
                "message": f"Redaction coverage is {avg_redact:.1f}% (threshold: {REDACTION_WARN_THRESHOLD:.0f}%). "
                           "A significant portion of the document is obscured, which may affect OCR accuracy.",
                "metric": "redactionPercent",
                "value": round(avg_redact, 2),
                "threshold": REDACTION_WARN_THRESHOLD,
            })

        if avg_ocr < OCR_WARN_THRESHOLD:
            warnings.append({
                "code": "W03",
                "severity": "high" if avg_ocr < 0.45 else "medium",
                "title": "Low OCR Readiness",
                "message": f"OCR readiness is {avg_ocr:.3f} (recommended: ≥{OCR_WARN_THRESHOLD:.2f}). "
                           "Text extraction quality may be degraded. Result should be reviewed carefully.",
                "metric": "ocrReadinessScore",
                "value": round(avg_ocr, 3),
                "threshold": OCR_WARN_THRESHOLD,
            })

        # Check per-page DPI
        avg_dpi = float(np.mean([p.get("estimatedDpi", 300) for p in valid_pages]))
        if avg_dpi < DPI_WARN_THRESHOLD:
            warnings.append({
                "code": "W04",
                "severity": "medium",
                "title": "Low Resolution",
                "message": f"Estimated DPI is {avg_dpi:.0f} (recommended: ≥{DPI_WARN_THRESHOLD}). "
                           "Low resolution may cause small text to be missed or misread.",
                "metric": "estimatedDpi",
                "value": round(avg_dpi),
                "threshold": DPI_WARN_THRESHOLD,
            })

        # Check contrast via OCR readiness breakdown (if blur is fine but readiness is low, contrast is the issue)
        if avg_blur >= BLUR_WARN_THRESHOLD and avg_redact <= REDACTION_WARN_THRESHOLD and avg_ocr < OCR_WARN_THRESHOLD:
            warnings.append({
                "code": "W05",
                "severity": "medium",
                "title": "Low Contrast",
                "message": "Despite acceptable sharpness, the OCR readiness score is low — "
                           "this typically indicates poor contrast between text and background. "
                           "Consider adjusting the contrast threshold or re-scanning with better lighting.",
                "metric": "contrast",
            })

        # Check for faded/washed-out pages
        faded_pages = [
            p for p in valid_pages
            if p.get("meanBrightness", 0) > FADED_BRIGHTNESS_THRESHOLD
            and p.get("darkPixelPercent", 100) < FADED_DARK_PIXEL_THRESHOLD
        ]
        if faded_pages:
            avg_brightness = float(np.mean([p["meanBrightness"] for p in faded_pages]))
            warnings.append({
                "code": "W06",
                "severity": "high",
                "title": "Faded / Washed-Out Document",
                "message": f"The document appears faded or washed-out (mean brightness {avg_brightness:.0f}, "
                           f"very few dark pixels). Text may be light gray on white background, "
                           f"making OCR extraction unreliable despite high blur scores. "
                           f"Consider re-scanning with better contrast or adjusting scanner brightness.",
                "metric": "meanBrightness",
                "value": round(avg_brightness, 1),
                "threshold": FADED_BRIGHTNESS_THRESHOLD,
            })

        return warnings

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _corrupt_metrics() -> dict:
        return {
            "blurScore": 0.0,
            "estimatedDpi": 0,
            "redactionPercent": 0.0,
            "ocrReadinessScore": 0.0,
            "failureCode": "F07",
        }

    @staticmethod
    def _fail_summary(code: str) -> dict:
        result = {
            "avgBlurScore": 0.0,
            "avgOcrReadinessScore": 0.0,
            "avgRedactionPercent": 0.0,
            "recommendedNextAction": "fail",
            "failureCode": code,
        }
        failure_info = FAILURE_DESCRIPTIONS.get(code)
        if failure_info:
            result["failureDescription"] = failure_info
        return result
