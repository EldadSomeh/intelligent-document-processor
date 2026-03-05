"""Azure Blob Storage helper – uses Managed Identity (DefaultAzureCredential).

No connection strings or keys ever appear in this module.
"""

from __future__ import annotations

import logging
import os

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

logger = logging.getLogger(__name__)


class BlobHelper:
    """Thin convenience wrapper around the Azure Blob SDK."""

    def __init__(self, account_url: str) -> None:
        credential = DefaultAzureCredential()
        self._client = BlobServiceClient(
            account_url=account_url,
            credential=credential,
        )

    # ── Download ─────────────────────────────────────────────────────

    def download(self, container: str, blob_path: str, local_path: str) -> None:
        """Download a blob to a local file path."""
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        blob_client = self._client.get_blob_client(
            container=container, blob=blob_path,
        )
        with open(local_path, "wb") as fh:
            stream = blob_client.download_blob()
            stream.readinto(fh)
        logger.info(
            "Downloaded %s/%s → %s (%d bytes)",
            container, blob_path, local_path, os.path.getsize(local_path),
        )

    # ── Upload file ──────────────────────────────────────────────────

    def upload(
        self,
        local_path: str,
        container: str,
        blob_path: str,
        content_type: str | None = None,
    ) -> None:
        """Upload a local file to a blob."""
        blob_client = self._client.get_blob_client(
            container=container, blob=blob_path,
        )
        kwargs: dict = {"overwrite": True}
        if content_type:
            kwargs["content_settings"] = ContentSettings(content_type=content_type)
        with open(local_path, "rb") as fh:
            blob_client.upload_blob(fh, **kwargs)
        logger.info("Uploaded %s → %s/%s", local_path, container, blob_path)

    # ── Upload raw bytes ─────────────────────────────────────────────

    def upload_bytes(
        self,
        data: bytes,
        container: str,
        blob_path: str,
        content_type: str | None = None,
    ) -> None:
        """Upload in-memory bytes to a blob."""
        blob_client = self._client.get_blob_client(
            container=container, blob=blob_path,
        )
        kwargs: dict = {"overwrite": True}
        if content_type:
            kwargs["content_settings"] = ContentSettings(content_type=content_type)
        blob_client.upload_blob(data, **kwargs)
        logger.info("Uploaded bytes → %s/%s (%d bytes)", container, blob_path, len(data))
