"""Azure Function HTTP-trigger: /api/preprocess

Receives container + blob path from the Logic App (no large payloads),
downloads the source document via Managed Identity, preprocesses every
page, uploads enhanced PNGs + a preprocess.json metadata file, and
returns the metadata as the HTTP response.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import traceback
import uuid
from pathlib import Path

import azure.functions as func
import azure.durable_functions as df

from preprocessing.auto_tuner import AutoTuner
from preprocessing.blob_helper import BlobHelper
from preprocessing.image_processor import ImageProcessor
from preprocessing.metrics import MetricsCalculator
from preprocessing.models import PreprocessOptions, PreprocessRequest
from preprocessing.pdf_handler import PdfHandler

# ── Azure Functions v2 app (DFApp extends FunctionApp with Durable) ──
app = df.DFApp()

logger = logging.getLogger("preprocess")


@app.route(route="preprocess", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def preprocess(req: func.HttpRequest) -> func.HttpResponse:
    """Pre-OCR preprocessing endpoint.

    Request body (JSON)::

        {
            "containerName": "raw",
            "blobPath": "invoices/scan_001.pdf",
            "docId":   "optional-caller-supplied-id",
            "options": { "aggressive": false }
        }

    Response body (JSON): the full ``preprocess.json`` document that is
    also written to the ``artifacts`` container.
    """
    correlation_id = req.headers.get("x-correlation-id", str(uuid.uuid4()))
    doc_id: str | None = None

    # ── Parse request ────────────────────────────────────────────────
    try:
        body = req.get_json()
    except ValueError:
        return _error_response("Invalid JSON body", correlation_id, 400)

    try:
        request = PreprocessRequest.from_dict(body)
        doc_id = request.doc_id or str(uuid.uuid4())

        # ── Skip non-document blobs (e.g. .settings.json) ───────────
        if request.blob_path.endswith('.settings.json'):
            logger.info("Skipping settings blob: %s", request.blob_path)
            return func.HttpResponse(
                json.dumps({
                    "docId": doc_id,
                    "skipped": True,
                    "reason": "Settings metadata file, not a document",
                    "sourceBlobPath": request.blob_path,
                    "aggregated": {"recommendedNextAction": "skip"},
                }),
                mimetype="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        # ── Check for stored UI settings ─────────────────────────────
        # When a user uploads via the dashboard with custom settings,
        # a .settings.json blob is stored alongside the raw file.
        # Stored settings are loaded as base, then body options override.
        storage_url = os.environ["STORAGE_ACCOUNT_URL"]
        blob_helper = BlobHelper(storage_url)

        # Always try to load stored UI settings
        settings_blob = f"settings/{request.blob_path}.settings.json"
        stored_settings: dict | None = None
        try:
            container_client = blob_helper._client.get_container_client("artifacts")
            stored_settings = json.loads(
                container_client.get_blob_client(settings_blob).download_blob().readall()
            )
            logger.info("Found stored UI settings for %s", request.blob_path)
        except Exception:
            pass  # No stored settings

        body_options = body.get("options")
        if stored_settings and body_options:
            # Merge: stored settings as base, body options override
            merged = {**stored_settings, **body_options}
            request.options = PreprocessOptions.from_dict(merged)
            logger.info(
                "Merged stored + body options for %s (aggressive=%s)",
                request.blob_path, request.options.aggressive,
            )
        elif stored_settings:
            request.options = PreprocessOptions.from_dict(stored_settings)
            logger.info(
                "Loaded stored UI settings for %s (aggressive=%s)",
                request.blob_path, request.options.aggressive,
            )
        elif body_options:
            request.options = PreprocessOptions.from_dict(body_options)
            logger.info(
                "Using body options for %s (aggressive=%s)",
                request.blob_path, request.options.aggressive,
            )
        # else: defaults from PreprocessRequest.from_dict already applied

        logger.info(
            "START | docId=%s | blob=%s/%s | aggressive=%s | autocrop=%s | cid=%s",
            doc_id,
            request.container_name,
            request.blob_path,
            request.options.aggressive,
            request.options.enable_autocrop,
            correlation_id,
        )

        # ── Initialise helpers ───────────────────────────────────────
        opts = request.options
        processor = ImageProcessor(
            aggressive=opts.aggressive,
            denoise_h=opts.denoise_h,
            clahe_clip=opts.clahe_clip,
            force_threshold=opts.force_threshold,
            force_upscale=opts.force_upscale,
            enable_autocrop=opts.enable_autocrop,
            autocrop_margin=opts.autocrop_margin,
            dark_threshold=opts.dark_threshold,
            noise_threshold=opts.noise_threshold,
            contrast_threshold=opts.contrast_threshold,
            skew_min_angle=opts.skew_min_angle,
            skew_max_angle=opts.skew_max_angle,
            upscale_below=opts.upscale_below,
            enable_denoise=opts.enable_denoise,
            enable_deskew=opts.enable_deskew,
            enable_upscale=opts.enable_upscale,
            enable_brightness_fix=opts.enable_brightness_fix,
            enable_contrast_fix=opts.enable_contrast_fix,
        )
        auto_tuner = AutoTuner(opts)
        pdf_handler = PdfHandler()
        metrics_calc = MetricsCalculator()

        with tempfile.TemporaryDirectory() as tmp_dir:
            # ── Download source blob ─────────────────────────────────
            suffix = Path(request.blob_path).suffix or ".bin"
            local_src = os.path.join(tmp_dir, f"source{suffix}")
            blob_helper.download(request.container_name, request.blob_path, local_src)

            # ── Resolve page images ──────────────────────────────────
            if pdf_handler.is_pdf(local_src):
                page_images = pdf_handler.split_to_images(local_src, tmp_dir)
                logger.info("PDF → %d page(s) | docId=%s", len(page_images), doc_id)
            else:
                page_images = [local_src]

            # ── Process each page (parallel when multi-page) ────────
            from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

            def _process_single_page(idx: int, page_path: str) -> dict:
                """Process one page: upload original, auto-tune, quality-gate, upload enhanced."""
                _enhanced_path = os.path.join(tmp_dir, f"page-{idx}.png")

                # Save original page image before enhancement
                _orig_blob = f"original/{doc_id}/page-{idx}.png"
                try:
                    with open(page_path, "rb") as orig_f:
                        blob_helper.upload_bytes(
                            orig_f.read(), "artifacts", _orig_blob,
                            content_type="image/png",
                        )
                except Exception:
                    logger.warning("Failed to save original page %d for %s", idx, doc_id)

                # ── Check if original is already clean enough ────────
                _mc = MetricsCalculator()
                skip_processing, _orig_m = _mc.is_already_clean(page_path)

                if skip_processing and not request.options.aggressive:
                    shutil.copy2(page_path, _enhanced_path)
                    processor.ensure_size_limit(_enhanced_path)
                    logger.info(
                        "Page %d SKIPPED preprocessing (already clean) | docId=%s",
                        idx, doc_id,
                    )
                    _blob_dest = f"enhanced/{doc_id}/page-{idx}.png"
                    blob_helper.upload(
                        _enhanced_path, "artifacts", _blob_dest,
                        content_type="image/png",
                    )
                    return {
                        "pageNumber": idx,
                        "enhancedBlobPath": _blob_dest,
                        "preprocessingSkipped": True,
                        **_orig_m,
                        "enhancedBlurScore": _orig_m.get("blurScore", 0),
                        "enhancedOcrReadinessScore": _orig_m.get("ocrReadinessScore", 0),
                    }

                # ── Multi-pass auto-tuning ────────────────────────────
                # Each thread gets its own AutoTuner to avoid shared state
                _tuner = AutoTuner(opts)
                try:
                    tune_result = _tuner.tune(
                        page_path, _enhanced_path, tmp_dir, page_idx=idx,
                    )
                    enhancement_report = tune_result["enhancement"]
                except Exception as proc_err:
                    logger.warning(
                        "Page %d processing failed | docId=%s | err=%s",
                        idx, doc_id, proc_err,
                    )
                    return {
                        "pageNumber": idx,
                        "enhancedBlobPath": None,
                        "blurScore": 0.0,
                        "estimatedDpi": 0,
                        "redactionPercent": 0.0,
                        "ocrReadinessScore": 0.0,
                        "failureCode": "F07",
                        "error": str(proc_err),
                    }

                # Metrics: compute on ORIGINAL page for quality decision
                _orig_m = _mc.calculate(page_path, page_path)
                enhanced_metrics = tune_result["winnerMetrics"]

                # ── Quality gate: keep original if enhancement made things worse ──
                orig_blur = _orig_m.get("blurScore", 0)
                enh_blur  = enhanced_metrics.get("blurScore", 0)
                orig_ocr  = _orig_m.get("ocrReadinessScore", 0)
                enh_ocr   = enhanced_metrics.get("ocrReadinessScore", 0)
                orig_redact = _orig_m.get("redactionPercent", 0)
                enh_redact  = enhanced_metrics.get("redactionPercent", 0)

                enhancement_worse = (
                    enh_blur < orig_blur
                    or enh_ocr < orig_ocr
                    or enh_redact > orig_redact + 1.0
                )

                if enhancement_worse:
                    shutil.copy2(page_path, _enhanced_path)
                    processor.ensure_size_limit(_enhanced_path)
                    logger.warning(
                        "Page %d ROLLED BACK to original — all %d presets degraded quality "
                        "(best='%s', blur: %.1f→%.1f, ocr: %.3f→%.3f, redact: %.1f%%→%.1f%%) | docId=%s",
                        idx,
                        enhancement_report.get("autoTuning", {}).get("presetsTriedCount", 1),
                        tune_result.get("winnerPreset", "unknown"),
                        orig_blur, enh_blur, orig_ocr, enh_ocr,
                        orig_redact, enh_redact, doc_id,
                    )
                    page_metrics = _orig_m
                    page_metrics["enhancedBlurScore"] = orig_blur
                    page_metrics["enhancedOcrReadinessScore"] = orig_ocr
                    enhancement_report["rolledBack"] = True
                    enhancement_report["rollbackReason"] = (
                        f"All {enhancement_report.get('autoTuning', {}).get('presetsTriedCount', 1)} "
                        f"presets degraded quality — best was '{tune_result.get('winnerPreset', 'unknown')}': "
                        f"blur: {orig_blur:.1f}→{enh_blur:.1f}, "
                        f"ocr: {orig_ocr:.3f}→{enh_ocr:.3f}, "
                        f"redact: {orig_redact:.1f}%→{enh_redact:.1f}%"
                    )
                else:
                    page_metrics = _orig_m
                    page_metrics["enhancedBlurScore"] = enh_blur
                    page_metrics["enhancedOcrReadinessScore"] = enh_ocr

                detection = tune_result.get("last_detection")
                region_info = detection.summary() if detection else None

                _blob_dest = f"enhanced/{doc_id}/page-{idx}.png"
                blob_helper.upload(
                    _enhanced_path, "artifacts", _blob_dest,
                    content_type="image/png",
                )

                _page_result = {
                    "pageNumber": idx,
                    "enhancedBlobPath": _blob_dest,
                    **page_metrics,
                    "enhancement": enhancement_report,
                }
                if region_info:
                    _page_result["regionDetection"] = region_info
                return _page_result

            # Fan-out: process pages in parallel (up to 3 workers to
            # balance CPU load vs. throughput — OpenCV releases the GIL).
            _max_pp_workers = min(len(page_images), 3)
            if _max_pp_workers <= 1:
                # Single page — run inline, no thread overhead
                page_results = [
                    _process_single_page(1, page_images[0])
                ]
            else:
                page_results_map: dict[int, dict] = {}
                with ThreadPoolExecutor(max_workers=_max_pp_workers) as _pp_pool:
                    _pp_futures = {
                        _pp_pool.submit(_process_single_page, idx, path): idx
                        for idx, path in enumerate(page_images, start=1)
                    }
                    for fut in _as_completed(_pp_futures):
                        res = fut.result()
                        page_results_map[res["pageNumber"]] = res
                # Return results in page order
                page_results = [
                    page_results_map[i]
                    for i in sorted(page_results_map.keys())
                ]

            # ── Aggregate ────────────────────────────────────────────
            aggregated = metrics_calc.aggregate(page_results)

            # When running in aggressive mode, the enhanced image is what gets
            # OCR'd.  If the *enhanced* metrics are good, there is no point in
            # recommending another retry – upgrade the recommendation instead.
            if opts.aggressive and aggregated.get("recommendedNextAction") == "retry_stronger":
                valid = [p for p in page_results if p.get("failureCode") != "F07"]
                if valid:
                    avg_enh_blur = sum(p.get("enhancedBlurScore", 0) for p in valid) / len(valid)
                    avg_enh_ocr  = sum(p.get("enhancedOcrReadinessScore", 0) for p in valid) / len(valid)
                    if avg_enh_ocr >= 0.50 or avg_enh_blur >= 120:
                        aggregated["recommendedNextAction"] = "run_doc_intel"
                        aggregated["failureCode"] = None
                        logger.info(
                            "Aggressive mode upgraded action → run_doc_intel "
                            "(enhanced blur=%.1f, enhanced ocr=%.3f) | docId=%s",
                            avg_enh_blur, avg_enh_ocr, doc_id,
                        )

            result = {
                "docId": doc_id,
                "correlationId": correlation_id,
                "sourceContainer": request.container_name,
                "sourceBlobPath": request.blob_path,
                "pages": page_results,
                "aggregated": aggregated,
            }

            # Upload preprocess.json → artifacts/enhanced/{docId}/preprocess.json
            metadata_json = json.dumps(result, indent=2)
            blob_helper.upload_bytes(
                metadata_json.encode("utf-8"),
                "artifacts",
                f"enhanced/{doc_id}/preprocess.json",
                content_type="application/json",
            )

            logger.info(
                "DONE | docId=%s | action=%s | failureCode=%s | pages=%d | cid=%s",
                doc_id,
                aggregated["recommendedNextAction"],
                aggregated.get("failureCode"),
                len(page_results),
                correlation_id,
            )

            return func.HttpResponse(
                metadata_json,
                status_code=200,
                mimetype="application/json",
            )

    except KeyError as ke:
        return _error_response(f"Missing required field: {ke}", correlation_id, 400)

    except Exception:
        logger.exception("Unhandled error | docId=%s | cid=%s", doc_id, correlation_id)
        return _error_response(
            "Internal processing error",
            correlation_id,
            500,
            doc_id=doc_id,
            detail=traceback.format_exc(),
        )


# ── Helpers ──────────────────────────────────────────────────────────

def _error_response(
    message: str,
    correlation_id: str,
    status_code: int,
    *,
    doc_id: str | None = None,
    detail: str | None = None,
) -> func.HttpResponse:
    body: dict = {
        "error": message,
        "correlationId": correlation_id,
    }
    if doc_id:
        body["docId"] = doc_id
    if detail:
        body["detail"] = detail
    return func.HttpResponse(
        json.dumps(body),
        status_code=status_code,
        mimetype="application/json",
    )


# ═══════════════════════════════════════════════════════════════════════
# Dashboard API endpoints
# ═══════════════════════════════════════════════════════════════════════

def _get_blob_helper() -> BlobHelper:
    return BlobHelper(os.environ["STORAGE_ACCOUNT_URL"])


import re as _re

# Emoji constants (non-raw strings so \U escape is processed to actual emoji)
_EMOJI_TABLE = "\U0001F4CB"   # 📋
_EMOJI_VISION = "\U0001F4CA"  # 📊

# Marker HTML — elegant glass-morphism style
_TABLE_DIV_OPEN = (
    '<section class="marker-table" style="display:block;position:relative;'
    'background:rgba(30,58,138,0.25);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);'
    'border:1px solid rgba(96,165,250,0.25);border-top:3px solid #3b82f6;'
    'padding:14px 20px;margin:20px 0 10px;border-radius:12px;'
    'box-shadow:0 4px 24px rgba(59,130,246,0.15),inset 0 1px 0 rgba(255,255,255,0.05);'
    'color:#e2e8f0;font-weight:500">'
    '<span class="marker-badge marker-badge-table" style="display:inline-flex;align-items:center;gap:6px;'
    'background:linear-gradient(135deg,#2563eb,#3b82f6);color:#fff;padding:3px 10px 3px 8px;'
    'border-radius:20px;font-size:10px;font-weight:700;letter-spacing:1.5px;margin-left:10px;'
    'text-transform:uppercase;box-shadow:0 2px 8px rgba(37,99,235,0.4)">'
    '\U0001F4CB TABLE</span> '
)
_TABLE_DIV_CLOSE = '</section>'
_VISION_DIV_OPEN = (
    '<section class="marker-vision" style="display:block;position:relative;'
    'background:rgba(91,33,182,0.2);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);'
    'border:1px solid rgba(167,139,250,0.25);border-top:3px solid #8b5cf6;'
    'padding:14px 20px;margin:20px 0 10px;border-radius:12px;'
    'box-shadow:0 4px 24px rgba(139,92,246,0.15),inset 0 1px 0 rgba(255,255,255,0.05);'
    'color:#e2e8f0;font-weight:500">'
    '<span class="marker-badge marker-badge-vision" style="display:inline-flex;align-items:center;gap:6px;'
    'background:linear-gradient(135deg,#7c3aed,#8b5cf6);color:#fff;padding:3px 10px 3px 8px;'
    'border-radius:20px;font-size:10px;font-weight:700;letter-spacing:1.5px;margin-left:10px;'
    'text-transform:uppercase;box-shadow:0 2px 8px rgba(124,58,237,0.4)">'
    '\U0001F4CA VISION</span> '
)
_VISION_DIV_CLOSE = '</section>'


def _markdown_to_html(md: str) -> str:
    """Server-side markdown→HTML with data-source marker styling.

    Converts summary markdown to styled HTML so markers display correctly
    regardless of client-side regex/emoji support.
    """
    lines = md.split("\n")
    html_lines: list[str] = []
    _block_tags = ("<h1", "<h2", "<h3", "<div", "<li", "<section")
    for line in lines:
        # Empty lines → paragraph break
        if not line.strip():
            html_lines.append("<br>")
            continue

        # Headings
        if line.startswith("### "):
            html_lines.append(f'<h3 style="font-size:1rem;font-weight:700;margin:16px 0 8px">{line[4:]}</h3>')
            continue
        elif line.startswith("## "):
            html_lines.append(f'<h2 style="font-size:1.125rem;font-weight:700;margin:24px 0 8px">{line[3:]}</h2>')
            continue
        elif line.startswith("# "):
            html_lines.append(f'<h1 style="font-size:1.25rem;font-weight:700;margin:24px 0 12px">{line[2:]}</h1>')
            continue

        # Bold + italic
        line = _re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', line)
        line = _re.sub(r'\*(.+?)\*', r'<em>\1</em>', line)

        # List items
        if line.startswith("- "):
            line = f'<li style="margin-right:20px;list-style:disc">{line[2:]}</li>'

        # Data-source markers: 📋 TABLE and 📊 VISION
        if _EMOJI_TABLE in line:
            line = _re.sub(
                '(' + _EMOJI_TABLE + r'\s*<strong>.*?</strong>)',
                _TABLE_DIV_OPEN + r'\1' + _TABLE_DIV_CLOSE,
                line,
            )
        if _EMOJI_VISION in line:
            line = _re.sub(
                '(' + _EMOJI_VISION + r'\s*<strong>.*?</strong>)',
                _VISION_DIV_OPEN + r'\1' + _VISION_DIV_CLOSE,
                line,
            )

        # Add <br> after non-block text lines
        if not any(line.lstrip().startswith(tag) for tag in _block_tags):
            line = line + "<br>"

        html_lines.append(line)

    return "\n".join(html_lines)


def _inject_marker_html_into_summary(summary_text: str) -> str:
    """Replace emoji+bold markers in raw markdown with styled HTML divs.

    This ensures markers are visible even when the client-side JS doesn't
    run the emoji regex (older cached bundles, etc.).
    Uses both inline styles AND CSS classes for maximum compatibility.
    """
    # Inline-style fallback for old React bundles that don't have marker CSS classes
    _TABLE_INLINE = (
        '<div class="marker-table" style="display:block;background:linear-gradient(135deg,#1e3a5f,#1e40af);'
        'border-left:5px solid #3b82f6;padding:10px 16px;margin:14px 0 6px;border-radius:8px;'
        'box-shadow:0 0 12px rgba(59,130,246,0.35);color:#e2e8f0">'
        '<span class="marker-badge marker-badge-table" style="display:inline-block;background:#3b82f6;color:#fff;'
        'padding:3px 10px;border-radius:5px;font-size:11px;font-weight:800;letter-spacing:1.5px;margin-left:10px">'
        'TABLE</span> '
    )
    _VISION_INLINE = (
        '<div class="marker-vision" style="display:block;background:linear-gradient(135deg,#2d1b4e,#5b21b6);'
        'border-left:5px solid #8b5cf6;padding:10px 16px;margin:14px 0 6px;border-radius:8px;'
        'box-shadow:0 0 12px rgba(139,92,246,0.35);color:#e2e8f0">'
        '<span class="marker-badge marker-badge-vision" style="display:inline-block;background:#8b5cf6;color:#fff;'
        'padding:3px 10px;border-radius:5px;font-size:11px;font-weight:800;letter-spacing:1.5px;margin-left:10px">'
        'VISION</span> '
    )
    result = _re.sub(
        _EMOJI_TABLE + r'\s*\*\*(.*?)\*\*',
        _TABLE_INLINE + _EMOJI_TABLE + r' <strong>\1</strong></div>',
        summary_text,
    )
    result = _re.sub(
        _EMOJI_VISION + r'\s*\*\*(.*?)\*\*',
        _VISION_INLINE + _EMOJI_VISION + r' <strong>\1</strong></div>',
        result,
    )
    return result


@app.route(route="documents", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def list_documents(req: func.HttpRequest) -> func.HttpResponse:
    """List all processed documents by scanning artifacts/enhanced/*/preprocess.json."""
    try:
        blob_helper = _get_blob_helper()
        container_client = blob_helper._client.get_container_client("artifacts")

        documents: list[dict] = []
        for blob in container_client.list_blobs(name_starts_with="enhanced/"):
            if blob.name.endswith("/preprocess.json"):
                # Download and parse preprocess.json
                blob_client = container_client.get_blob_client(blob.name)
                data = json.loads(blob_client.download_blob().readall())
                doc_id = data.get("docId", "unknown")

                # Check if OCR result exists
                ocr_exists = False
                try:
                    outputs_client = blob_helper._client.get_container_client("outputs")
                    outputs_client.get_blob_client(f"{doc_id}_ocr_result.json").get_blob_properties()
                    ocr_exists = True
                except Exception:
                    pass

                # Check if failure exists
                failure = None
                try:
                    outputs_client = blob_helper._client.get_container_client("outputs")
                    fail_blob = outputs_client.get_blob_client(f"{doc_id}_failure.json")
                    failure = json.loads(fail_blob.download_blob().readall())
                except Exception:
                    pass

                # Check if summary exists
                summary_exists = False
                summary_vision = False
                try:
                    outputs_client = blob_helper._client.get_container_client("outputs")
                    summary_data = json.loads(
                        outputs_client.get_blob_client(f"{doc_id}_summary.json")
                        .download_blob().readall()
                    )
                    summary_exists = True
                    summary_vision = summary_data.get("visionUsed", False)
                except Exception:
                    pass

                documents.append({
                    "docId": doc_id,
                    "sourceBlobPath": data.get("sourceBlobPath", ""),
                    "pageCount": len(data.get("pages", [])),
                    "aggregated": data.get("aggregated", {}),
                    "ocrCompleted": ocr_exists,
                    "summaryCompleted": summary_exists,
                    "visionUsed": summary_vision,
                    "failure": failure,
                    "timestamp": blob.last_modified.isoformat() if blob.last_modified else None,
                })

        documents.sort(key=lambda d: d.get("timestamp", ""), reverse=True)

        return func.HttpResponse(
            json.dumps(documents, default=str),
            mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    except Exception:
        logger.exception("Error listing documents")
        return func.HttpResponse(
            json.dumps({"error": "Failed to list documents"}),
            status_code=500,
            mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache, no-store, must-revalidate"},
        )


@app.route(route="documents/{docId}", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def get_document(req: func.HttpRequest) -> func.HttpResponse:
    """Get full details for a single document: preprocess metadata + OCR result."""
    doc_id = req.route_params.get("docId")
    try:
        blob_helper = _get_blob_helper()

        # Load preprocess.json
        container_client = blob_helper._client.get_container_client("artifacts")
        preprocess_blob = container_client.get_blob_client(f"enhanced/{doc_id}/preprocess.json")
        preprocess_data = json.loads(preprocess_blob.download_blob().readall())

        # Try to load OCR result
        ocr_data = None
        try:
            outputs_client = blob_helper._client.get_container_client("outputs")
            ocr_blob = outputs_client.get_blob_client(f"{doc_id}_ocr_result.json")
            ocr_data = json.loads(ocr_blob.download_blob().readall())
        except Exception:
            pass

        # Try to load failure
        failure_data = None
        try:
            outputs_client = blob_helper._client.get_container_client("outputs")
            fail_blob = outputs_client.get_blob_client(f"{doc_id}_failure.json")
            failure_data = json.loads(fail_blob.download_blob().readall())
        except Exception:
            pass

        # Try to load summary
        summary_data = None
        try:
            outputs_client = blob_helper._client.get_container_client("outputs")
            sum_blob = outputs_client.get_blob_client(f"{doc_id}_summary.json")
            summary_data = json.loads(sum_blob.download_blob().readall())
        except Exception:
            pass

        # Pre-render summary markdown → HTML server-side (bypasses client-side regex issues)
        if summary_data and summary_data.get("summary"):
            original_md = summary_data["summary"]
            # Generate fully-styled HTML for both UIs (React uses summaryHtml, dashboard uses it too)
            summary_data["summaryHtml"] = _markdown_to_html(original_md)
            # Keep raw summary as-is (original markdown) — dashboard's renderMarkdown()
            # calls escapeHtml() first, so injecting HTML here would break it.

        result = {
            "preprocess": preprocess_data,
            "ocr": ocr_data,
            "failure": failure_data,
            "summary": summary_data,
        }

        return func.HttpResponse(
            json.dumps(result, default=str),
            mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    except Exception:
        logger.exception("Error getting document %s", doc_id)
        return func.HttpResponse(
            json.dumps({"error": f"Document {doc_id} not found"}),
            status_code=404,
            mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache, no-store, must-revalidate"},
        )


@app.route(route="image/{docId}/{page}", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def get_image(req: func.HttpRequest) -> func.HttpResponse:
    """Serve an enhanced page image."""
    doc_id = req.route_params.get("docId")
    page = req.route_params.get("page")
    try:
        blob_helper = _get_blob_helper()
        container_client = blob_helper._client.get_container_client("artifacts")
        blob_client = container_client.get_blob_client(f"enhanced/{doc_id}/page-{page}.png")
        image_data = blob_client.download_blob().readall()

        return func.HttpResponse(
            image_data,
            mimetype="image/png",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=3600"},
        )
    except Exception:
        return func.HttpResponse("Image not found", status_code=404)


@app.route(route="image-original/{docId}/{page}", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def get_original_image(req: func.HttpRequest) -> func.HttpResponse:
    """Serve the original (pre-enhancement) page image.

    First checks for a cached original PNG at artifacts/original/{docId}/page-{page}.png.
    If not found, downloads the raw PDF, renders the requested page, caches it, and returns it.
    """
    doc_id = req.route_params.get("docId")
    page = req.route_params.get("page")
    try:
        blob_helper = _get_blob_helper()
        artifacts_client = blob_helper._client.get_container_client("artifacts")

        # Try cached original first
        cache_path = f"original/{doc_id}/page-{page}.png"
        try:
            cached = artifacts_client.get_blob_client(cache_path)
            image_data = cached.download_blob().readall()
            return func.HttpResponse(
                image_data,
                mimetype="image/png",
                headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=3600"},
            )
        except Exception:
            pass

        # Look up the source blob path from preprocess.json
        pp_blob = artifacts_client.get_blob_client(f"enhanced/{doc_id}/preprocess.json")
        pp_data = json.loads(pp_blob.download_blob().readall())
        src_container = pp_data.get("sourceContainer", "raw")
        src_path = pp_data.get("sourceBlobPath", "")

        if not src_path:
            return func.HttpResponse("Source blob path unknown", status_code=404)

        # Download the raw file and render the page
        with tempfile.TemporaryDirectory() as tmp_dir:
            suffix = Path(src_path).suffix or ".bin"
            local_src = os.path.join(tmp_dir, f"source{suffix}")
            blob_helper.download(src_container, src_path, local_src)

            pdf_handler = PdfHandler()
            if pdf_handler.is_pdf(local_src):
                page_images = pdf_handler.split_to_images(local_src, tmp_dir)
                page_idx = int(page) - 1
                if page_idx < 0 or page_idx >= len(page_images):
                    return func.HttpResponse("Page out of range", status_code=404)
                page_file = page_images[page_idx]
            else:
                page_file = local_src

            with open(page_file, "rb") as f:
                image_data = f.read()

            # Cache for future requests
            try:
                blob_helper.upload_bytes(image_data, "artifacts", cache_path, content_type="image/png")
            except Exception:
                logger.warning("Failed to cache original image for %s page %s", doc_id, page)

        return func.HttpResponse(
            image_data,
            mimetype="image/png",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=3600"},
        )
    except Exception:
        logger.exception("Error getting original image %s page %s", doc_id, page)
        return func.HttpResponse("Image not found", status_code=404)


# ═══════════════════════════════════════════════════════════════════════
# Helper functions for prompt, examples, and quality
# ═══════════════════════════════════════════════════════════════════════

def _load_active_prompt(blob_helper: BlobHelper) -> str:
    """Load the active prompt version from blob storage.

    Falls back to the built-in MEDICAL_SYSTEM_PROMPT if no external
    prompt is configured or available.
    """
    active_version = os.environ.get("ACTIVE_PROMPT_VERSION", "")
    if not active_version:
        return MEDICAL_SYSTEM_PROMPT

    try:
        container_client = blob_helper._client.get_container_client("artifacts")
        blob_client = container_client.get_blob_client(f"prompts/{active_version}.txt")
        prompt_text = blob_client.download_blob().readall().decode("utf-8")
        logger.info("Loaded external prompt version: %s (%d chars)", active_version, len(prompt_text))
        return prompt_text
    except Exception:
        logger.warning(
            "Failed to load prompt version '%s' – falling back to built-in",
            active_version,
        )
        return MEDICAL_SYSTEM_PROMPT


def _load_few_shot_examples(
    blob_helper: BlobHelper,
    max_examples: int = 5,
    document_type: str = "",
    input_text_for_similarity: str = "",
) -> list[dict]:
    """Load few-shot examples from artifacts/examples/ as chat messages.

    Selection strategy (in priority order):
    1. Golden examples matching the document type
    2. Golden examples (any type)
    3. Non-golden examples matching the document type
    4. Remaining examples by recency

    If *input_text_for_similarity* is provided and Azure OpenAI embeddings
    are configured, examples are re-ranked by cosine similarity to the
    incoming document so the most relevant ones are chosen.

    Returns a list of alternating user/assistant message dicts that can
    be injected between the system message and the real user query.
    """
    try:
        container_client = blob_helper._client.get_container_client("artifacts")

        # ── Collect all examples with metadata ───────────────────
        example_metas: list[dict] = []
        seen_ids: set[str] = set()
        for blob in container_client.list_blobs(name_starts_with="examples/"):
            parts = blob.name.split("/")
            if len(parts) >= 3 and parts[2] == "input.txt":
                eid = parts[1]
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)
                # Try to load metadata
                meta: dict = {"exampleId": eid, "isGolden": False, "documentType": "general", "tags": []}
                try:
                    meta_blob = container_client.get_blob_client(f"examples/{eid}/metadata.json")
                    stored_meta = json.loads(meta_blob.download_blob().readall())
                    meta.update(stored_meta)
                    meta.setdefault("isGolden", False)
                    meta.setdefault("documentType", meta.get("category", "general"))
                    meta.setdefault("tags", [])
                except Exception:
                    pass
                example_metas.append(meta)

        if not example_metas:
            return []

        # ── Score & rank examples ────────────────────────────────
        def _score(m: dict) -> tuple:
            """Higher score = higher priority. Returns tuple for sorting."""
            is_golden = 1 if m.get("isGolden") else 0
            type_match = 1 if (document_type and m.get("documentType", "").lower() == document_type.lower()) else 0
            # Use createdAt as tiebreaker (newer = higher)
            created = m.get("createdAt", "")
            return (is_golden, type_match, created)

        example_metas.sort(key=_score, reverse=True)

        # ── Optional: re-rank by embedding similarity ────────────
        if input_text_for_similarity and os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT"):
            try:
                example_metas = _rerank_by_similarity(
                    example_metas, input_text_for_similarity, container_client
                )
            except Exception:
                logger.warning("Embedding re-ranking failed, using priority sort")

        # Take top N
        selected = example_metas[:max_examples]

        # ── Build chat messages from selected examples ───────────
        messages: list[dict] = []
        for meta in selected:
            eid = meta["exampleId"]
            try:
                input_blob = container_client.get_blob_client(f"examples/{eid}/input.txt")
                input_text = input_blob.download_blob().readall().decode("utf-8")

                summary_blob = container_client.get_blob_client(f"examples/{eid}/summary.txt")
                summary_text = summary_blob.download_blob().readall().decode("utf-8")

                # Truncate if very long to avoid token overflow
                if len(input_text) > 3000:
                    input_text = input_text[:3000] + "\n\n[... truncated for few-shot example ...]"

                doc_type_label = meta.get("documentType", "general")
                messages.append({
                    "role": "user",
                    "content": (
                        f"[EXAMPLE – {doc_type_label}] "
                        f"Summarize the following medical document. Output language: Hebrew.\n\n---\n\n{input_text}"
                    ),
                })
                messages.append({
                    "role": "assistant",
                    "content": summary_text,
                })
            except Exception:
                logger.warning("Failed to load few-shot example %s", eid)
                continue

        example_info = {
            "count": len(messages) // 2,
            "goldenCount": sum(1 for m in selected if m.get("isGolden")),
            "typeMatchCount": sum(
                1 for m in selected
                if document_type and m.get("documentType", "").lower() == document_type.lower()
            ),
            "examples": [
                {
                    "exampleId": m.get("exampleId"),
                    "documentType": m.get("documentType", "general"),
                    "isGolden": m.get("isGolden", False),
                    "description": m.get("description", ""),
                }
                for m in selected
                if any(  # only include examples that actually loaded
                    msg.get("content", "").startswith(f"[EXAMPLE")
                    for msg in messages
                    if msg.get("role") == "user"
                )
            ],
        }

        if messages:
            logger.info(
                "Loaded %d few-shot example(s) (golden=%d, type-match=%d) | ids=%s",
                example_info["count"],
                example_info["goldenCount"],
                example_info["typeMatchCount"],
                [e["exampleId"] for e in example_info["examples"]],
            )
        return messages, example_info

    except Exception:
        logger.debug("No few-shot examples available")
        return [], {"count": 0, "goldenCount": 0, "typeMatchCount": 0, "examples": []}


def _rerank_by_similarity(
    example_metas: list[dict],
    query_text: str,
    container_client,
) -> list[dict]:
    """Re-rank examples by cosine similarity to the query text.

    Uses Azure OpenAI embeddings.  Pre-computed embeddings are read from
    ``examples/{id}/embedding.json``; if missing the example keeps its
    original rank position.
    """
    from openai import AzureOpenAI
    import math

    client = AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_KEY"],
        api_version="2024-08-01-preview",
    )
    deployment = os.environ["AZURE_OPENAI_EMBEDDING_DEPLOYMENT"]

    # Embed query (truncate to ~8000 chars to stay within token limits)
    query_snippet = query_text[:8000]
    resp = client.embeddings.create(model=deployment, input=[query_snippet])
    query_vec = resp.data[0].embedding

    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na and nb else 0.0

    scored: list[tuple[float, dict]] = []
    for meta in example_metas:
        eid = meta["exampleId"]
        sim = 0.0
        try:
            emb_blob = container_client.get_blob_client(f"examples/{eid}/embedding.json")
            emb_data = json.loads(emb_blob.download_blob().readall())
            emb_vec = emb_data.get("embedding", [])
            if emb_vec:
                sim = _cosine(query_vec, emb_vec)
        except Exception:
            pass  # no embedding stored – keep sim=0

        # Composite score: golden bonus + type-match bonus + similarity
        golden_bonus = 0.3 if meta.get("isGolden") else 0.0
        scored.append((sim + golden_bonus, meta))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for _, m in scored]


def _store_example_embedding(
    blob_helper: BlobHelper, example_id: str, input_text: str
) -> None:
    """Compute and store an embedding vector for an example's input text.

    Silently skipped if the embedding deployment is not configured.
    """
    deployment = os.environ.get("AZURE_OPENAI_EMBEDDING_DEPLOYMENT")
    if not deployment:
        return
    try:
        from openai import AzureOpenAI

        client = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_KEY"],
            api_version="2024-08-01-preview",
        )
        snippet = input_text[:8000]
        resp = client.embeddings.create(model=deployment, input=[snippet])
        embedding = resp.data[0].embedding
        blob_helper.upload_bytes(
            json.dumps({"embedding": embedding}).encode("utf-8"),
            "artifacts",
            f"examples/{example_id}/embedding.json",
            content_type="application/json",
        )
        logger.info("Stored embedding for example %s (%d dims)", example_id, len(embedding))
    except Exception:
        logger.warning("Failed to store embedding for example %s", example_id)


# ── Smart figure filtering ─────────────────────────────────────────
# Doc Intelligence often flags headers, logos, stamps, and seals as
# "figures". We only trigger the expensive Vision model (gpt-4o) for
# real charts, graphs, or images that are large enough to contain
# meaningful visual data.
_MIN_FIGURE_PAGE_FRACTION = 0.05   # figure must cover ≥5% of page area
_MAX_FIGURE_ASPECT_RATIO = 5.0     # width/height (or inverse) must be <5


def _filter_real_figures(
    ocr_figures: list[dict],
    ocr_pages: list[dict],
) -> list[dict]:
    """Return only figures that are likely real charts/graphs.

    Criteria (any → keep):
      1. Figure has a caption  → always keep
      2. Figure area ≥ 5 % of its page  AND  aspect ratio < 5:1
    """
    if not ocr_figures:
        return []

    # Build page-dimension lookup: pageNumber → (width, height)
    page_dims: dict[int, tuple[float, float]] = {}
    for p in ocr_pages or []:
        pn = p.get("pageNumber")
        w = p.get("width", 0)
        h = p.get("height", 0)
        if pn and w and h:
            page_dims[pn] = (float(w), float(h))

    kept: list[dict] = []
    for fig in ocr_figures:
        # Always keep captioned figures
        cap = fig.get("caption")
        if cap and cap.get("content", "").strip():
            kept.append(fig)
            logger.info("Figure kept: has caption '%s'", cap["content"][:60])
            continue

        # Check bounding regions for size / aspect ratio
        dominated = True  # assume too small until proven otherwise
        for region in fig.get("boundingRegions", []):
            pn = region.get("pageNumber")
            poly = region.get("polygon", [])
            if not pn or len(poly) < 8:
                continue
            page_w, page_h = page_dims.get(pn, (0, 0))
            if not page_w or not page_h:
                continue

            xs = [poly[j] for j in range(0, len(poly), 2)]
            ys = [poly[j] for j in range(1, len(poly), 2)]
            fig_w = max(xs) - min(xs)
            fig_h = max(ys) - min(ys)
            if fig_w <= 0 or fig_h <= 0:
                continue

            page_area = page_w * page_h
            fig_area = fig_w * fig_h
            fraction = fig_area / page_area
            aspect = max(fig_w / fig_h, fig_h / fig_w)

            if fraction >= _MIN_FIGURE_PAGE_FRACTION and aspect < _MAX_FIGURE_ASPECT_RATIO:
                dominated = False
                logger.info(
                    "Figure kept: page %d, area %.1f%% of page, aspect %.1f:1",
                    pn, fraction * 100, aspect,
                )
                break
            else:
                logger.info(
                    "Figure skipped: page %d, area %.1f%% of page (min %.0f%%), aspect %.1f:1 (max %.0f:1)",
                    pn, fraction * 100, _MIN_FIGURE_PAGE_FRACTION * 100,
                    aspect, _MAX_FIGURE_ASPECT_RATIO,
                )

        if not dominated:
            kept.append(fig)

    logger.info("Figure filtering: %d → %d kept", len(ocr_figures), len(kept))
    return kept


def _assess_summary_quality(summary_text: str, ocr_text: str) -> dict:
    """Assess the quality of a generated summary.

    Returns a dict with quality indicators and flags.
    """
    summary_stripped = summary_text.strip()
    ocr_stripped = ocr_text.strip()

    # Quality signals
    summary_len = len(summary_stripped)
    ocr_len = len(ocr_stripped)
    ratio = summary_len / max(ocr_len, 1)

    # Check for known poor-quality patterns
    no_medical_data = any(phrase in summary_stripped.lower() for phrase in [
        "no medical data identified",
        "no medical data",
        "לא זוהה מידע רפואי",
        "אין מידע רפואי",
        "no clinical content",
    ])

    too_short = summary_len < 100
    mostly_gaps = summary_stripped.count("[Unclear") > 5
    admin_only = any(phrase in summary_stripped.lower() for phrase in [
        "administrative only",
        "administrative document",
        "מסמך מנהלתי",
    ])

    # Determine overall quality grade
    if no_medical_data:
        grade = "no_medical_content"
        failure_code = "F05"
    elif too_short:
        grade = "poor"
        failure_code = "F08"
    elif mostly_gaps:
        grade = "low"
        failure_code = None
    elif admin_only:
        grade = "administrative"
        failure_code = "F05"
    elif ratio < 0.02:
        grade = "poor"
        failure_code = "F08"
    elif ratio < 0.05:
        grade = "fair"
        failure_code = None
    else:
        grade = "good"
        failure_code = None

    result = {
        "grade": grade,
        "summaryLength": summary_len,
        "ocrLength": ocr_len,
        "compressionRatio": round(ratio, 4),
        "hasUnclearMarkers": summary_stripped.count("[Unclear") > 0,
        "unclearCount": summary_stripped.count("[Unclear"),
    }

    if failure_code:
        from preprocessing.metrics import FAILURE_DESCRIPTIONS
        result["failureCode"] = failure_code
        desc = FAILURE_DESCRIPTIONS.get(failure_code)
        if desc:
            result["failureDescription"] = desc

    return result


# ═══════════════════════════════════════════════════════════════════════
# Clinical Summarization endpoint
# ═══════════════════════════════════════════════════════════════════════

MEDICAL_SYSTEM_PROMPT = """You are a clinical medical document summarization agent.

Your task is to summarize medical documents that originate from OCR output.
The OCR text may contain:
- Spelling errors
- Broken sentences
- Mixed Hebrew and English
- Incorrect punctuation
- Duplicated fragments
- Scanning artifacts

You must carefully reconstruct meaning before summarizing.

=====================================
PROCESSING INSTRUCTIONS
=====================================

Step 1 – OCR Cleanup (Internal reasoning)
- Fix obvious OCR mistakes.
- Merge broken lines into logical sentences.
- Remove duplicated fragments.
- Infer medical terminology if OCR distorted it.
- Normalize units (mg, mmHg, %, dates).
- Detect language (Hebrew / English / mixed).

Step 2 – Clinical Structuring
Extract and organize the information into:

1. Patient Demographics (if exists)
2. Medical History
3. Active Diagnoses
4. Medications (name + dose if available)
5. Procedures / Surgeries
6. Imaging Findings
7. Lab Results (include values from tables if detected)
8. Functional Limitations
9. Mental Health Notes (if relevant)
10. Physician Assessment
11. Recommendations / Restrictions
12. Structured Tables / Forms Data

Step 2b – Table & Form Processing
If structured tables are provided (from Document Intelligence layout analysis):
- Parse rows/columns and map headers to values.
- For lab results tables → extract test name, value, reference range, flag.
- For medication tables → extract drug name, dose, frequency.
- For vital signs tables → extract parameter and reading.
- For form fields → map field labels to filled values.
- Present tabular data in a clear structured format in the summary.
- If a table has no clear headers, describe its content.
- **Mark data extracted from structured tables** by starting the relevant line or
  sub-section with the prefix "📋 " (clipboard emoji). For example:
  📋 **Lab Results (from table):** Hemoglobin 14.2 g/dL …

Step 2c – Figures & Charts
If figures/charts are detected:
- Note their presence in the summary.
- If caption text is available, include the caption.
- If page images are attached (marked "[Page N image – figure/chart for visual analysis]"):
  • Visually analyze the chart, graph, or figure in detail.
  • Extract axis labels, data points, trends, and numeric values.
  • Describe what the visual shows in its clinical context.
  • Include any extracted numeric data in the structured summary.
  • **Mark data extracted from visual analysis** by starting the relevant line or
    sub-section with the prefix "📊 " (chart emoji) and bold the heading. For example:
    📊 **EKG Interpretation (visual analysis):** Sinus rhythm, 72 BPM …
- If no page image is attached for a figure, state that chart data requires visual review.

Step 3 – Summarization Rules
- Be concise but medically accurate.
- Do NOT hallucinate missing data.
- If information is unclear → mark as: [Unclear due to OCR]
- If critical medical data is missing → explicitly say so.
- Preserve clinical terminology.
- Do not exaggerate severity.
- Keep neutral tone.

Step 4 – Risk Flagging
If the document contains:
- Suicidal ideation
- Severe psychiatric disorder
- Active cancer
- Cardiac risk
- Neurological deficits
- Severe functional impairment

Add a section:
"⚠ Clinical Risk Indicators"

=====================================
OUTPUT FORMAT (STRICT)
=====================================

## סיכום רפואי

### 1. פרטי המטופל
...

### 2. מצבים רפואיים
...

### 3. תרופות
...

### 4. מצב תפקודי
...

### 5. הערכה קלינית
...

### 6. המלצות
...

### 7. פערי מידע / אי-ודאות OCR
- ...

### 8. אינדיקטורים לסיכון קליני (אם רלוונטי)
- ...

=====================================
DATA SOURCE MARKERS (MANDATORY)
=====================================

Whenever your summary includes data that was extracted from a structured table
or from a visual chart/figure/graph/EKG image, you MUST prefix the relevant
lines with the appropriate marker:

  📋  → data extracted from a structured table (lab results, medications, vitals, form fields)
  📊  → data extracted from visual analysis of a chart, graph, figure, or EKG image

Format the marker at the START of the relevant bullet point or sub-heading.
Also BOLD the description after the marker.
Examples:
  - 📋 **תוצאות מעבדה (מטבלה):** המוגלובין 14.2 g/dL, WBC 8.2 ...
  - 📊 **פענוח א.ק.ג (ניתוח תמונה):** קצב סינוס, 72 פעימות/דקה ...
  - 📊 **גרף מגמה (ניתוח תמונה):** עלייה הדרגתית ב-TSH ...

This is MANDATORY. Do NOT omit these markers.
If no table or chart data exists, do not add markers.

=====================================
ADDITIONAL RULES
=====================================

- If document is administrative only → state that clearly.
- If document is non-medical → return: "No medical data identified."
- If multiple visits appear → summarize chronologically.
- If Hebrew appears → summarize in the requested output language.
- Do NOT output internal reasoning.

=====================================
FIELD CONSTRAINTS (STRICT)
=====================================

These rules override general summarization when the specific field exists:

- "Auxiliary Tests" / "בדיקות עזר":
  Must include ONLY lab results and diagnostic tests performed UP TO the current
  encounter. Do NOT include tests ordered or planned during the current visit.
  Example: "CBC (12/01/2025): WBC 8.2, Hgb 13.1; HbA1c (11/2025): 6.8%"

- "Diagnoses" / "אבחנות":
  Use ICD-10 terminology when identifiable. Differentiate between confirmed
  diagnoses and suspected/differential diagnoses.

- "Medications" / "תרופות":
  Include drug name, dosage, route, and frequency. If any element is missing,
  note it explicitly. List both active and recently discontinued medications.

- "Functional Status" / "מצב תפקודי":
  Express limitations in concrete terms (e.g., "cannot stand > 30 minutes")
  rather than vague descriptions. Include military-relevant limitations.

- "Physician Assessment" / "הערכת רופא":
  Distinguish between the physician's own clinical opinion and findings
  quoted from other specialists."""


@app.route(route="summarize", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def summarize_document(req: func.HttpRequest) -> func.HttpResponse:
    """Summarize OCR output using Azure OpenAI.

    Request body (JSON)::

        {
            "docId": "abc-123",
            "ocrText": "optional - if not provided, reads from outputs container",
            "ocrTables": [],  // optional - structured tables from prebuilt-layout
            "ocrFigures": [],  // optional - figure captions from prebuilt-layout
            "outputLanguage": "Hebrew"  // optional, default: same as document
        }
    """
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "Invalid JSON body"}),
            status_code=400, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    doc_id = body.get("docId")
    ocr_text = body.get("ocrText", "")
    ocr_tables = body.get("ocrTables", [])
    ocr_figures = body.get("ocrFigures", [])
    ocr_pages_body: list[dict] = body.get("ocrPages", [])
    output_language = body.get("outputLanguage", "")

    if not doc_id:
        return func.HttpResponse(
            json.dumps({"error": "docId is required"}),
            status_code=400, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    try:
        blob_helper = _get_blob_helper()

        # If no OCR text provided, read from blob storage
        ocr_pages: list[dict] = ocr_pages_body
        if not ocr_text:
            try:
                outputs_client = blob_helper._client.get_container_client("outputs")
                ocr_blob = outputs_client.get_blob_client(f"{doc_id}_ocr_result.json")
                ocr_data = json.loads(ocr_blob.download_blob().readall())
                analyze = ocr_data.get("analyzeResult", {})
                ocr_text = analyze.get("content", "")
                if not ocr_pages:
                    ocr_pages = analyze.get("pages", [])
                # Extract tables and figures from stored OCR result
                if not ocr_tables:
                    ocr_tables = analyze.get("tables", [])
                if not ocr_figures:
                    ocr_figures = analyze.get("figures", [])
            except Exception:
                return func.HttpResponse(
                    json.dumps({"error": f"No OCR result found for doc {doc_id}"}),
                    status_code=404, mimetype="application/json",
                    headers={"Access-Control-Allow-Origin": "*"},
                )

        # ── Smart figure filtering: skip logos, stamps, headers ──
        raw_figure_count = len(ocr_figures)
        ocr_figures = _filter_real_figures(ocr_figures, ocr_pages)
        if raw_figure_count != len(ocr_figures):
            logger.info(
                "Figure filtering: %d → %d for docId=%s",
                raw_figure_count, len(ocr_figures), doc_id,
            )

        if not ocr_text.strip():
            return func.HttpResponse(
                json.dumps({"error": "OCR text is empty"}),
                status_code=400, mimetype="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        # Call Azure OpenAI
        from openai import AzureOpenAI

        client = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_KEY"],
            api_version="2024-08-01-preview",
        )

        user_prompt = f"Summarize the following OCR-extracted medical document."
        if not output_language:
            output_language = "Hebrew"
        user_prompt += f" Output language: {output_language}."

        # ── Few-shot examples ────────────────────────────────────
        few_shot_messages, few_shot_info = _load_few_shot_examples(
            blob_helper,
            max_examples=5,
            document_type=body.get("documentType", ""),
            input_text_for_similarity=ocr_text,
        )

        user_prompt += f"\n\n---\n\n{ocr_text}"

        # Append structured table data if present
        if ocr_tables:
            user_prompt += "\n\n---\n\n## STRUCTURED TABLES EXTRACTED FROM DOCUMENT\n\n"
            for i, tbl in enumerate(ocr_tables):
                rows = tbl.get("rowCount", 0)
                cols = tbl.get("columnCount", 0)
                user_prompt += f"### Table {i+1} ({rows} rows x {cols} columns)\n\n"
                # Build a markdown table from cells
                grid = {}
                for cell in tbl.get("cells", []):
                    r = cell.get("rowIndex", 0)
                    c = cell.get("columnIndex", 0)
                    grid[(r, c)] = cell.get("content", "").replace("\n", " ")
                if grid:
                    for r in range(rows):
                        row_cells = [grid.get((r, c), "") for c in range(cols)]
                        user_prompt += "| " + " | ".join(row_cells) + " |\n"
                        if r == 0:
                            user_prompt += "|" + "|".join(["---"] * cols) + "|\n"
                user_prompt += "\n"

        # Append figure/chart info if present
        figure_page_numbers: set[int] = set()
        if ocr_figures:
            user_prompt += "\n## FIGURES/CHARTS DETECTED\n\n"
            for i, fig in enumerate(ocr_figures):
                caption = ""
                if fig.get("caption"):
                    caption = fig["caption"].get("content", "")
                user_prompt += f"- Figure {i+1}: {caption or 'No caption detected'}\n"
                # Track which pages contain figures for image attachment
                for region in fig.get("boundingRegions", []):
                    pn = region.get("pageNumber")
                    if pn:
                        figure_page_numbers.add(pn)

        # ── Conditionally load page images for figures/charts ────
        page_images_b64: dict[int, str] = {}
        if figure_page_numbers:
            import base64 as _b64
            artifacts_client = blob_helper._client.get_container_client("artifacts")
            for pn in sorted(figure_page_numbers):
                blob_path = f"enhanced/{doc_id}/page-{pn}.png"
                try:
                    img_bytes = artifacts_client.get_blob_client(blob_path).download_blob().readall()
                    page_images_b64[pn] = _b64.b64encode(img_bytes).decode("ascii")
                    logger.info("Loaded page %d image for figure analysis | docId=%s", pn, doc_id)
                except Exception:
                    logger.warning("Could not load page image %s for figure analysis", blob_path)

        # ── Add data-source marker reminder at end of user prompt ──
        _has_enriched = bool(ocr_tables) or bool(page_images_b64)
        if _has_enriched:
            user_prompt += "\n\n---\n\n"
            user_prompt += "⚠ REMINDER: This document contains "
            parts = []
            if ocr_tables:
                parts.append(f"{len(ocr_tables)} structured table(s)")
            if page_images_b64:
                parts.append(f"visual chart/figure image(s) on page(s) {', '.join(str(p) for p in sorted(page_images_b64))}")
            user_prompt += " and ".join(parts) + ".\n"
            user_prompt += (
                "You MUST prefix every bullet or sub-heading that contains data "
                "extracted from a table with 📋, and every bullet or sub-heading "
                "that contains data extracted from a visual chart/graph/EKG image with 📊. "
                "Bold the description after the marker. Do NOT skip this."
            )

        # Load active prompt (external or built-in)
        system_prompt = _load_active_prompt(blob_helper)

        # ── Build messages (multimodal if figure images available) ──
        use_vision = bool(page_images_b64)
        if use_vision:
            user_content: list[dict] = [{"type": "text", "text": user_prompt}]
            for pn in sorted(page_images_b64):
                user_content.append(
                    {"type": "text", "text": f"\n[Page {pn} image \u2013 figure/chart for visual analysis]:"}
                )
                user_content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{page_images_b64[pn]}",
                        "detail": "high",
                    },
                })
            messages = [
                {"role": "system", "content": system_prompt},
                *few_shot_messages,
                {"role": "user", "content": user_content},
            ]
        else:
            messages = [
                {"role": "system", "content": system_prompt},
                *few_shot_messages,
                {"role": "user", "content": user_prompt},
            ]

        vision_model = os.environ.get("AZURE_OPENAI_VISION_DEPLOYMENT", "")
        default_model = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
        model_name = (vision_model if use_vision and vision_model else default_model)

        response = client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=0.2,
            max_tokens=4000,
        )

        summary_text = response.choices[0].message.content
        usage = {
            "promptTokens": response.usage.prompt_tokens,
            "completionTokens": response.usage.completion_tokens,
            "totalTokens": response.usage.total_tokens,
        }

        # ── Summary quality assessment ───────────────────────────
        summary_quality = _assess_summary_quality(summary_text, ocr_text)

        result = {
            "docId": doc_id,
            "summary": summary_text,
            "model": response.model,
            "usage": usage,
            "summaryQuality": summary_quality,
            "visionUsed": use_vision,
            "figurePages": sorted(page_images_b64.keys()) if use_vision else [],
            "fewShotInfo": few_shot_info,
            "lowConfidence": body.get("lowConfidence", False),
            "timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        }

        # Save summary to blob storage
        blob_helper.upload_bytes(
            json.dumps(result, indent=2, ensure_ascii=False).encode("utf-8"),
            "outputs",
            f"{doc_id}_summary.json",
            content_type="application/json",
        )

        logger.info("Summary saved for docId=%s | tokens=%d", doc_id, usage["totalTokens"])

        return func.HttpResponse(
            json.dumps(result, ensure_ascii=False),
            status_code=200, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    except Exception:
        logger.exception("Error summarizing document %s", doc_id)
        return func.HttpResponse(
            json.dumps({"error": "Failed to summarize document", "detail": traceback.format_exc()}),
            status_code=500, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )


@app.route(route="summary/{docId}", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def get_summary(req: func.HttpRequest) -> func.HttpResponse:
    """Retrieve a saved summary for a document."""
    doc_id = req.route_params.get("docId")
    try:
        blob_helper = _get_blob_helper()
        outputs_client = blob_helper._client.get_container_client("outputs")
        blob_client = outputs_client.get_blob_client(f"{doc_id}_summary.json")
        data = blob_client.download_blob().readall()
        return func.HttpResponse(
            data, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except Exception:
        return func.HttpResponse(
            json.dumps({"error": f"Summary not found for {doc_id}"}),
            status_code=404, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )


@app.route(route="upload", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def upload_file(req: func.HttpRequest) -> func.HttpResponse:
    """Upload a file (PDF/image) to the raw container.

    Accepts multipart/form-data with a file field named 'file'.
    Optionally accepts a 'path' field to override the blob path.
    Optionally accepts a 'settings' JSON field with preprocessing options.
    When settings are provided, they are stored alongside the blob as
    {blobPath}.settings.json so the Logic App can pick them up.
    Returns the blob path so the Logic App can pick it up via the blob trigger.
    """
    try:
        # ── Parse multipart form data ────────────────────────────────
        files = req.files
        if not files or "file" not in files:
            return func.HttpResponse(
                json.dumps({"error": "No file field in multipart form data"}),
                status_code=400,
                mimetype="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        uploaded = files["file"]
        filename = uploaded.filename or f"upload_{uuid.uuid4().hex[:8]}"

        # Optional path override
        custom_path = req.form.get("path", "").strip() if req.form else ""
        blob_path = custom_path if custom_path else filename

        # Optional preprocessing settings
        settings_json = req.form.get("settings", "").strip() if req.form else ""

        # Read file content
        file_bytes = uploaded.read()
        if not file_bytes:
            return func.HttpResponse(
                json.dumps({"error": "Uploaded file is empty"}),
                status_code=400,
                mimetype="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        # Detect content type
        ext = Path(filename).suffix.lower()
        content_type_map = {
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".tif": "image/tiff",
            ".tiff": "image/tiff",
            ".bmp": "image/bmp",
        }
        content_type = content_type_map.get(ext, "application/octet-stream")

        # Upload to raw container
        blob_helper = _get_blob_helper()
        blob_helper.upload_bytes(file_bytes, "raw", blob_path, content_type=content_type)

        # Store preprocessing settings if provided
        if settings_json:
            try:
                settings_data = json.loads(settings_json)
                blob_helper.upload_bytes(
                    json.dumps(settings_data).encode("utf-8"),
                    "artifacts",
                    f"settings/{blob_path}.settings.json",
                    content_type="application/json",
                )
                logger.info("Stored preprocessing settings for %s", blob_path)
            except json.JSONDecodeError:
                logger.warning("Invalid settings JSON, ignoring: %s", settings_json)

        logger.info("Uploaded %s (%d bytes) → raw/%s", filename, len(file_bytes), blob_path)

        return func.HttpResponse(
            json.dumps({
                "success": True,
                "blobPath": blob_path,
                "container": "raw",
                "sizeBytes": len(file_bytes),
                "contentType": content_type,
                "settingsApplied": bool(settings_json),
            }),
            status_code=200,
            mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    except Exception:
        logger.exception("Error uploading file")
        return func.HttpResponse(
            json.dumps({"error": "Failed to upload file", "detail": traceback.format_exc()}),
            status_code=500,
            mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )


@app.route(route="upload", methods=["OPTIONS"], auth_level=func.AuthLevel.ANONYMOUS)
def upload_cors(req: func.HttpRequest) -> func.HttpResponse:
    """Handle CORS preflight for upload endpoint."""
    return func.HttpResponse(
        "",
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, x-functions-key",
        },
    )


# ═══════════════════════════════════════════════════════════════════════
# Multi-page OCR endpoint
# ═══════════════════════════════════════════════════════════════════════

@app.route(route="ocr", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def run_ocr(req: func.HttpRequest) -> func.HttpResponse:
    """Run Document Intelligence OCR on ALL enhanced pages for a document.

    Request body (JSON)::

        {
            "docId": "abc-123",
            "pageCount": 3,
            "lowConfidence": false
        }

    This endpoint reads each enhanced page from artifacts, sends them all
    to Document Intelligence, concatenates the results, and saves a single
    combined OCR result to outputs/{docId}_ocr_result.json.
    """
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "Invalid JSON body"}),
            status_code=400, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    doc_id = body.get("docId")
    page_count = body.get("pageCount", 1)
    low_confidence = body.get("lowConfidence", False)

    if not doc_id:
        return func.HttpResponse(
            json.dumps({"error": "docId is required"}),
            status_code=400, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    try:
        import time
        import requests

        blob_helper = _get_blob_helper()
        artifacts_client = blob_helper._client.get_container_client("artifacts")

        doc_intel_endpoint = os.environ["DOC_INTEL_ENDPOINT"]
        doc_intel_key = os.environ["DOC_INTEL_KEY"]

        all_content_parts: list[str] = []
        all_tables: list[dict] = []
        all_figures: list[dict] = []
        all_pages: list[dict] = []
        page_errors: list[str] = []

        # ── Parallel OCR: submit + poll all pages concurrently ───
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _ocr_single_page(page_num: int) -> tuple[int, dict | None, str | None]:
            """Submit one page to Doc Intelligence and poll to completion.

            Returns (page_num, result_data_or_None, error_string_or_None).
            """
            blob_path = f"enhanced/{doc_id}/page-{page_num}.png"
            try:
                blob_client = artifacts_client.get_blob_client(blob_path)
                image_bytes = blob_client.download_blob().readall()
            except Exception as e:
                return page_num, None, f"Page {page_num}: failed to read blob - {e}"

            try:
                submit_resp = requests.post(
                    doc_intel_endpoint,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Ocp-Apim-Subscription-Key": doc_intel_key,
                    },
                    data=image_bytes,
                    timeout=30,
                )
                submit_resp.raise_for_status()
                operation_url = submit_resp.headers.get("Operation-Location")
                if not operation_url:
                    return page_num, None, f"Page {page_num}: no Operation-Location header"
            except Exception as e:
                return page_num, None, f"Page {page_num}: submit failed - {e}"

            # Poll with exponential back-off
            for attempt in range(15):
                delay = min(2.0 * (1.3 ** attempt), 10.0)
                time.sleep(delay)
                try:
                    poll_resp = requests.get(
                        operation_url,
                        headers={"Ocp-Apim-Subscription-Key": doc_intel_key},
                        timeout=30,
                    )
                    poll_resp.raise_for_status()
                    poll_data = poll_resp.json()
                    if poll_data.get("status") == "succeeded":
                        return page_num, poll_data, None
                    elif poll_data.get("status") == "failed":
                        return page_num, None, f"Page {page_num}: analysis failed"
                except Exception:
                    continue

            return page_num, None, f"Page {page_num}: no result after polling"

        # Fan-out: process up to 4 pages concurrently
        max_workers = min(page_count, 4)
        page_results_map: dict[int, tuple[dict | None, str | None]] = {}

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_ocr_single_page, p): p
                for p in range(1, page_count + 1)
            }
            for fut in as_completed(futures):
                pg_num, result_data, error = fut.result()
                page_results_map[pg_num] = (result_data, error)

        # Merge results in page order so content/offsets are deterministic
        for page_num in range(1, page_count + 1):
            result_data, error = page_results_map[page_num]
            if error:
                page_errors.append(error)
                logger.warning("OCR parallel: %s", error)
                continue

            if result_data and "analyzeResult" in result_data:
                ar = result_data["analyzeResult"]
                content = ar.get("content", "")
                page_prefix = f"--- Page {page_num} ---\n"
                if content:
                    all_content_parts.append(page_prefix + content)
                if ar.get("tables"):
                    all_tables.extend(ar["tables"])
                if ar.get("figures"):
                    all_figures.extend(ar["figures"])
                if ar.get("pages"):
                    for pg in ar["pages"]:
                        pg["pageNumber"] = page_num
                        combined_offset = sum(len(p) for p in all_content_parts[:-1])
                        if all_content_parts[:-1]:
                            combined_offset += 2 * (len(all_content_parts) - 1)
                        combined_offset += len(page_prefix)
                        for span in pg.get("spans", []):
                            span["offset"] = span.get("offset", 0) + combined_offset
                    all_pages.extend(ar["pages"])
            else:
                page_errors.append(f"Page {page_num}: no result after polling")

        # Combine into a single OCR result
        combined_content = "\n\n".join(all_content_parts)

        combined_result = {
            "status": "succeeded",
            "analyzeResult": {
                "content": combined_content,
                "pages": all_pages,
                "tables": all_tables,
                "figures": all_figures,
            },
            "pagesProcessed": page_count,
            "pagesSucceeded": page_count - len(page_errors),
            "lowConfidence": low_confidence,
        }

        if page_errors:
            combined_result["pageErrors"] = page_errors

        # Save to outputs
        blob_helper.upload_bytes(
            json.dumps(combined_result, indent=2, ensure_ascii=False).encode("utf-8"),
            "outputs",
            f"{doc_id}_ocr_result.json",
            content_type="application/json",
        )

        logger.info(
            "OCR complete | docId=%s | pages=%d | succeeded=%d | errors=%d",
            doc_id, page_count, page_count - len(page_errors), len(page_errors),
        )

        return func.HttpResponse(
            json.dumps(combined_result, ensure_ascii=False),
            status_code=200, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    except Exception:
        logger.exception("Error running OCR for %s", doc_id)
        return func.HttpResponse(
            json.dumps({"error": "OCR failed", "detail": traceback.format_exc()}),
            status_code=500, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )


# ═══════════════════════════════════════════════════════════════════════
# Prompt Management endpoints
# ═══════════════════════════════════════════════════════════════════════

@app.route(route="prompts", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def list_prompts(req: func.HttpRequest) -> func.HttpResponse:
    """List all prompt versions stored in artifacts/prompts/."""
    try:
        blob_helper = _get_blob_helper()
        container_client = blob_helper._client.get_container_client("artifacts")

        prompts: list[dict] = []
        active_version = os.environ.get("ACTIVE_PROMPT_VERSION", "")

        for blob in container_client.list_blobs(name_starts_with="prompts/"):
            name = blob.name  # e.g. prompts/v1.txt
            version = name.replace("prompts/", "").replace(".txt", "")
            prompts.append({
                "version": version,
                "blobPath": name,
                "size": blob.size,
                "lastModified": blob.last_modified.isoformat() if blob.last_modified else None,
                "isActive": version == active_version,
            })

        prompts.sort(key=lambda p: p.get("lastModified", ""), reverse=True)

        return func.HttpResponse(
            json.dumps({"prompts": prompts, "activeVersion": active_version}),
            mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except Exception:
        logger.exception("Error listing prompts")
        return func.HttpResponse(
            json.dumps({"error": "Failed to list prompts"}),
            status_code=500, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )


@app.route(route="prompts", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def create_prompt(req: func.HttpRequest) -> func.HttpResponse:
    """Create a new prompt version.

    Request body (JSON)::

        {
            "version": "v2",
            "content": "You are a clinical ...",
            "description": "Added field constraints for auxiliary tests"
        }
    """
    try:
        body = req.get_json()
        version = body.get("version", "")
        content = body.get("content", "")
        description = body.get("description", "")

        if not version or not content:
            return func.HttpResponse(
                json.dumps({"error": "version and content are required"}),
                status_code=400, mimetype="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        blob_helper = _get_blob_helper()

        # Save the prompt text
        blob_helper.upload_bytes(
            content.encode("utf-8"),
            "artifacts",
            f"prompts/{version}.txt",
            content_type="text/plain",
        )

        # Save metadata
        import datetime
        metadata = {
            "version": version,
            "description": description,
            "createdAt": datetime.datetime.utcnow().isoformat() + "Z",
            "promptLength": len(content),
        }
        blob_helper.upload_bytes(
            json.dumps(metadata, indent=2).encode("utf-8"),
            "artifacts",
            f"prompts/{version}.meta.json",
            content_type="application/json",
        )

        logger.info("Created prompt version %s (%d chars)", version, len(content))

        return func.HttpResponse(
            json.dumps({"success": True, **metadata}),
            status_code=201, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except Exception:
        logger.exception("Error creating prompt")
        return func.HttpResponse(
            json.dumps({"error": "Failed to create prompt"}),
            status_code=500, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )


@app.route(route="prompts/{version}", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def get_prompt(req: func.HttpRequest) -> func.HttpResponse:
    """Retrieve a specific prompt version."""
    version = req.route_params.get("version")
    try:
        blob_helper = _get_blob_helper()
        container_client = blob_helper._client.get_container_client("artifacts")
        blob_client = container_client.get_blob_client(f"prompts/{version}.txt")
        content = blob_client.download_blob().readall().decode("utf-8")

        # Try to load metadata
        meta = {}
        try:
            meta_blob = container_client.get_blob_client(f"prompts/{version}.meta.json")
            meta = json.loads(meta_blob.download_blob().readall())
        except Exception:
            pass

        return func.HttpResponse(
            json.dumps({"version": version, "content": content, "metadata": meta}),
            mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except Exception:
        return func.HttpResponse(
            json.dumps({"error": f"Prompt version '{version}' not found"}),
            status_code=404, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )


@app.route(route="prompt-test", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def test_prompt(req: func.HttpRequest) -> func.HttpResponse:
    """Test a prompt against a document WITHOUT saving the result.

    Request body (JSON)::

        {
            "docId": "abc-123",
            "promptContent": "You are a clinical...",
            "outputLanguage": "Hebrew"
        }
    """
    try:
        body = req.get_json()
        doc_id = body.get("docId")
        prompt_content = body.get("promptContent", "")
        output_language = body.get("outputLanguage", "Hebrew")

        if not doc_id or not prompt_content:
            return func.HttpResponse(
                json.dumps({"error": "docId and promptContent are required"}),
                status_code=400, mimetype="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        blob_helper = _get_blob_helper()
        outputs_client = blob_helper._client.get_container_client("outputs")
        ocr_blob = outputs_client.get_blob_client(f"{doc_id}_ocr_result.json")
        ocr_data = json.loads(ocr_blob.download_blob().readall())
        analyze = ocr_data.get("analyzeResult", {})
        ocr_text = analyze.get("content", "")
        ocr_tables = analyze.get("tables", [])
        ocr_figures = analyze.get("figures", [])

        if not ocr_text.strip():
            return func.HttpResponse(
                json.dumps({"error": "No OCR text available for this document"}),
                status_code=400, mimetype="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        from openai import AzureOpenAI
        client = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_KEY"],
            api_version="2024-08-01-preview",
        )

        user_prompt = f"Summarize the following OCR-extracted medical document. Output language: {output_language}.\n\n---\n\n{ocr_text}"

        # Add tables/figures
        if ocr_tables:
            user_prompt += "\n\n---\n\n## STRUCTURED TABLES\n\n"
            for i, tbl in enumerate(ocr_tables):
                rows = tbl.get("rowCount", 0)
                cols = tbl.get("columnCount", 0)
                user_prompt += f"### Table {i+1} ({rows}x{cols})\n\n"
                grid = {}
                for cell in tbl.get("cells", []):
                    r, c = cell.get("rowIndex", 0), cell.get("columnIndex", 0)
                    grid[(r, c)] = cell.get("content", "").replace("\n", " ")
                for r in range(rows):
                    row_cells = [grid.get((r, c), "") for c in range(cols)]
                    user_prompt += "| " + " | ".join(row_cells) + " |\n"
                    if r == 0:
                        user_prompt += "|" + "|".join(["---"] * cols) + "|\n"
                user_prompt += "\n"

        response = client.chat.completions.create(
            model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": prompt_content},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=4000,
        )

        summary_text = response.choices[0].message.content
        quality = _assess_summary_quality(summary_text, ocr_text)

        return func.HttpResponse(
            json.dumps({
                "docId": doc_id,
                "summary": summary_text,
                "summaryQuality": quality,
                "usage": {
                    "promptTokens": response.usage.prompt_tokens,
                    "completionTokens": response.usage.completion_tokens,
                    "totalTokens": response.usage.total_tokens,
                },
                "note": "This is a TEST run. The result was NOT saved.",
            }, ensure_ascii=False),
            status_code=200, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    except Exception:
        logger.exception("Error testing prompt")
        return func.HttpResponse(
            json.dumps({"error": "Prompt test failed", "detail": traceback.format_exc()}),
            status_code=500, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )


@app.route(route="prompts", methods=["OPTIONS"], auth_level=func.AuthLevel.ANONYMOUS)
def prompts_cors(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse("", status_code=200, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, x-functions-key",
    })


# ═══════════════════════════════════════════════════════════════════════
# Few-shot examples management
# ═══════════════════════════════════════════════════════════════════════

@app.route(route="examples", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def list_examples(req: func.HttpRequest) -> func.HttpResponse:
    """List all few-shot examples stored in artifacts/examples/."""
    try:
        blob_helper = _get_blob_helper()
        container_client = blob_helper._client.get_container_client("artifacts")

        examples: list[dict] = []
        seen_ids: set[str] = set()

        for blob in container_client.list_blobs(name_starts_with="examples/"):
            # examples/{id}/metadata.json
            parts = blob.name.split("/")
            if len(parts) >= 3 and parts[2] == "metadata.json":
                example_id = parts[1]
                if example_id in seen_ids:
                    continue
                seen_ids.add(example_id)
                try:
                    meta_blob = container_client.get_blob_client(blob.name)
                    meta = json.loads(meta_blob.download_blob().readall())
                    meta["exampleId"] = example_id
                    examples.append(meta)
                except Exception:
                    examples.append({"exampleId": example_id})

        return func.HttpResponse(
            json.dumps(examples),
            mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except Exception:
        logger.exception("Error listing examples")
        return func.HttpResponse(
            json.dumps({"error": "Failed to list examples"}),
            status_code=500, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )


@app.route(route="examples", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def create_example(req: func.HttpRequest) -> func.HttpResponse:
    """Create a new few-shot example.

    Request body (JSON)::

        {
            "inputText": "OCR content of the document...",
            "idealSummary": "## Cleaned Medical Summary\\n...",
            "category": "recruitment",
            "documentType": "recruitment",
            "description": "Standard recruitment medical form",
            "isGolden": true,
            "tags": ["hebrew", "with-tables"]
        }
    """
    try:
        body = req.get_json()
        input_text = body.get("inputText", "")
        ideal_summary = body.get("idealSummary", "")
        category = body.get("category", "general")
        document_type = body.get("documentType", category)
        description = body.get("description", "")
        is_golden = body.get("isGolden", False)
        tags = body.get("tags", [])

        if not input_text or not ideal_summary:
            return func.HttpResponse(
                json.dumps({"error": "inputText and idealSummary are required"}),
                status_code=400, mimetype="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        example_id = f"ex-{uuid.uuid4().hex[:8]}"
        blob_helper = _get_blob_helper()

        # Save input
        blob_helper.upload_bytes(
            input_text.encode("utf-8"),
            "artifacts",
            f"examples/{example_id}/input.txt",
            content_type="text/plain",
        )

        # Save ideal summary
        blob_helper.upload_bytes(
            ideal_summary.encode("utf-8"),
            "artifacts",
            f"examples/{example_id}/summary.txt",
            content_type="text/plain",
        )

        # Compute & store embedding if configured
        _store_example_embedding(blob_helper, example_id, input_text)

        # Save metadata
        import datetime
        metadata = {
            "exampleId": example_id,
            "category": category,
            "documentType": document_type,
            "description": description,
            "isGolden": is_golden,
            "tags": tags,
            "inputLength": len(input_text),
            "summaryLength": len(ideal_summary),
            "createdAt": datetime.datetime.utcnow().isoformat() + "Z",
        }
        blob_helper.upload_bytes(
            json.dumps(metadata, indent=2).encode("utf-8"),
            "artifacts",
            f"examples/{example_id}/metadata.json",
            content_type="application/json",
        )

        logger.info("Created few-shot example %s (category=%s)", example_id, category)

        return func.HttpResponse(
            json.dumps({"success": True, **metadata}),
            status_code=201, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except Exception:
        logger.exception("Error creating example")
        return func.HttpResponse(
            json.dumps({"error": "Failed to create example"}),
            status_code=500, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )


@app.route(route="examples", methods=["OPTIONS"], auth_level=func.AuthLevel.ANONYMOUS)
def examples_cors(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse("", status_code=200, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, x-functions-key",
    })


@app.route(route="examples/{exampleId}", methods=["PUT"], auth_level=func.AuthLevel.FUNCTION)
def update_example(req: func.HttpRequest) -> func.HttpResponse:
    """Update metadata for an existing few-shot example.

    Request body (JSON) – all fields optional::

        {
            "documentType": "cardiology",
            "description": "Updated description",
            "isGolden": true,
            "tags": ["hebrew", "with-tables"],
            "category": "cardiology"
        }
    """
    example_id = req.route_params.get("exampleId")
    try:
        body = req.get_json()
    except ValueError:
        body = {}

    try:
        blob_helper = _get_blob_helper()
        container_client = blob_helper._client.get_container_client("artifacts")

        # Load existing metadata
        meta_blob = container_client.get_blob_client(f"examples/{example_id}/metadata.json")
        try:
            metadata = json.loads(meta_blob.download_blob().readall())
        except Exception:
            return func.HttpResponse(
                json.dumps({"error": f"Example {example_id} not found"}),
                status_code=404, mimetype="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        # Merge updates
        for field in ("documentType", "description", "isGolden", "tags", "category"):
            if field in body:
                metadata[field] = body[field]

        import datetime
        metadata["updatedAt"] = datetime.datetime.utcnow().isoformat() + "Z"

        blob_helper.upload_bytes(
            json.dumps(metadata, indent=2, ensure_ascii=False).encode("utf-8"),
            "artifacts",
            f"examples/{example_id}/metadata.json",
            content_type="application/json",
        )

        logger.info("Updated example %s", example_id)
        return func.HttpResponse(
            json.dumps({"success": True, **metadata}),
            mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except Exception:
        logger.exception("Error updating example %s", example_id)
        return func.HttpResponse(
            json.dumps({"error": "Failed to update example"}),
            status_code=500, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )


@app.route(route="examples/{exampleId}", methods=["DELETE"], auth_level=func.AuthLevel.FUNCTION)
def delete_example(req: func.HttpRequest) -> func.HttpResponse:
    """Delete a few-shot example and all its blobs."""
    example_id = req.route_params.get("exampleId")
    try:
        blob_helper = _get_blob_helper()
        container_client = blob_helper._client.get_container_client("artifacts")

        deleted = 0
        for blob in container_client.list_blobs(name_starts_with=f"examples/{example_id}/"):
            container_client.get_blob_client(blob.name).delete_blob()
            deleted += 1

        if deleted == 0:
            return func.HttpResponse(
                json.dumps({"error": f"Example {example_id} not found"}),
                status_code=404, mimetype="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        logger.info("Deleted example %s (%d blobs)", example_id, deleted)
        return func.HttpResponse(
            json.dumps({"success": True, "deleted": deleted}),
            mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except Exception:
        logger.exception("Error deleting example %s", example_id)
        return func.HttpResponse(
            json.dumps({"error": "Failed to delete example"}),
            status_code=500, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )


@app.route(route="examples/{exampleId}", methods=["OPTIONS"], auth_level=func.AuthLevel.ANONYMOUS)
def example_detail_cors(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse("", status_code=200, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, x-functions-key",
    })


@app.route(route="promote-to-example", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def promote_to_example(req: func.HttpRequest) -> func.HttpResponse:
    """Promote an existing document + summary pair to a golden example.

    Request body (JSON)::

        {
            "docId": "abc-123",
            "documentType": "cardiology",
            "description": "Great cardiology summary",
            "isGolden": true,
            "tags": ["hebrew"],
            "pages": [1, 3]
        }

    If ``pages`` is provided, only the OCR text from those page numbers
    is included in the example input.  Otherwise all pages are used.
    """
    try:
        body = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"error": "Invalid JSON body"}),
            status_code=400, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    doc_id = body.get("docId")
    if not doc_id:
        return func.HttpResponse(
            json.dumps({"error": "docId is required"}),
            status_code=400, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    try:
        blob_helper = _get_blob_helper()
        outputs_client = blob_helper._client.get_container_client("outputs")

        selected_pages = body.get("pages")  # e.g. [1, 3]

        # Load OCR text
        try:
            ocr_blob = outputs_client.get_blob_client(f"{doc_id}_ocr_result.json")
            ocr_data = json.loads(ocr_blob.download_blob().readall())
            analyze = ocr_data.get("analyzeResult", {})
            full_content = analyze.get("content", "")

            if selected_pages and isinstance(selected_pages, list):
                # Extract text only from chosen pages using span offsets
                page_texts = []
                for pg in analyze.get("pages", []):
                    if pg.get("pageNumber") in selected_pages:
                        for span in pg.get("spans", []):
                            offset = span.get("offset", 0)
                            length = span.get("length", 0)
                            page_texts.append(full_content[offset:offset + length])
                ocr_text = "\n".join(page_texts) if page_texts else full_content
            else:
                ocr_text = full_content
        except Exception:
            return func.HttpResponse(
                json.dumps({"error": f"No OCR result found for doc {doc_id}"}),
                status_code=404, mimetype="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        # Load summary
        try:
            summary_blob = outputs_client.get_blob_client(f"{doc_id}_summary.json")
            summary_data = json.loads(summary_blob.download_blob().readall())
            summary_text = summary_data.get("summary", "")
        except Exception:
            return func.HttpResponse(
                json.dumps({"error": f"No summary found for doc {doc_id}"}),
                status_code=404, mimetype="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        if not ocr_text.strip() or not summary_text.strip():
            return func.HttpResponse(
                json.dumps({"error": "OCR text or summary is empty"}),
                status_code=400, mimetype="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        # Create example
        example_id = f"ex-{uuid.uuid4().hex[:8]}"
        document_type = body.get("documentType", "general")
        description = body.get("description", f"Promoted from doc {doc_id}")
        is_golden = body.get("isGolden", True)
        tags = body.get("tags", [])

        blob_helper.upload_bytes(
            ocr_text.encode("utf-8"), "artifacts",
            f"examples/{example_id}/input.txt", content_type="text/plain",
        )
        blob_helper.upload_bytes(
            summary_text.encode("utf-8"), "artifacts",
            f"examples/{example_id}/summary.txt", content_type="text/plain",
        )

        _store_example_embedding(blob_helper, example_id, ocr_text)

        import datetime
        metadata = {
            "exampleId": example_id,
            "category": document_type,
            "documentType": document_type,
            "description": description,
            "isGolden": is_golden,
            "tags": tags,
            "sourceDocId": doc_id,
            "selectedPages": selected_pages if selected_pages else None,
            "inputLength": len(ocr_text),
            "summaryLength": len(summary_text),
            "createdAt": datetime.datetime.utcnow().isoformat() + "Z",
        }
        blob_helper.upload_bytes(
            json.dumps(metadata, indent=2, ensure_ascii=False).encode("utf-8"),
            "artifacts",
            f"examples/{example_id}/metadata.json",
            content_type="application/json",
        )

        logger.info("Promoted doc %s to example %s", doc_id, example_id)
        return func.HttpResponse(
            json.dumps({"success": True, **metadata}),
            status_code=201, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except Exception:
        logger.exception("Error promoting doc %s to example", doc_id)
        return func.HttpResponse(
            json.dumps({"error": "Failed to promote to example"}),
            status_code=500, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )


@app.route(route="promote-to-example", methods=["OPTIONS"], auth_level=func.AuthLevel.ANONYMOUS)
def promote_cors(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse("", status_code=200, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, x-functions-key",
    })


# ═══════════════════════════════════════════════════════════════════════
# Failure monitoring endpoint
# ═══════════════════════════════════════════════════════════════════════

@app.route(route="failures", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def list_failures(req: func.HttpRequest) -> func.HttpResponse:
    """Aggregate failure statistics from outputs container."""
    try:
        from preprocessing.metrics import FAILURE_DESCRIPTIONS

        blob_helper = _get_blob_helper()
        outputs_client = blob_helper._client.get_container_client("outputs")

        failures: list[dict] = []
        code_counts: dict[str, int] = {}

        for blob in outputs_client.list_blobs():
            if blob.name.endswith("_failure.json"):
                try:
                    blob_client = outputs_client.get_blob_client(blob.name)
                    data = json.loads(blob_client.download_blob().readall())
                    code = data.get("failureCode", "UNKNOWN")
                    code_counts[code] = code_counts.get(code, 0) + 1
                    data["failureDescription"] = FAILURE_DESCRIPTIONS.get(code)
                    failures.append(data)
                except Exception:
                    continue

        result = {
            "totalFailures": len(failures),
            "byCode": code_counts,
            "codeDescriptions": {
                k: v["title"] for k, v in FAILURE_DESCRIPTIONS.items()
            },
            "failures": sorted(
                failures,
                key=lambda f: f.get("timestamp", ""),
                reverse=True,
            ),
        }

        return func.HttpResponse(
            json.dumps(result, default=str),
            mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except Exception:
        logger.exception("Error listing failures")
        return func.HttpResponse(
            json.dumps({"error": "Failed to list failures"}),
            status_code=500, mimetype="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )


@app.route(route="dashboard", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def dashboard(req: func.HttpRequest) -> func.HttpResponse:
    """Serve the dashboard frontend."""
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        return func.HttpResponse(html, mimetype="text/html", headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"})
    except FileNotFoundError:
        return func.HttpResponse("Dashboard not found", status_code=404)


def _get_function_key() -> str:
    """Return the default function key.

    Priority:
    1. PREOCR_FUNCTION_KEY env var (explicit override / set by buildScript)
    2. Auto-discover via the local admin endpoint
       (http://localhost:{ADMIN_PORT}/admin/host/keys)
    The result is cached in-process so we only fetch once.
    """
    cached = getattr(_get_function_key, "_cached", None)
    if cached is not None:
        return cached

    # 1) Explicit env var
    key = os.environ.get("PREOCR_FUNCTION_KEY", "")
    if key:
        _get_function_key._cached = key
        return key

    # 2) Auto-discover from the local admin API
    import urllib.request, urllib.error  # noqa: E401
    admin_port = os.environ.get("FUNCTIONS_HOST_ADMIN_PORT", "")
    admin_url = f"http://localhost:{admin_port}/admin/host/keys" if admin_port else None
    try:
        if admin_url:
            req_obj = urllib.request.Request(admin_url)
            with urllib.request.urlopen(req_obj, timeout=5) as resp:
                data = json.loads(resp.read())
                for k in data.get("keys", []):
                    if k.get("name") == "default":
                        key = k.get("value", "")
                        break
    except Exception:
        logger.debug("Could not auto-discover function key from admin API")

    _get_function_key._cached = key
    return key


@app.route(route="ui", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def ui(req: func.HttpRequest) -> func.HttpResponse:
    """Serve the new React UI (single-file build).

    Injects the default function key so the URL stays clean
    (no ?code= needed).  The key is auto-discovered from the
    PREOCR_FUNCTION_KEY env var or the Functions admin API.
    """
    html_path = os.path.join(os.path.dirname(__file__), "static", "ui.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()

        # Inject function key so the React app can call authenticated APIs
        func_key = _get_function_key()
        key_script = f'<script>window.__PREOCR_KEY__="{func_key}";</script>'
        html = html.replace("</head>", f"{key_script}</head>", 1)

        return func.HttpResponse(html, mimetype="text/html", headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"})
    except FileNotFoundError:
        return func.HttpResponse("UI not found", status_code=404)


@app.route(route="acme-challenge/{token}", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def acme_challenge(req: func.HttpRequest) -> func.HttpResponse:
    """Serve ACME HTTP-01 challenge responses for Let's Encrypt.

    The challenge value is stored in the ACME_CHALLENGE_RESPONSE app setting.
    Format: token=response  (if multiple, separate with semicolons).
    """
    token = req.route_params.get("token", "")
    challenges = os.environ.get("ACME_CHALLENGE_RESPONSE", "")
    for entry in challenges.split(";"):
        entry = entry.strip()
        if "=" in entry:
            t, r = entry.split("=", 1)
            if t.strip() == token:
                return func.HttpResponse(r.strip(), mimetype="text/plain")
    return func.HttpResponse("Not found", status_code=404)


# ═══════════════════════════════════════════════════════════════════════
# Durable Functions – Document Processing Pipeline
# (replaces the Logic App Standard orchestration)
#
# Architecture:
#   blob_pipeline_start  (blob trigger → starts orchestration)
#   doc_pipeline_orchestrator  (orchestrator → routes the pipeline)
#   activity_preprocess  (activity → calls /api/preprocess internally)
#   activity_ocr         (activity → calls /api/ocr internally)
#   activity_summarize   (activity → calls /api/summarize internally)
#   activity_write_failure (activity → writes failure JSON to blob)
# ═══════════════════════════════════════════════════════════════════════


def _internal_api_call(
    endpoint: str,
    payload: dict,
    correlation_id: str = "",
    timeout: int = 600,
) -> dict:
    """POST to an HTTP-trigger in the same Function App via localhost.

    This avoids refactoring existing HTTP handlers – activities simply
    call the already-tested endpoints internally.
    """
    import requests as _req

    func_key = os.environ.get("PREOCR_FUNCTION_KEY", "")
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if func_key:
        headers["x-functions-key"] = func_key
    if correlation_id:
        headers["x-correlation-id"] = correlation_id

    resp = _req.post(
        f"http://localhost/api/{endpoint}",
        json=payload,
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


# ── Blob-trigger starter ────────────────────────────────────────────

@app.blob_trigger(
    arg_name="blob",
    path="raw/{name}",
    connection="AzureWebJobsStorage",
)
@app.durable_client_input(client_name="client")
async def blob_pipeline_start(
    blob: func.InputStream,
    client: df.DurableOrchestrationClient,
):
    """Kick off the pipeline whenever a new blob lands in *raw/*."""
    blob_name = blob.name or ""
    # Strip container prefix if the runtime includes it
    if blob_name.startswith("raw/"):
        blob_name = blob_name[4:]

    # Skip non-document helper files
    if blob_name.endswith(".settings.json"):
        logger.info("[pipeline] skipping settings blob: %s", blob_name)
        return

    instance_id = await client.start_new(
        "doc_pipeline_orchestrator",
        instance_id=None,
        client_input={"blob_name": blob_name},
    )
    logger.info(
        "[pipeline] started orchestration %s for %s",
        instance_id,
        blob_name,
    )


# ── Orchestrator ─────────────────────────────────────────────────────

@app.orchestration_trigger(context_name="context")
def doc_pipeline_orchestrator(context: df.DurableOrchestrationContext):
    """Preprocess → route → OCR → Summarize  (mirrors the Logic App).

    Flow:
        1. Call preprocess
        2. Switch on recommendedNextAction:
           • skip → done
           • fail → write failure JSON
           • run_doc_intel → OCR → Summarize
           • run_doc_intel_low_confidence → OCR → check text → Summarize
           • retry_stronger → re-preprocess (aggressive) → re-evaluate
    """
    input_data = context.get_input()
    blob_name: str = input_data["blob_name"]
    doc_id = str(context.new_guid())
    correlation_id = str(context.new_guid())

    # Retry policies (mirror the Logic App retry settings)
    pp_retry = df.RetryOptions(
        first_retry_interval_in_milliseconds=30_000, max_number_of_attempts=3,
    )
    ocr_retry = df.RetryOptions(
        first_retry_interval_in_milliseconds=30_000, max_number_of_attempts=2,
    )
    sum_retry = df.RetryOptions(
        first_retry_interval_in_milliseconds=10_000, max_number_of_attempts=2,
    )

    # ── Step 1: preprocess ───────────────────────────────────────────
    pp_result: dict = yield context.call_activity_with_retry(
        "activity_preprocess",
        pp_retry,
        {
            "container_name": "raw",
            "blob_path": blob_name,
            "doc_id": doc_id,
            "correlation_id": correlation_id,
        },
    )

    action: str = (
        pp_result.get("aggregated", {}).get("recommendedNextAction", "")
    )
    pp_aggregated: dict = pp_result.get("aggregated", {})
    pages: list = pp_result.get("pages", [])
    retry_attempted = False

    # ── Handle "skip" ────────────────────────────────────────────────
    if action == "skip":
        return {"status": "skipped", "docId": doc_id}

    # ── Handle "fail" ────────────────────────────────────────────────
    if action == "fail":
        yield context.call_activity("activity_write_failure", {
            "doc_id": doc_id,
            "correlation_id": correlation_id,
            "failure_code": pp_aggregated.get("failureCode", "UNKNOWN"),
            "pp_aggregated": pp_aggregated,
            "retry_attempted": False,
        })
        return {
            "status": "failed",
            "docId": doc_id,
            "failureCode": pp_aggregated.get("failureCode"),
        }

    # ── Handle "retry_stronger" ──────────────────────────────────────
    if action == "retry_stronger":
        retry_attempted = True
        pp_result = yield context.call_activity_with_retry(
            "activity_preprocess",
            pp_retry,
            {
                "container_name": "raw",
                "blob_path": blob_name,
                "doc_id": doc_id,
                "correlation_id": correlation_id,
                "options": {"aggressive": True},
            },
        )
        action = (
            pp_result.get("aggregated", {})
            .get("recommendedNextAction", "")
        )
        pp_aggregated = pp_result.get("aggregated", {})
        pages = pp_result.get("pages", [])

        if action not in ("run_doc_intel", "run_doc_intel_low_confidence"):
            yield context.call_activity("activity_write_failure", {
                "doc_id": doc_id,
                "correlation_id": correlation_id,
                "failure_code": pp_aggregated.get("failureCode", "UNKNOWN"),
                "pp_aggregated": pp_aggregated,
                "retry_attempted": True,
            })
            return {"status": "failed", "docId": doc_id, "retry": True}

    # ── At this point action is run_doc_intel or run_doc_intel_low_confidence
    if action not in ("run_doc_intel", "run_doc_intel_low_confidence"):
        # Defensive: unknown action
        return {
            "status": "failed",
            "docId": doc_id,
            "error": f"Unknown action: {action}",
        }

    low_confidence = action == "run_doc_intel_low_confidence"

    # ── Step 2: OCR ──────────────────────────────────────────────────
    ocr_result: dict = yield context.call_activity_with_retry(
        "activity_ocr",
        ocr_retry,
        {
            "doc_id": doc_id,
            "page_count": len(pages),
            "low_confidence": low_confidence,
        },
    )

    ocr_text: str = (
        ocr_result.get("analyzeResult", {}).get("content", "")
    )

    # Gate: fail if OCR returned no text at all
    if not (ocr_text or "").strip():
        yield context.call_activity("activity_write_failure", {
            "doc_id": doc_id,
            "correlation_id": correlation_id,
            "failure_code": pp_aggregated.get("failureCode", "F08"),
            "failure_title": "OCR Returned Empty Text",
            "failure_description": (
                "Document Intelligence returned no extracted text. "
                "The document may be blank, image-only, or the OCR "
                "service could not process it."
            ),
            "pp_aggregated": pp_aggregated,
            "retry_attempted": retry_attempted,
            "low_confidence_attempted": low_confidence,
        })
        return {"status": "failed", "docId": doc_id, "failureCode": "F08"}

    # Low-confidence gate: fail if OCR text too short
    if low_confidence and len(ocr_text or "") <= 20:
        yield context.call_activity("activity_write_failure", {
            "doc_id": doc_id,
            "correlation_id": correlation_id,
            "failure_code": pp_aggregated.get("failureCode", "F01"),
            "failure_title": "Low OCR Confidence",
            "failure_description": (
                "Document quality too low and OCR extracted "
                "insufficient text."
            ),
            "pp_aggregated": pp_aggregated,
            "retry_attempted": retry_attempted,
            "low_confidence_attempted": True,
        })
        return {"status": "failed", "docId": doc_id, "failureCode": "F01"}

    # ── Step 3: Summarize ────────────────────────────────────────────
    yield context.call_activity_with_retry(
        "activity_summarize",
        sum_retry,
        {
            "doc_id": doc_id,
            "ocr_text": ocr_text,
            "ocr_tables": (
                ocr_result.get("analyzeResult", {}).get("tables", [])
            ),
            "ocr_figures": (
                ocr_result.get("analyzeResult", {}).get("figures", [])
            ),
            "ocr_pages": (
                ocr_result.get("analyzeResult", {}).get("pages", [])
            ),
            "low_confidence": low_confidence,
        },
    )

    return {
        "status": "completed",
        "docId": doc_id,
        "action": action,
        "retry": retry_attempted,
    }


# ── Activity: preprocess ─────────────────────────────────────────────

@app.activity_trigger(input_name="payload")
def activity_preprocess(payload: dict) -> dict:
    """Call /api/preprocess on the same Function App host."""
    body: dict = {
        "containerName": payload["container_name"],
        "blobPath": payload["blob_path"],
        "docId": payload["doc_id"],
    }
    if payload.get("options"):
        body["options"] = payload["options"]
    return _internal_api_call(
        "preprocess",
        body,
        correlation_id=payload.get("correlation_id", ""),
        timeout=600,
    )


# ── Activity: OCR ────────────────────────────────────────────────────

@app.activity_trigger(input_name="payload")
def activity_ocr(payload: dict) -> dict:
    """Call /api/ocr on the same Function App host."""
    return _internal_api_call(
        "ocr",
        {
            "docId": payload["doc_id"],
            "pageCount": payload["page_count"],
            "lowConfidence": payload.get("low_confidence", False),
        },
        timeout=600,
    )


# ── Activity: summarize ──────────────────────────────────────────────

@app.activity_trigger(input_name="payload")
def activity_summarize(payload: dict) -> dict:
    """Call /api/summarize on the same Function App host."""
    return _internal_api_call(
        "summarize",
        {
            "docId": payload["doc_id"],
            "ocrText": payload.get("ocr_text", ""),
            "ocrTables": payload.get("ocr_tables", []),
            "ocrFigures": payload.get("ocr_figures", []),
            "ocrPages": payload.get("ocr_pages", []),
            "lowConfidence": payload.get("low_confidence", False),
        },
        timeout=300,
    )


# ── Activity: write failure JSON ─────────────────────────────────────

@app.activity_trigger(input_name="payload")
def activity_write_failure(payload: dict) -> dict:
    """Write a failure document to the *outputs* container."""
    from datetime import datetime, timezone

    doc_id = payload["doc_id"]
    pp = payload.get("pp_aggregated", {})
    failure = {
        "docId": doc_id,
        "correlationId": payload.get("correlation_id", ""),
        "status": "failed",
        "failureCode": payload.get("failure_code", "UNKNOWN"),
        "failureTitle": payload.get(
            "failure_title", "Preprocessing Failed",
        ),
        "failureDescription": payload.get(
            "failure_description", "Document failed preprocessing.",
        ),
        "avgBlurScore": pp.get("avgBlurScore", 0),
        "avgOcrReadinessScore": pp.get("avgOcrReadinessScore", 0),
        "avgRedactionPercent": pp.get("avgRedactionPercent", 0),
        "retryAttempted": payload.get("retry_attempted", False),
        "lowConfidenceAttempted": payload.get(
            "low_confidence_attempted", False,
        ),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    blob_helper = _get_blob_helper()
    blob_helper.upload_bytes(
        json.dumps(failure, indent=2, ensure_ascii=False).encode("utf-8"),
        "outputs",
        f"{doc_id}_failure.json",
        content_type="application/json",
    )
    logger.info(
        "[pipeline] wrote failure for doc %s: %s",
        doc_id,
        failure["failureCode"],
    )
    return failure


# ── HTTP: pipeline status (for the dashboard) ────────────────────────

@app.route(
    route="pipeline-status/{instanceId}",
    methods=["GET"],
    auth_level=func.AuthLevel.FUNCTION,
)
@app.durable_client_input(client_name="client")
async def get_pipeline_status(
    req: func.HttpRequest,
    client: df.DurableOrchestrationClient,
):
    """Return the Durable Functions orchestration status for a pipeline run."""
    instance_id = req.route_params.get("instanceId", "")
    return client.create_check_status_response(req, instance_id)
