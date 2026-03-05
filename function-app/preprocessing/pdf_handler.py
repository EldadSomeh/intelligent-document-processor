"""PDF handling – split multi-page PDFs into per-page PNG images.

Requires **poppler-utils** installed on the host (see Dockerfile).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pdf2image import convert_from_path

logger = logging.getLogger(__name__)

_PDF_MAGIC = b"%PDF-"


class PdfHandler:
    """Detect and split PDF files into individual page images."""

    @staticmethod
    def is_pdf(file_path: str) -> bool:
        """Return True if *file_path* is a PDF (by extension or magic bytes)."""
        if Path(file_path).suffix.lower() == ".pdf":
            return True
        try:
            with open(file_path, "rb") as fh:
                return fh.read(5) == _PDF_MAGIC
        except OSError:
            return False

    @staticmethod
    def split_to_images(pdf_path: str, output_dir: str, dpi: int = 300) -> list[str]:
        """Convert each page of *pdf_path* to a PNG file in *output_dir*.

        Returns a list of absolute paths to the generated images, ordered
        by page number.
        """
        pages = convert_from_path(pdf_path, dpi=dpi)
        image_paths: list[str] = []

        for idx, page_img in enumerate(pages, start=1):
            out_path = os.path.join(output_dir, f"pdf_page_{idx}.png")
            page_img.save(out_path, "PNG")
            image_paths.append(out_path)

        logger.info("Split %s → %d page image(s)", pdf_path, len(image_paths))
        return image_paths
