"""Multi-pass auto-tuner for image preprocessing.

Tries several parameter presets on each page image, measures quality
metrics for every result, and keeps the **best** output automatically.

Presets
───────
  gentle       – minimal intervention; preserve original quality
  balanced     – current default processing
  denoise_heavy – stronger denoising + compensatory sharpening
  aggressive   – full aggressive pipeline
  sharp_focus  – minimal denoising, maximum sharpening

Scoring
───────
The composite score is:

    score = 0.6 × ocrReadinessScore + 0.4 × blur_norm

where ``blur_norm = min(blurScore / 500, 1.0)``.

If the best preset still degrades the original, the caller (quality gate
in function_app.py) will roll back to the original image.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from typing import Any

import cv2

from .image_processor import ImageProcessor
from .metrics import MetricsCalculator
from .models import PreprocessOptions

logger = logging.getLogger(__name__)


# ── Preset definitions ───────────────────────────────────────────────

@dataclass
class _Preset:
    """One set of tuning parameters to attempt."""
    name: str
    aggressive: bool
    denoise_h: int
    clahe_clip: float
    sharpen_amount: float
    sharpen_radius: float
    enable_morph_cleanup: bool
    enable_sharpen: bool
    enable_final_sharpen: bool

    # Extra description for logging / reports
    description: str = ""


_PRESETS: list[_Preset] = [
    _Preset(
        name="gentle",
        aggressive=False,
        denoise_h=2,
        clahe_clip=1.0,
        sharpen_amount=0.3,
        sharpen_radius=1.0,
        enable_morph_cleanup=False,
        enable_sharpen=True,
        enable_final_sharpen=True,
        description="Minimal intervention – preserves original quality",
    ),
    _Preset(
        name="balanced",
        aggressive=False,
        denoise_h=3,
        clahe_clip=1.5,
        sharpen_amount=0.7,
        sharpen_radius=1.2,
        enable_morph_cleanup=True,
        enable_sharpen=True,
        enable_final_sharpen=True,
        description="Standard processing with good sharpening",
    ),
    _Preset(
        name="denoise_heavy",
        aggressive=False,
        denoise_h=7,
        clahe_clip=2.0,
        sharpen_amount=0.9,
        sharpen_radius=1.2,
        enable_morph_cleanup=True,
        enable_sharpen=True,
        enable_final_sharpen=True,
        description="Strong denoising with aggressive compensatory sharpening",
    ),
    _Preset(
        name="aggressive",
        aggressive=True,
        denoise_h=10,
        clahe_clip=2.5,
        sharpen_amount=0.7,
        sharpen_radius=1.0,
        enable_morph_cleanup=True,
        enable_sharpen=True,
        enable_final_sharpen=True,
        description="Full aggressive enhancement pipeline",
    ),
    _Preset(
        name="sharp_focus",
        aggressive=False,
        denoise_h=1,
        clahe_clip=1.5,
        sharpen_amount=0.9,
        sharpen_radius=1.5,
        enable_morph_cleanup=False,
        enable_sharpen=True,
        enable_final_sharpen=True,
        description="Minimal denoising, maximum sharpening for blurry docs",
    ),
    _Preset(
        name="ultra_sharp",
        aggressive=False,
        denoise_h=1,
        clahe_clip=1.5,
        sharpen_amount=1.2,
        sharpen_radius=2.0,
        enable_morph_cleanup=False,
        enable_sharpen=True,
        enable_final_sharpen=True,
        description="Maximum edge crispness — double-pass sharpening for soft scans",
    ),
]


# If a preset scores at or above this threshold, skip remaining
# presets — the quality is already excellent and further attempts
# are unlikely to improve on it.
EARLY_EXIT_SCORE: float = 0.85


def _composite_score(metrics: dict) -> float:
    """Compute a composite quality score from metrics.

    Higher is better.  Range approximately [0, 1].
    """
    blur = metrics.get("blurScore", 0)
    ocr = metrics.get("ocrReadinessScore", 0)
    blur_norm = min(blur / 500.0, 1.0)
    return 0.6 * ocr + 0.4 * blur_norm


# ── Auto-tuner ───────────────────────────────────────────────────────

class AutoTuner:
    """Try multiple parameter presets and keep the best result."""

    def __init__(self, user_opts: PreprocessOptions) -> None:
        self._user_opts = user_opts

    # ─────────────────────────────────────────────────────────────────

    def tune(
        self,
        page_path: str,
        output_path: str,
        tmp_dir: str,
        page_idx: int = 1,
    ) -> dict[str, Any]:
        """Run all presets on *page_path*, keep the best one.

        Memory-optimised: processes one preset at a time and only keeps
        the current best candidate on disk.  At most TWO temp images
        exist simultaneously (current candidate + previous best).

        Writes the winning image to *output_path*.

        Returns a dict with:
          - ``enhancement``: the enhancement report from the winning preset
          - ``autoTuning``: metadata about all presets tried
          - ``winnerPreset``: name of the winning preset
          - ``last_detection``: region detector result from the winner
        """
        import gc

        from .region_detector import RegionDetector

        metrics_calc = MetricsCalculator()
        user = self._user_opts

        # ── Shared region detection ──────────────────────────────
        # Run once on the original page and share across all presets
        # so we don't repeat the expensive contour/colour analysis.
        img_for_detect = cv2.imread(page_path)
        if img_for_detect is not None:
            shared_detection = RegionDetector().detect(img_for_detect)
            del img_for_detect
        else:
            shared_detection = None

        # Tracking for the summary (lightweight — no image data)
        preset_scores: list[dict] = []

        # Current best
        best_score = -1.0
        best_report: dict | None = None
        best_metrics: dict | None = None
        best_preset_name: str = ""
        best_detection: Any = None
        best_path: str | None = None

        for preset in _PRESETS:
            candidate_path = os.path.join(
                tmp_dir, f"autotune-p{page_idx}-{preset.name}.png"
            )

            # Build processor respecting user toggles but using the
            # preset's core tuning parameters.
            processor = ImageProcessor(
                aggressive=preset.aggressive,
                denoise_h=preset.denoise_h,
                clahe_clip=preset.clahe_clip,
                # Respect user-set toggles
                enable_autocrop=user.enable_autocrop,
                autocrop_margin=user.autocrop_margin,
                enable_denoise=user.enable_denoise,
                enable_deskew=user.enable_deskew,
                enable_upscale=user.enable_upscale,
                enable_brightness_fix=user.enable_brightness_fix,
                enable_contrast_fix=user.enable_contrast_fix,
                # Diagnosis thresholds from user
                dark_threshold=user.dark_threshold,
                noise_threshold=user.noise_threshold,
                contrast_threshold=user.contrast_threshold,
                skew_min_angle=user.skew_min_angle,
                skew_max_angle=user.skew_max_angle,
                upscale_below=user.upscale_below,
            )

            # Override internal sharpening / cleanup knobs per preset
            processor._sharpen_amount = preset.sharpen_amount
            processor._sharpen_radius = preset.sharpen_radius
            processor._enable_morph_cleanup = preset.enable_morph_cleanup
            processor._enable_sharpen = preset.enable_sharpen
            processor._enable_final_sharpen = preset.enable_final_sharpen

            try:
                report = processor.process(
                    page_path, candidate_path,
                    precomputed_detection=shared_detection,
                )
            except Exception as exc:
                logger.warning(
                    "AutoTune preset '%s' failed for page %d: %s",
                    preset.name, page_idx, exc,
                )
                continue

            # Measure quality
            m = metrics_calc.calculate(candidate_path, page_path)
            score = _composite_score(m)

            logger.info(
                "AutoTune preset='%s' | page=%d | score=%.4f | "
                "blur=%.1f ocr=%.3f redact=%.1f%%",
                preset.name, page_idx, score,
                m.get("blurScore", 0),
                m.get("ocrReadinessScore", 0),
                m.get("redactionPercent", 0),
            )

            # Record lightweight summary for the report
            preset_scores.append({
                "preset": preset.name,
                "description": preset.description,
                "score": round(score, 4),
                "blurScore": round(m.get("blurScore", 0), 1),
                "ocrReadiness": round(m.get("ocrReadinessScore", 0), 3),
                "redactionPercent": round(m.get("redactionPercent", 0), 1),
            })

            if score > best_score:
                # New best — delete old best file (if any)
                if best_path and os.path.exists(best_path):
                    try:
                        os.remove(best_path)
                    except OSError:
                        pass

                best_score = score
                best_report = report
                best_metrics = m
                best_preset_name = preset.name
                best_detection = processor.last_detection
                best_path = candidate_path
            else:
                # This candidate lost — delete it immediately
                try:
                    os.remove(candidate_path)
                except OSError:
                    pass

            # Release processor memory
            del processor
            gc.collect()

            # ── Early exit: skip remaining presets if quality is
            #    already excellent — further attempts are unlikely
            #    to improve on this score.
            if best_score >= EARLY_EXIT_SCORE:
                logger.info(
                    "AutoTune EARLY EXIT at preset '%s' | page=%d | "
                    "score=%.4f >= %.2f threshold",
                    preset.name, page_idx, best_score, EARLY_EXIT_SCORE,
                )
                break

        if best_report is None or best_metrics is None or best_path is None:
            raise RuntimeError(
                f"All auto-tuning presets failed for page {page_idx}"
            )

        # Copy winning image to the requested output path
        shutil.copy2(best_path, output_path)
        try:
            os.remove(best_path)
        except OSError:
            pass

        # Build auto-tuning summary
        preset_scores.sort(key=lambda p: p["score"], reverse=True)

        best_report["autoTuning"] = {
            "presetsTriedCount": len(preset_scores),
            "winnerPreset": best_preset_name,
            "winnerScore": best_score,
            "allPresets": preset_scores,
        }

        logger.info(
            "AutoTune WINNER='%s' | page=%d | score=%.4f | "
            "tried=%d presets",
            best_preset_name, page_idx, best_score,
            len(preset_scores),
        )

        return {
            "enhancement": best_report,
            "winnerPreset": best_preset_name,
            "winnerMetrics": best_metrics,
            "last_detection": best_detection,
        }
