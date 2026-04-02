#!/usr/bin/env python3
"""
Git remote helper for Google Cloud Storage.

This implements the git-remote-helpers protocol so that Git can use
GCS buckets as remotes with the gcs:// URI scheme.

Usage:
    git remote add origin gcs://my-bucket/my-repo
    git push origin main
    git clone gcs://my-bucket/my-repo
"""

import os
import re
import sys
import subprocess
import tempfile
import time
from pathlib import Path

from git_remote_gcs.gcs_client import GCSClient

VERBOSE = os.environ.get("GIT_REMOTE_GCS_VERBOSE", "0") == "1"
LOCK_TTL = int(os.environ.get("GIT_REMOTE_GCS_LOCK_TTL", "60"))


def log(msg: str) -> None:
    if VERBOSE:
        print(f"git-remote-gcs: {msg}", file=sys.stderr)


def parse_uri(uri: str) -> tuple[str, str]:
    """Parse a gcs:// URI into (bucket, prefix).

    Supported formats:
        gcs://bucket
        gcs://bucket/prefix
        gcs://bucket/prefix/subprefix
    """
    uri = uri.rstrip("/")
    match = re.match(r"^gcs://([^/]+)(?:/(.+))?$", uri)
    if not match:
        raise ValueError(f"Invalid GCS URI: {uri}")
    bucket = match.group(1)
    prefix = match.group(2) or ""
    return bucket, prefix


def git(*args: str, capture: bool = True, check: bool = True) -> str:
    """Run a git command and return stdout."""
    cmd = ["git"] + list(args)
    log(f"Running: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        check=check,
    )
    if capture:
        return result.stdout.strip()
    return ""


