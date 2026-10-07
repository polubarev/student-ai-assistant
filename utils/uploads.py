"""Authorize only the exact object issued to this authenticated session."""

import hmac


def authorize_upload(uri, grant, configured_bucket, owner, session_id):
    bucket = (configured_bucket or "").removeprefix("gs://")
    if not bucket or not owner or not session_id or not grant:
        raise PermissionError("This upload is not authorized for this session")
    expected = f"gs://{bucket}/{grant.get('object_key', '')}"
    checks = (
        grant.get("bucket_name") == bucket,
        grant.get("owner") == owner,
        grant.get("session_id") == session_id,
        bool(grant.get("object_key")),
        isinstance(uri, str) and hmac.compare_digest(uri.encode(), expected.encode()),
    )
    if not all(checks):
        raise PermissionError("This upload is not authorized for this session")
    return bucket, grant["object_key"]
