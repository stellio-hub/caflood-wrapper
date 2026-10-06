#!/usr/bin/env python3
"""
Dev check for the file-storage (S3) endpoint: uploads a file to a bucket,
fetches it back using the returned "s3.url", checks the round trip is
byte-identical, then deletes the object (unless --keep). Uses the same
FileStorageClient / Config as the pipeline (caflood_common.py), so it tests
exactly what caflood_build.py (DEM fetch) and caflood_publish.py (results
upload) do.

Endpoint: FILE_STORAGE_URL. Auth: a bearer token from the STELLIO_*
credentials. The endpoint (auth.groovy) requires an RS256 token from the
realm it trusts, with that exact "iss" and sub/iat/exp/jti claims - the
claims are printed first, so a wrong realm is obvious before any request.

Usage:
  python check_file_storage.py test8DEMFinal.asc            # upload, fetch, compare, delete
  python check_file_storage.py test8DEMFinal.asc --keep     # ... and keep the object
  python check_file_storage.py --fetch-only <s3.url or key> [--out fetched.asc]
  python check_file_storage.py --delete <s3.url or key>
  (--bucket defaults to urbaquantum-caflood-inputs-dem)

Exit codes: 0 = ok, 1 = a storage call failed or the round trip differs,
2 = configuration / token error.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from caflood_common import (
    Config,
    ConfigError,
    FileStorageClient,
    FileStorageError,
)

DEFAULT_BUCKET = "urbaquantum-caflood-inputs-dem"
# Claims auth.groovy requires on top of a valid signature + issuer.
REQUIRED_CLAIMS = ("sub", "iat", "exp", "jti")


def print_token_claims(token: str) -> None:
    """Decode (without verifying) the JWT payload - never prints the token."""
    payload_b64 = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
    print("Token claims:")
    print(f"  iss = {claims.get('iss')}")
    print(f"  azp = {claims.get('azp')}")
    for claim in ("iat", "exp"):
        if claim in claims:
            print(f"  {claim} = {datetime.fromtimestamp(claims[claim], timezone.utc).isoformat()}")
    missing = [c for c in REQUIRED_CLAIMS if c not in claims]
    print(f"  required {list(REQUIRED_CLAIMS)} -> missing: {missing or 'none'}")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("file", nargs="?", help="File to upload, fetch back and delete")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--keep", action="store_true", help="Don't delete the uploaded object")
    parser.add_argument("--fetch-only", metavar="ID", help="Only fetch this object")
    parser.add_argument("--delete", metavar="ID", help="Only delete this object")
    parser.add_argument("--out", help="Save the fetched file here")
    args = parser.parse_args()
    if sum(bool(x) for x in (args.file, args.fetch_only, args.delete)) != 1:
        parser.error("give exactly one of: a file to upload, --fetch-only ID, --delete ID")

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    storage = FileStorageClient(config)
    print(f"File storage: {config.file_storage_url} (bucket {args.bucket})")
    try:
        print_token_claims(storage.access_token())
    except requests.HTTPError as exc:
        print(f"Could not get a token from {config.stellio_token_url}: {exc}", file=sys.stderr)
        return 2

    try:
        if args.delete:
            storage.delete(args.bucket, args.delete)
            print(f"\nDeleted {args.delete}")
            return 0

        if args.fetch_only:
            fetched = storage.fetch(args.bucket, args.fetch_only)
            print(f"\nFetched {args.fetch_only}: {len(fetched)} bytes")
        else:
            data = Path(args.file).read_bytes()
            print(f"\nUploading {args.file} ({len(data)} bytes, sha256 {sha256(data)[:16]}...)")
            object_id = storage.upload(args.bucket, data)
            print(f"  -> s3.url: {object_id}")
            fetched = storage.fetch(args.bucket, object_id)
            same = fetched == data
            print(f"Fetched back: {len(fetched)} bytes - {'identical' if same else 'MISMATCH'}")
            if args.keep:
                print("Object kept (--keep)")
            else:
                storage.delete(args.bucket, object_id)
                print("Deleted")
            if not same:
                return 1
    except FileStorageError as exc:
        print(f"  FAILED - {exc}")
        return 1

    if args.out:
        Path(args.out).write_bytes(fetched)
        print(f"Saved to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