class RemoteHelper:
    """Implements the git remote helper protocol for GCS."""

    def __init__(self, remote_name: str, uri: str):
        self.remote_name = remote_name
        self.uri = uri
        bucket, prefix = parse_uri(uri)
        self.client = GCSClient(bucket, prefix)
        self.capabilities = ["push", "fetch", "option"]
        self.verbosity = 0

    def run(self) -> None:
        """Main loop: read commands from stdin and dispatch."""
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                break

            log(f"Command: {line}")

            if line == "capabilities":
                self._cmd_capabilities()
            elif line.startswith("option"):
                self._cmd_option(line)
            elif line == "list" or line == "list for-push":
                self._cmd_list()
            elif line.startswith("fetch"):
                self._cmd_fetch(line)
            elif line.startswith("push"):
                self._cmd_push(line)
            else:
                log(f"Unknown command: {line}")
                sys.exit(1)

    def _cmd_capabilities(self) -> None:
        for cap in self.capabilities:
            print(cap)
        print()
        sys.stdout.flush()

    def _cmd_option(self, line: str) -> None:
        parts = line.split(" ", 2)
        if len(parts) >= 3 and parts[1] == "verbosity":
            self.verbosity = int(parts[2])
            if self.verbosity >= 2:
                global VERBOSE
                VERBOSE = True
            print("ok")
        else:
            print("unsupported")
        sys.stdout.flush()

    def _cmd_list(self) -> None:
        """List remote refs by scanning GCS for bundle objects."""
        refs = self.client.list_refs()
        head_ref = self.client.get_head()

        for ref, sha in refs.items():
            print(f"{sha} {ref}")

        if head_ref and head_ref in refs:
            print(f"@{head_ref} HEAD")
        elif refs:
            # Default to first ref found
            first_ref = next(iter(refs))
            print(f"@{first_ref} HEAD")

        print()
        sys.stdout.flush()

    def _cmd_fetch(self, first_line: str) -> None:
        """Fetch refs from GCS. Read all fetch lines, then process."""
        fetch_lines = [first_line]
        while True:
            line = sys.stdin.readline().strip()
            if not line:
                break
            fetch_lines.append(line)

        for line in fetch_lines:
            parts = line.split()
            if len(parts) < 3:
                continue
            sha = parts[1]
            ref = parts[2]
            log(f"Fetching {ref} ({sha})")
            self._fetch_ref(ref, sha)

        print()
        sys.stdout.flush()

    def _fetch_ref(self, ref: str, sha: str) -> None:
        """Download a bundle from GCS and unbundle it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            bundle_path = os.path.join(tmpdir, f"{sha}.bundle")
            self.client.download_bundle(ref, sha, bundle_path)

            # Verify and unbundle
            try:
                git("bundle", "verify", bundle_path, check=True)
            except subprocess.CalledProcessError:
                log(f"Warning: bundle verification failed for {ref}")

            git("bundle", "unbundle", bundle_path, check=False)

    def _cmd_push(self, first_line: str) -> None:
        """Push refs to GCS. Read all push lines, then process."""
        push_lines = [first_line]
        while True:
            line = sys.stdin.readline().strip()
            if not line:
                break
            push_lines.append(line)

        for line in push_lines:
            parts = line.split()
            if len(parts) < 2:
                continue
            refspec = parts[1]
            self._push_refspec(refspec)

        print()
        sys.stdout.flush()

    def _push_refspec(self, refspec: str) -> None:
        """Push a single refspec to GCS.

        Handles:
            src:dst      - normal push
            +src:dst     - force push
            :dst         - delete ref
        """
        force = refspec.startswith("+")
        if force:
            refspec = refspec[1:]

        if ":" not in refspec:
            log(f"Invalid refspec: {refspec}")
            print(f"error {refspec} invalid refspec")
            sys.stdout.flush()
            return

        src, dst = refspec.split(":", 1)

        # Delete ref
        if not src:
            log(f"Deleting remote ref {dst}")
            try:
                # Check if branch is protected
                if self.client.is_protected(dst):
                    print(f"error {dst} branch is protected")
                    sys.stdout.flush()
                    return
                self.client.delete_ref(dst)
                print(f"ok {dst}")
            except Exception as e:
                print(f"error {dst} {e}")
            sys.stdout.flush()
            return

        # Normal push
        try:
            # Check if branch is protected and this is a force push
            if force and self.client.is_protected(dst):
                print(f"error {dst} branch is protected, force push not allowed")
                sys.stdout.flush()
                return

            # Check ancestry for non-force pushes
            if not force:
                remote_refs = self.client.list_refs()
                if dst in remote_refs:
                    remote_sha = remote_refs[dst]
                    local_sha = git("rev-parse", src)
                    try:
                        git("merge-base", "--is-ancestor", remote_sha, local_sha)
                    except subprocess.CalledProcessError:
                        print(
                            f"error {dst} non-fast-forward update, "
                            f"use --force to override"
                        )
                        sys.stdout.flush()
                        return

            sha = git("rev-parse", src)
            log(f"Pushing {src} -> {dst} ({sha})")

            # Acquire lock
            lock_key = self.client.acquire_lock(dst, LOCK_TTL)
            if not lock_key:
                print(
                    f'error {dst} "failed to acquire ref lock at '
                    f"{self.client.prefix}/{dst}/LOCK.lock. "
                    f"Another client may be pushing. If this persists beyond "
                    f"{LOCK_TTL}s, run git-gcs doctor {self.uri} "
                    f'--lock-ttl {LOCK_TTL} to inspect and clear stale locks."'
                )
                sys.stdout.flush()
                return

            try:
                # Get previous bundles for cleanup
                old_bundles = self.client.list_bundles(dst)

                # Create bundle
                with tempfile.TemporaryDirectory() as tmpdir:
                    bundle_path = os.path.join(tmpdir, f"{sha}.bundle")
                    git("bundle", "create", bundle_path, src)
                    self.client.upload_bundle(dst, sha, bundle_path)

                # Set HEAD if this is the first push
                if not self.client.get_head():
                    self.client.set_head(dst)

                # Clean up old bundles
                for old_bundle in old_bundles:
                    try:
                        self.client.delete_blob(old_bundle)
                    except Exception:
                        pass

                print(f"ok {dst}")
            finally:
                self.client.release_lock(dst, lock_key)

        except Exception as e:
            print(f"error {dst} {e}")

        sys.stdout.flush()


def main() -> None:
    """Entry point for git-remote-gcs."""
    if len(sys.argv) < 3:
        print("Usage: git-remote-gcs <remote-name> <url>", file=sys.stderr)
        sys.exit(1)

    remote_name = sys.argv[1]
    uri = sys.argv[2]

    log(f"Starting with remote={remote_name}, uri={uri}")

    helper = RemoteHelper(remote_name, uri)
    helper.run()


if __name__ == "__main__":
    main()
