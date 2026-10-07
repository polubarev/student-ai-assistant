from pathlib import Path
from typing import Dict, Tuple
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import os
import logging
from urllib.request import Request as UrlRequest, urlopen
from config import Config

import google.auth
from google.cloud import storage
from google.cloud.storage.retry import DEFAULT_RETRY
from google.auth.transport.requests import Request

logger = logging.getLogger(__name__)


class _BoundedWriter:
    def __init__(self, output, limit):
        self.output = output
        self.limit = limit
        self.written = 0

    def write(self, data):
        if self.written + len(data) > self.limit:
            raise ValueError("Download exceeds the supported size")
        count = self.output.write(data)
        self.written += count
        return count

    def __getattr__(self, name):
        return getattr(self.output, name)


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

    def download_to_path(
        self, gcs_uri: str, destination_path: Path, *, expected_bucket: str,
        expected_object: str, max_size_bytes: int, max_text_bytes: int,
    ) -> Dict[str, str | int]:
        """Download one authorized, size-checked generation; never list other objects."""
        bucket_name, blob_name = self.parse_gcs_uri(gcs_uri)
        if (bucket_name, blob_name) != (expected_bucket, expected_object):
            raise PermissionError("Object does not match the authorized upload")
        blob = self.client.bucket(bucket_name).get_blob(blob_name, timeout=30, retry=DEFAULT_RETRY.with_deadline(30))
        if blob is None:
            raise FileNotFoundError("Uploaded object is not available yet")
        limit = max_size_bytes
        if (blob.content_type or "").startswith("text/") or blob_name.lower().endswith(".txt"):
            limit = min(limit, max_text_bytes)
        if blob.size is None or not 0 < int(blob.size) <= limit or blob.generation is None:
            raise ValueError("Uploaded object exceeds the supported size or has invalid metadata")
        destination_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        created = False
        try:
            with destination_path.open("xb") as output:
                created = True
                blob.download_to_file(
                    _BoundedWriter(output, limit), if_generation_match=blob.generation,
                    raw_download=True, timeout=60, retry=DEFAULT_RETRY.with_deadline(60),
                )
            if destination_path.stat().st_size != int(blob.size):
                raise ValueError("Uploaded object size changed")
        except Exception:
            if created:
                destination_path.unlink(missing_ok=True)
            raise
        return {
            "name": Path(blob_name).name,
            "size": int(blob.size),
            "content_type": blob.content_type or "",
            "updated": blob.updated.isoformat() if blob.updated else "",
            "generation": str(blob.generation),
        }

    def media_metadata(self, gcs_uri, *, expected_bucket, expected_object):
        """Inspect an authorized generation without copying media into RAM-backed /tmp."""
        bucket, key = self.parse_gcs_uri(gcs_uri)
        if (bucket, key) != (expected_bucket, expected_object):
            raise PermissionError("Object does not match the authorized upload")
        blob = self.client.bucket(bucket).get_blob(key, timeout=30, retry=DEFAULT_RETRY.with_deadline(30))
        if blob is None:
            raise FileNotFoundError("Uploaded object is not available yet")
        if blob.size is None or not 0 < int(blob.size) <= Config.MAX_UPLOAD_BYTES or blob.generation is None:
            raise ValueError("Uploaded object exceeds the supported size or has invalid metadata")
        return {"name": Path(key).name, "size": int(blob.size),
                "content_type": blob.content_type or "", "generation": str(blob.generation)}

    def signed_media_url(self, gcs_uri, *, expected_bucket, expected_object, generation):
        """Create a short-lived read URL for the exact, previously inspected generation."""
        bucket, key = self.parse_gcs_uri(gcs_uri)
        if (bucket, key) != (expected_bucket, expected_object) or not str(generation).isdigit():
            raise PermissionError("Object does not match the authorized upload")
        email, token = self._get_signing_identity()
        return self.client.bucket(bucket).blob(key).generate_signed_url(
            version="v4", expiration=timedelta(hours=1), method="GET",
            query_parameters={"generation": str(generation)},
            service_account_email=email, access_token=token,
        )

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
        self, bucket_name: str, key_prefix: str, success_redirect_url: str,
        expiration_minutes: int = 60, max_size_bytes: int = Config.MAX_UPLOAD_BYTES,
    ) -> Dict[str, object]:
        """Issue only a size-limited POST policy. Fail closed if signing fails."""
        if not bucket_name or not key_prefix or not success_redirect_url or max_size_bytes <= 0:
            raise ValueError("Invalid upload configuration")
        object_key = f"{key_prefix}upload-{uuid4().hex}.bin"
        service_account_email, access_token = self._get_signing_identity()
        policy = self.client.generate_signed_post_policy_v4(
            bucket_name=bucket_name, blob_name=object_key,
            expiration=timedelta(minutes=expiration_minutes),
            conditions=[["eq", "$key", object_key], ["content-length-range", 1, max_size_bytes],
                        ["starts-with", "$Content-Type", ""]],
            fields={"key": object_key, "success_action_status": "201"},
            service_account_email=service_account_email, access_token=access_token,
        )
        return {
            "mode": "post", "url": policy["url"], "fields": policy["fields"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=expiration_minutes)).isoformat(),
            "bucket_name": bucket_name, "object_key": object_key,
            "success_redirect_url": success_redirect_url, "max_size_bytes": max_size_bytes,
        }
