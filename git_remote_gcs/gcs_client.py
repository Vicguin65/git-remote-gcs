"""
Google Cloud Storage client for git-remote-gcs.

Handles all GCS operations: listing refs, uploading/downloading bundles,
locking, HEAD management, and branch protection.

Storage layout in GCS:
    <prefix>/HEAD                          - contains the default branch ref
    <prefix>/refs/heads/<branch>/<sha>.bundle  - git bundle for a branch
    <prefix>/refs/tags/<tag>/<sha>.bundle      - git bundle for a tag
    <prefix>/refs/heads/<branch>/LOCK.lock     - lock file for concurrent push protection
    <prefix>/refs/heads/<branch>/PROTECTED     - marker for protected branches
"""

import json
import os
import re
import sys
import time
from typing import Optional

from google.cloud import storage
from google.api_core import exceptions as gcs_exceptions


def log(msg: str) -> None:
    if os.environ.get("GIT_REMOTE_GCS_VERBOSE", "0") == "1":
        print(f"git-remote-gcs [gcs]: {msg}", file=sys.stderr)


class GCSClient:
    """Client for interacting with Git data stored in GCS."""

    def __init__(self, bucket_name: str, prefix: str):
        self.bucket_name = bucket_name
        self.prefix = prefix
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket_name)

    def _key(self, *parts: str) -> str:
        """Build a GCS object key from parts, respecting the prefix."""
        all_parts = [p for p in [self.prefix] + list(parts) if p]
        return "/".join(all_parts)

    # ── Ref operations ──────────────────────────────────────────────

    def list_refs(self) -> dict[str, str]:
        """List all refs and their SHAs by scanning for bundle objects.

        Returns a dict of {ref: sha}, e.g.:
            {"refs/heads/main": "abc123...", "refs/tags/v1.0": "def456..."}
        """
        refs: dict[str, str] = {}
        prefix = self._key("refs/")
        log(f"Listing refs under {prefix}")

        blobs = self._client.list_blobs(self.bucket_name, prefix=prefix)
        for blob in blobs:
            name = blob.name
            if not name.endswith(".bundle"):
                continue

            # Extract ref and sha from path:
            # <prefix>/refs/heads/main/abc123.bundle -> refs/heads/main, abc123
            if self.prefix:
                rel_path = name[len(self.prefix) + 1:]
            else:
                rel_path = name

            # Split off the filename
            parts = rel_path.rsplit("/", 1)
            if len(parts) != 2:
                continue

            ref = parts[0]
            sha = parts[1].replace(".bundle", "")

            # Validate SHA format (40 hex chars)
            if re.match(r"^[0-9a-f]{40}$", sha):
                if ref in refs:
                    # Multiple bundles for same ref - take the newest
                    log(f"Warning: multiple bundles for {ref}")
                refs[ref] = sha

        return refs

    def list_bundles(self, ref: str) -> list[str]:
        """List all bundle object keys for a given ref."""
        prefix = self._key(ref) + "/"
        bundles = []
        blobs = self._client.list_blobs(self.bucket_name, prefix=prefix)
        for blob in blobs:
            if blob.name.endswith(".bundle"):
                bundles.append(blob.name)
        return bundles

    # ── HEAD management ─────────────────────────────────────────────

    def get_head(self) -> Optional[str]:
        """Get the remote HEAD ref (e.g. 'refs/heads/main')."""
        key = self._key("HEAD")
        blob = self._bucket.blob(key)
        try:
            content = blob.download_as_text()
            return content.strip()
        except gcs_exceptions.NotFound:
            return None

    def set_head(self, ref: str) -> None:
        """Set the remote HEAD to point to a ref."""
        key = self._key("HEAD")
        blob = self._bucket.blob(key)
        blob.upload_from_string(ref, content_type="text/plain")
        log(f"Set HEAD -> {ref}")

    # ── Bundle upload/download ──────────────────────────────────────

    def upload_bundle(self, ref: str, sha: str, local_path: str) -> None:
        """Upload a git bundle to GCS."""
        key = self._key(ref, f"{sha}.bundle")
        blob = self._bucket.blob(key)
        blob.upload_from_filename(local_path)
        log(f"Uploaded bundle: {key}")

    def download_bundle(self, ref: str, sha: str, local_path: str) -> None:
        """Download a git bundle from GCS."""
        key = self._key(ref, f"{sha}.bundle")
        blob = self._bucket.blob(key)
        blob.download_to_filename(local_path)
        log(f"Downloaded bundle: {key}")

    def delete_blob(self, key: str) -> None:
        """Delete a blob by its full key."""
        blob = self._bucket.blob(key)
        blob.delete()
        log(f"Deleted: {key}")

    # ── Ref deletion ────────────────────────────────────────────────

    def delete_ref(self, ref: str) -> None:
        """Delete all bundles and metadata for a ref."""
        prefix = self._key(ref) + "/"
        blobs = list(self._client.list_blobs(self.bucket_name, prefix=prefix))
        for blob in blobs:
            blob.delete()
        log(f"Deleted ref: {ref}")

        # Update HEAD if it pointed to this ref
        head = self.get_head()
        if head == ref:
            blob = self._bucket.blob(self._key("HEAD"))
            try:
                blob.delete()
            except gcs_exceptions.NotFound:
                pass

    # ── Locking ─────────────────────────────────────────────────────

    def acquire_lock(self, ref: str, ttl: int = 60) -> Optional[str]:
        """Acquire a lock for a ref using GCS preconditions.

        GCS doesn't support IfNoneMatch="*" like S3 conditional writes,
        so we use generation-match preconditions instead:
        - If the lock doesn't exist, create with if_generation_match=0
          (only succeeds if blob doesn't exist)
        - If it exists but is stale (older than TTL), delete and retry

        Returns the lock key on success, None on failure.
        """
        lock_key = self._key(ref, "LOCK.lock")
        lock_blob = self._bucket.blob(lock_key)

        lock_data = json.dumps({
            "timestamp": time.time(),
            "pid": os.getpid(),
            "host": os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
        })

        try:
            # Try to create the lock (only succeeds if it doesn't exist)
            lock_blob.upload_from_string(
                lock_data,
                content_type="application/json",
                if_generation_match=0,
            )
            log(f"Acquired lock: {lock_key}")
            return lock_key
        except gcs_exceptions.PreconditionFailed:
            # Lock exists - check if it's stale
            log(f"Lock exists at {lock_key}, checking staleness")
            try:
                lock_blob.reload()
                existing_data = lock_blob.download_as_text()
                existing = json.loads(existing_data)
                age = time.time() - existing.get("timestamp", 0)

                if age > ttl:
                    log(f"Lock is stale ({age:.0f}s > {ttl}s), replacing")
                    # Delete stale lock using its generation to avoid races
                    lock_blob.delete(if_generation_match=lock_blob.generation)
                    # Retry creation
                    try:
                        lock_blob.upload_from_string(
                            lock_data,
                            content_type="application/json",
                            if_generation_match=0,
                        )
                        log(
                            f"Acquired lock after clearing stale lock: {lock_key}")
                        return lock_key
                    except gcs_exceptions.PreconditionFailed:
                        log("Another client acquired the lock first")
                        return None
                else:
                    log(f"Lock is fresh ({age:.0f}s < {ttl}s)")
                    return None
            except Exception as e:
                log(f"Error checking lock: {e}")
                return None

    def release_lock(self, ref: str, lock_key: str) -> None:
        """Release a previously acquired lock."""
        lock_blob = self._bucket.blob(lock_key)
        try:
            lock_blob.delete()
            log(f"Released lock: {lock_key}")
        except gcs_exceptions.NotFound:
            log(f"Lock already released: {lock_key}")

    # ── Branch protection ───────────────────────────────────────────

    def is_protected(self, ref: str) -> bool:
        """Check if a ref is protected."""
        key = self._key(ref, "PROTECTED")
        blob = self._bucket.blob(key)
        return blob.exists()

    def protect_ref(self, ref: str) -> None:
        """Mark a ref as protected."""
        key = self._key(ref, "PROTECTED")
        blob = self._bucket.blob(key)
        blob.upload_from_string("", content_type="text/plain")
        log(f"Protected: {ref}")

    def unprotect_ref(self, ref: str) -> None:
        """Remove protection from a ref."""
        key = self._key(ref, "PROTECTED")
        blob = self._bucket.blob(key)
        try:
            blob.delete()
            log(f"Unprotected: {ref}")
        except gcs_exceptions.NotFound:
            pass
