#!/usr/bin/env python3
"""
Management tools for git-remote-gcs repositories.

Provides:
    git-gcs doctor <gcs-uri>        - Diagnose and fix repo issues
    git-gcs protect <gcs-uri> <branch>   - Protect a branch
    git-gcs unprotect <gcs-uri> <branch> - Unprotect a branch
    git-gcs delete-branch <gcs-uri> -b <branch> - Delete a remote branch
"""

import argparse
import json
import re
import sys
import time

from git_remote_gcs.gcs_client import GCSClient
from git_remote_gcs.remote import parse_uri


def cmd_doctor(args: argparse.Namespace) -> None:
    """Diagnose and fix issues with a GCS-backed Git repository."""
    bucket, prefix = parse_uri(args.uri)
    client = GCSClient(bucket, prefix)

    print(f"Analyzing repository at gcs://{bucket}/{prefix}")
    print()

    # Check HEAD
    head = client.get_head()
    if head:
        print(f"HEAD -> {head}")
    else:
        print("WARNING: No HEAD set")

    # List all refs and their bundles
    refs = client.list_refs()
    if not refs:
        print("No refs found. Repository appears empty.")
        return

    print(f"\nFound {len(refs)} ref(s):")
    for ref, sha in sorted(refs.items()):
        protected = client.is_protected(ref)
        prot_str = " [PROTECTED]" if protected else ""
        print(f"  {ref} -> {sha[:12]}{prot_str}")

    # Check for multiple bundles per ref (conflict detection)
    print("\nChecking for conflicts...")
    from google.cloud import storage as gcs_storage

    gcs_client = gcs_storage.Client()
    ref_prefix = client._key("refs/")
    all_blobs = list(gcs_client.list_blobs(bucket, prefix=ref_prefix))

    # Group bundles by ref
    ref_bundles: dict[str, list[str]] = {}
    for blob in all_blobs:
        if not blob.name.endswith(".bundle"):
            continue
        if prefix:
            rel_path = blob.name[len(prefix) + 1 :]
        else:
            rel_path = blob.name
        parts = rel_path.rsplit("/", 1)
        if len(parts) == 2:
            ref = parts[0]
            ref_bundles.setdefault(ref, []).append(blob.name)

    conflicts_found = False
    for ref, bundles in ref_bundles.items():
        if len(bundles) > 1:
            conflicts_found = True
            print(f"\n  CONFLICT: {ref} has {len(bundles)} bundles:")
            for b in bundles:
                print(f"    - {b}")

            if args.delete_bundle:
                # Keep only the newest bundle (last uploaded)
                blobs_with_time = []
                for b_name in bundles:
                    blob = gcs_client.bucket(bucket).blob(b_name)
                    blob.reload()
                    blobs_with_time.append((b_name, blob.updated))
                blobs_with_time.sort(key=lambda x: x[1], reverse=True)

                # Keep the newest, delete the rest
                keep = blobs_with_time[0][0]
                print(f"    Keeping: {keep}")
                for b_name, _ in blobs_with_time[1:]:
                    print(f"    Deleting: {b_name}")
                    gcs_client.bucket(bucket).blob(b_name).delete()
            else:
                print(
                    "    Run with --delete-bundle to remove extras, "
                    "or without to create branches for each."
                )

    if not conflicts_found:
        print("  No conflicts found.")

    # Check for stale locks
    print("\nChecking for stale locks...")
    lock_prefix = client._key("refs/")
    lock_blobs = list(gcs_client.list_blobs(bucket, prefix=lock_prefix))
    stale_locks = []
    for blob in lock_blobs:
        if blob.name.endswith("LOCK.lock"):
            blob.reload()
            try:
                data = json.loads(blob.download_as_text())
                age = time.time() - data.get("timestamp", 0)
                if age > args.lock_ttl:
                    stale_locks.append((blob.name, age, data))
            except (json.JSONDecodeError, Exception):
                stale_locks.append((blob.name, float("inf"), {}))

    if stale_locks:
        for lock_name, age, data in stale_locks:
            host = data.get("host", "unknown")
            print(f"  STALE LOCK: {lock_name} (age: {age:.0f}s, host: {host})")
            if args.lock_ttl:
                print(f"    Removing stale lock...")
                gcs_client.bucket(bucket).blob(lock_name).delete()
    else:
        print("  No stale locks found.")

    # Validate HEAD points to an existing ref
    if head and head not in refs:
        print(f"\nWARNING: HEAD points to non-existent ref '{head}'")
        print("Available refs:")
        for i, ref in enumerate(sorted(refs.keys())):
            print(f"  [{i}] {ref}")

        try:
            choice = input("Select new HEAD ref number (or Enter to skip): ").strip()
            if choice:
                idx = int(choice)
                new_head = sorted(refs.keys())[idx]
                client.set_head(new_head)
                print(f"HEAD updated to {new_head}")
        except (ValueError, IndexError, EOFError):
            pass

    print("\nDone.")


