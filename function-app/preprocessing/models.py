"""Data models for preprocessing requests and results."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PreprocessOptions:
    """Tuning knobs passed from the Logic App or the dashboard UI.

    Every field has a sensible default.  The UI sends only the fields
    the user has changed; everything else falls back to defaults.
    """

    aggressive: bool = False
    """When True the pipeline applies stronger denoising, forces adaptive
    thresholding, and always up-scales regardless of resolution."""

    # ── Step toggles ─────────────────────────────────────────────────
    enable_autocrop: bool = True
    enable_denoise: bool = True
    enable_deskew: bool = True
    enable_upscale: bool = True
    enable_brightness_fix: bool = True
    enable_contrast_fix: bool = True

    # ── Per-step tuning ──────────────────────────────────────────────
    denoise_h: int | None = None          # None → auto (3 gentle / 10 aggressive)
    clahe_clip: float | None = None       # None → auto (1.5 / 2.5)
    force_threshold: bool | None = None   # None → follows aggressive flag
    force_upscale: bool | None = None     # None → follows aggressive flag
    autocrop_margin: int = 10             # px margin to keep around content

    # ── Diagnosis thresholds ─────────────────────────────────────────
    dark_threshold: float = 140.0         # mean brightness below this → dark
    noise_threshold: float = 8.0          # noise est above this → noisy
    contrast_threshold: float = 50.0      # stddev below this → low contrast
    skew_min_angle: float = 0.5           # degrees – ignore below this
    skew_max_angle: float = 15.0          # degrees – ignore above this
    upscale_below: int = 2400             # max dim below this → upscale

    @classmethod
    def from_dict(cls, data: dict | None) -> PreprocessOptions:
        if not data:
            return cls()

        def _toggle(data: dict, backend_key: str, ui_key: str, default: bool = True) -> bool:
            """Accept both backend (enableAutocrop) and UI (autoCrop) key names."""
            if backend_key in data:
                return bool(data[backend_key])
            if ui_key in data:
                return bool(data[ui_key])
            return default

        def _num(data: dict, *keys, default=None):
            """Return the first key found in data (None-safe, 0-safe)."""
            for k in keys:
                if k in data:
                    return data[k]
            return default

        return cls(
            aggressive=data.get("aggressive", False),
            # Step toggles – accept both "enableAutocrop" and "autoCrop" etc.
            enable_autocrop=_toggle(data, "enableAutocrop", "autoCrop", True),
            enable_denoise=_toggle(data, "enableDenoise", "denoise", True),
            enable_deskew=_toggle(data, "enableDeskew", "deskew", True),
            enable_upscale=_toggle(data, "enableUpscale", "upscale", True),
            enable_brightness_fix=_toggle(data, "enableBrightnessFix", "brightnessFix", True),
            enable_contrast_fix=_toggle(data, "enableContrastFix", "contrastFix", True),
            # Per-step tuning – accept both naming conventions
            denoise_h=_num(data, "denoiseH", "denoiseStrength"),
            clahe_clip=_num(data, "claheClip", "claheClipLimit"),
            force_threshold=data.get("forceThreshold"),
            force_upscale=data.get("forceUpscale"),
            autocrop_margin=_num(data, "autocropMargin", "autoCropMargin", default=10),
            # Diagnosis thresholds
            dark_threshold=data.get("darkThreshold", 140.0),
            noise_threshold=data.get("noiseThreshold", 8.0),
            contrast_threshold=data.get("contrastThreshold", 50.0),
            skew_min_angle=data.get("skewMinAngle", 0.5),
            skew_max_angle=data.get("skewMaxAngle", 15.0),
            upscale_below=data.get("upscaleBelow", 2400),
        )


@dataclass
class PreprocessRequest:
    """Inbound HTTP request body."""

    container_name: str
    blob_path: str
    doc_id: str | None = None
    options: PreprocessOptions = field(default_factory=PreprocessOptions)

    @classmethod
    def from_dict(cls, data: dict) -> PreprocessRequest:
        return cls(
            container_name=data.get("containerName", "raw"),
            blob_path=data["blobPath"],
            doc_id=data.get("docId"),
            options=PreprocessOptions.from_dict(data.get("options")),
        )
