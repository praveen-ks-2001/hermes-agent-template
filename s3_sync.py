"""
S3-compatible storage sync for Hermes data persistence.

Syncs /data/.hermes/ to any S3-compatible object store (Backblaze B2,
Cloudflare R2, MinIO, etc.) so data survives container restarts on
free-tier platforms without persistent volumes.

Environment variables:
  S3_ENDPOINT           - S3-compatible endpoint URL
  S3_REGION             - Region (default: auto)
  S3_ACCESS_KEY_ID      - Access key ID
  S3_SECRET_ACCESS_KEY  - Secret access key
  S3_BUCKET             - Bucket name
  S3_PREFIX             - Path prefix within bucket (default: hermes-data)
  S3_SYNC_INTERVAL      - Periodic sync interval in seconds (default: 300)
"""

import asyncio
import os
from pathlib import Path

EXCLUDE_NAMES = {"gateway.pid"}


class S3Sync:
    """Sync HERMES_HOME to/from S3-compatible storage."""

    def __init__(self, hermes_home: str):
        self.hermes_home = Path(hermes_home)
        self.bucket = os.environ.get("S3_BUCKET", "").strip()
        self.prefix = os.environ.get("S3_PREFIX", "hermes-data").strip("/")
        self.interval = int(os.environ.get("S3_SYNC_INTERVAL", "300"))
        self._enabled = bool(
            self.bucket
            and os.environ.get("S3_ACCESS_KEY_ID")
        )
        self._client = None
        self._periodic_task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _get_client(self):
        if self._client is None and self._enabled:
            import boto3
            self._client = boto3.client(
                "s3",
                endpoint_url=os.environ.get("S3_ENDPOINT"),
                region_name=os.environ.get("S3_REGION", "auto"),
                aws_access_key_id=os.environ.get("S3_ACCESS_KEY_ID"),
                aws_secret_access_key=os.environ.get("S3_SECRET_ACCESS_KEY"),
            )
        return self._client

    async def pull(self):
        """Download all files from S3 to local HERMES_HOME."""
        if not self._enabled:
            return
        client = self._get_client()
        if client is None:
            return
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._do_pull, client)
        except Exception as exc:
            print(f"[s3-sync] pull failed: {exc}", flush=True)

    async def push(self):
        """Upload local HERMES_HOME files to S3."""
        if not self._enabled:
            return
        client = self._get_client()
        if client is None:
            return
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._do_push, client)
        except Exception as exc:
            print(f"[s3-sync] push failed: {exc}", flush=True)

    def _do_pull(self, client):
        from botocore.exceptions import ClientError
        try:
            client.head_bucket(Bucket=self.bucket)
        except ClientError:
            return

        paginator = client.get_paginator("list_objects_v2")
        pulled = 0
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                rel = key[len(self.prefix):].lstrip("/")
                if not rel or rel in EXCLUDE_NAMES:
                    continue
                local = self.hermes_home / rel
                local.parent.mkdir(parents=True, exist_ok=True)
                client.download_file(self.bucket, key, str(local))
                pulled += 1
        if pulled:
            print(f"[s3-sync] pulled {pulled} files from s3://{self.bucket}/{self.prefix}", flush=True)

    def _do_push(self, client):
        from botocore.exceptions import ClientError
        try:
            client.head_bucket(Bucket=self.bucket)
        except ClientError:
            try:
                client.create_bucket(Bucket=self.bucket)
                print(f"[s3-sync] created bucket {self.bucket}", flush=True)
            except Exception as exc:
                print(f"[s3-sync] cannot create bucket {self.bucket}: {exc}", flush=True)
                return

        pushed = 0
        for f in sorted(self.hermes_home.rglob("*")):
            if not f.is_file() or f.name in EXCLUDE_NAMES:
                continue
            rel = f.relative_to(self.hermes_home).as_posix()
            key = f"{self.prefix}/{rel}"
            client.upload_file(str(f), self.bucket, key)
            pushed += 1
        if pushed:
            print(f"[s3-sync] pushed {pushed} files to s3://{self.bucket}/{self.prefix}", flush=True)

    async def start_periodic(self):
        if not self._enabled:
            return

        async def _loop():
            while True:
                await asyncio.sleep(self.interval)
                await self.push()

        self._periodic_task = asyncio.create_task(_loop())

    async def stop_periodic(self):
        if self._periodic_task:
            self._periodic_task.cancel()
            try:
                await self._periodic_task
            except asyncio.CancelledError:
                pass
            self._periodic_task = None