def cmd_protect(args: argparse.Namespace) -> None:
    """Protect a branch from force pushes and deletion."""
    bucket, prefix = parse_uri(args.uri)
    client = GCSClient(bucket, prefix)
    ref = f"refs/heads/{args.branch}"
    client.protect_ref(ref)
    print(f"Protected branch: {args.branch}")


def cmd_unprotect(args: argparse.Namespace) -> None:
    """Remove protection from a branch."""
    bucket, prefix = parse_uri(args.uri)
    client = GCSClient(bucket, prefix)
    ref = f"refs/heads/{args.branch}"
    client.unprotect_ref(ref)
    print(f"Unprotected branch: {args.branch}")


def cmd_delete_branch(args: argparse.Namespace) -> None:
    """Delete a remote branch."""
    bucket, prefix = parse_uri(args.uri)
    client = GCSClient(bucket, prefix)
    ref = f"refs/heads/{args.branch}"

    if client.is_protected(ref):
        print(f"Error: branch '{args.branch}' is protected. Unprotect it first.")
        sys.exit(1)

    bundles = client.list_bundles(ref)
    if not bundles:
        print(f"Branch '{args.branch}' not found on remote.")
        sys.exit(1)

    client.delete_ref(ref)
    print(f"Deleted branch: {args.branch}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="git-gcs",
        description="Management tools for git-remote-gcs repositories",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # doctor
    doctor_parser = subparsers.add_parser(
        "doctor", help="Diagnose and fix repository issues"
    )
    doctor_parser.add_argument("uri", help="GCS URI (gcs://bucket/prefix)")
    doctor_parser.add_argument(
        "--delete-bundle",
        action="store_true",
        help="Delete extra bundles instead of creating branches",
    )
    doctor_parser.add_argument(
        "--lock-ttl",
        type=int,
        default=60,
        help="Lock TTL in seconds for stale lock detection (default: 60)",
    )

    # protect
    protect_parser = subparsers.add_parser("protect", help="Protect a branch")
    protect_parser.add_argument("uri", help="GCS URI (gcs://bucket/prefix)")
    protect_parser.add_argument("branch", help="Branch name to protect")

    # unprotect
    unprotect_parser = subparsers.add_parser("unprotect", help="Unprotect a branch")
    unprotect_parser.add_argument("uri", help="GCS URI (gcs://bucket/prefix)")
    unprotect_parser.add_argument("branch", help="Branch name to unprotect")

    # delete-branch
    delete_parser = subparsers.add_parser(
        "delete-branch", help="Delete a remote branch"
    )
    delete_parser.add_argument("uri", help="GCS URI (gcs://bucket/prefix)")
    delete_parser.add_argument("-b", "--branch", required=True, help="Branch to delete")

    args = parser.parse_args()

    commands = {
        "doctor": cmd_doctor,
        "protect": cmd_protect,
        "unprotect": cmd_unprotect,
        "delete-branch": cmd_delete_branch,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
