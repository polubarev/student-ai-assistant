from pathlib import Path
from typing import Dict, Tuple
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import os
import logging
from urllib.request import Request as UrlRequest, urlopen

import google.auth
from google.cloud import storage
from google.auth.transport.requests import Request

logger = logging.getLogger(__name__)


class GCSStorageService:
    """Storage service for downloading files from Google Cloud Storage."""

    def __init__(self, client: storage.Client | None = None):
        self.client = client or storage.Client()

    @staticmethod
    def parse_gcs_uri(gcs_uri: str) -> Tuple[str, str]:
        """Parse `gs://bucket/object` URI into bucket and object path."""
        if not gcs_uri or not gcs_uri.startswith("gs://"):
            raise ValueError("GCS URI must start with 'gs://'.")

        raw = gcs_uri[5:]
        if "/" not in raw:
            raise ValueError("GCS URI must include an object path, e.g. gs://bucket/path/file.m4a")

        bucket, blob_name = raw.split("/", 1)
        if not bucket or not blob_name:
            raise ValueError("Invalid GCS URI. Expected format: gs://bucket/path/file.ext")
        return bucket, blob_name

    def download_to_path(self, gcs_uri: str, destination_path: Path) -> Dict[str, str | int]:
        """Download object from GCS URI to local destination path."""
        bucket_name, blob_name = self.parse_gcs_uri(gcs_uri)
        bucket = self.client.bucket(bucket_name)
        blob = bucket.get_blob(blob_name)
        if blob is None:
            raise FileNotFoundError(f"GCS object not found: {gcs_uri}")

        destination_path.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(destination_path))

        # Defensive fallback: in some flows blob metadata can be partially missing.
        # Use downloaded file size so UI always shows a correct size.
        size_bytes = int(blob.size) if blob.size is not None else int(destination_path.stat().st_size)

        return {
            "name": Path(blob_name).name,
            "size": size_bytes,
            "content_type": blob.content_type or "",
            "updated": blob.updated.isoformat() if blob.updated else "",
        }

    def list_object_names(self, bucket_name: str, prefix: str = "", max_results: int = 10) -> list[str]:
        """List object names in a bucket for diagnostics."""
        blobs = self.client.list_blobs(bucket_name, prefix=prefix, max_results=max_results)
        return [str(blob.name) for blob in blobs]

    @staticmethod
    def _is_valid_service_account_email(value: str | None) -> bool:
        if not value:
            return False
        cleaned = value.strip()
        return bool(cleaned and cleaned.lower() != "default" and "@" in cleaned)

    def _resolve_service_account_email(self, base_creds, signing_creds) -> str | None:
        """
        Resolve runtime service account email for IAM SignBlob requests.
        Priority:
        1) Explicit env var.
        2) Credential objects.
        3) Metadata server (Cloud Run / GCE).
        """
        env_email = (os.getenv("GCS_SIGNER_SERVICE_ACCOUNT_EMAIL") or "").strip()
        if self._is_valid_service_account_email(env_email):
            return env_email

        for creds in (base_creds, signing_creds):
            candidate = str(getattr(creds, "service_account_email", "") or "").strip()
            if self._is_valid_service_account_email(candidate):
                return candidate

        metadata_urls = (
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
            "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/email",
        )
        for metadata_url in metadata_urls:
            try:
                req = UrlRequest(metadata_url, headers={"Metadata-Flavor": "Google"})
                with urlopen(req, timeout=2) as resp:
                    candidate = resp.read().decode("utf-8").strip()
                if self._is_valid_service_account_email(candidate):
                    return candidate
            except Exception:
                continue

        return None

    def _get_signing_identity(self) -> Tuple[str, str]:
        """
        Return (service_account_email, access_token) for runtime signing.
        Works in Cloud Run/Compute Engine without a local private key file.
        """
        base_creds = self.client._credentials

        # Use a cloud-platform scoped token for IAMCredentials SignBlob API.
        signing_creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        if not signing_creds.valid or signing_creds.expired:
            signing_creds.refresh(Request())

        access_token = getattr(signing_creds, "token", None)
        service_account_email = self._resolve_service_account_email(base_creds, signing_creds)

        if not access_token:
            raise ValueError("Could not obtain access token for signed upload generation.")
        if not service_account_email:
            raise ValueError(
                "Could not determine service account email for signed upload generation. "
                "Set GCS_SIGNER_SERVICE_ACCOUNT_EMAIL to the Cloud Run runtime service account email."
            )
        return service_account_email, access_token

    def create_signed_upload_form(
        self,
        bucket_name: str,
        key_prefix: str,
        success_redirect_url: str,
        expiration_minutes: int = 10,
        max_size_bytes: int = 10 * 1024 * 1024 * 1024,  # 10 GB
    ) -> Dict[str, object]:
        """
        Create a V4 signed POST policy for direct browser upload to GCS.
        """
        if not bucket_name:
            raise ValueError("bucket_name is required")
        if not key_prefix:
            raise ValueError("key_prefix is required")
        if not success_redirect_url:
            raise ValueError("success_redirect_url is required")

        expires_at = datetime.now(timezone.utc) + timedelta(minutes=expiration_minutes)
        bucket = self.client.bucket(bucket_name)
        object_key = f"{key_prefix}upload-{uuid4().hex}.bin"
        blob = bucket.blob(object_key)
        service_account_email, access_token = self._get_signing_identity()

        logger.info(
            "Preparing signed upload form: bucket=%s key_prefix=%s storage_version=%s client_has_post=%s blob_has_post=%s",
            bucket_name,
            key_prefix,
            getattr(storage, "__version__", "unknown"),
            hasattr(self.client, "generate_signed_post_policy_v4"),
            hasattr(blob, "generate_signed_post_policy_v4"),
        )

        post_conditions = [
            ["eq", "$key", object_key],
            ["content-length-range", 1, max_size_bytes],
        ]
        post_fields = {
            "key": object_key,
            "success_action_status": "201",
        }

        # Preferred path: signed POST policy (no JS upload code needed).
        if hasattr(self.client, "generate_signed_post_policy_v4"):
            try:
                policy = self.client.generate_signed_post_policy_v4(
                    bucket_name=bucket_name,
                    blob_name=object_key,
                    expiration=timedelta(minutes=expiration_minutes),
                    conditions=post_conditions,
                    fields=post_fields,
                    service_account_email=service_account_email,
                    access_token=access_token,
                )
                logger.info("Using signed POST policy via storage client method.")
                return {
                    "mode": "post",
                    "url": policy["url"],
                    "fields": policy["fields"],
                    "expires_at": expires_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "bucket_name": bucket_name,
                    "key_prefix": key_prefix,
                    "object_key": object_key,
                    "success_redirect_url": success_redirect_url,
                }
            except TypeError:
                # Older client versions may not support named kwargs for this method.
                try:
                    policy = self.client.generate_signed_post_policy_v4(
                        bucket_name,
                        object_key,
                        timedelta(minutes=expiration_minutes),
                        conditions=post_conditions,
                        fields=post_fields,
                        service_account_email=service_account_email,
                        access_token=access_token,
                    )
                    logger.info("Using signed POST policy via storage client method (positional args).")
                    return {
                        "mode": "post",
                        "url": policy["url"],
                        "fields": policy["fields"],
                        "expires_at": expires_at.strftime("%Y-%m-%d %H:%M:%S"),
                        "bucket_name": bucket_name,
                        "key_prefix": key_prefix,
                        "object_key": object_key,
                        "success_redirect_url": success_redirect_url,
                    }
                except Exception:
                    logger.exception("Client signed POST policy generation failed, trying blob method.")
            except Exception:
                logger.exception("Client signed POST policy generation failed, trying blob method.")

        if hasattr(blob, "generate_signed_post_policy_v4"):
            try:
                policy = blob.generate_signed_post_policy_v4(
                    expiration=timedelta(minutes=expiration_minutes),
                    service_account_email=service_account_email,
                    access_token=access_token,
                    conditions=post_conditions,
                    fields=post_fields,
                )
                logger.info("Using signed POST policy via blob method.")
                return {
                    "mode": "post",
                    "url": policy["url"],
                    "fields": policy["fields"],
                    "expires_at": expires_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "bucket_name": bucket_name,
                    "key_prefix": key_prefix,
                    "object_key": object_key,
                    "success_redirect_url": success_redirect_url,
                }
            except Exception:
                logger.exception("Blob signed POST policy generation failed, falling back to PUT URL.")

        # Compatibility fallback: signed PUT URL.
        put_blob = bucket.blob(object_key)
        upload_url = put_blob.generate_signed_url(
            version="v4",
            method="PUT",
            expiration=timedelta(minutes=expiration_minutes),
            service_account_email=service_account_email,
            access_token=access_token,
        )
        logger.warning("Falling back to signed PUT URL upload path.")
        return {
            "mode": "put",
            "url": upload_url,
            "fields": {},
            "expires_at": expires_at.strftime("%Y-%m-%d %H:%M:%S"),
            "bucket_name": bucket_name,
            "object_key": object_key,
            "success_redirect_url": success_redirect_url,
        }
