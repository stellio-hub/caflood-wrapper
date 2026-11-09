# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 EGM
"""
Shared plumbing for the CAFLOOD service scripts (caflood_build.py,
caflood_poll.py, caflood_publish.py and the check_* tools):

  - configuration: environment variables, completed by a .env file
    (load_dotenv / Config);
  - StellioClient: reads entities from the broker;
  - FileStorageClient: uploads / fetches / deletes files through the
    NiFi-exposed S3 storage endpoint;
  - SSH access to the CAFLOOD VM (ssh_settings / ssh_argv / run_ssh).

Environment (never argv / stdin, so secrets stay out of NiFi provenance and
`ps`). A .env is loaded first from $CAFLOOD_ENV_FILE, else next to these
scripts, else the current directory; real environment variables win.

  Stellio (required by Config)
    STELLIO_BASE_URL             broker base URL
    STELLIO_TOKEN_URL            OAuth2 client_credentials token endpoint
    STELLIO_CLIENT_ID / STELLIO_CLIENT_SECRET
    STELLIO_CONTEXT_URL          optional NGSI-LD @context (Link header)
    STELLIO_TENANT               optional NGSILD-Tenant header,
                                 e.g. urn:ngsi-ld:tenant:urbaquantum
  File storage (authenticated with the STELLIO_* credentials)
    FILE_STORAGE_URL             storage endpoint (required)
    FILE_STORAGE_OUTPUT_BUCKET   results bucket (caflood_publish.py), default
                                 urbaquantum-caflood-outputs-rasters
  Local folders (caflood_build.py)
    CAFLOOD_WORK_DIR             where job folders / zips are built (default cwd)
    CAFLOOD_DATA_DIR             base for a DEM given as a local path (default cwd)
  CAFLOOD VM (caflood_poll.py, caflood_publish.py)
    CAFLOOD_SSH_TARGET           user@host of the VM (required)
    CAFLOOD_SSH_PORT             default 22
    CAFLOOD_SSH_KEY              private key file (required)
    CAFLOOD_SSH_KNOWN_HOSTS      known_hosts file (required)
    CAFLOOD_RUNS_DIR             run folders on the VM (required)
    CAFLOOD_LOG_RETENTION_DAYS   default 7: how long a reported run's
                                 output.log is kept (caflood_poll.py)
    CAFLOOD_SSH_MULTIPLEX        default 1: reuse one SSH connection (OpenSSH
                                 ControlMaster) for 60 s - set 0 to disable
    CAFLOOD_SSH_CONTROL_DIR      where its socket lives, default the temp dir
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


# --------------------------------------------------------------------------
# Config / credentials
# --------------------------------------------------------------------------


class ConfigError(Exception):
    """Missing/invalid environment configuration - exit code 2."""


class InputResolutionError(Exception):
    """A ServiceExecution input could not be resolved/mapped - exit code 1."""


def load_dotenv() -> None:
    """Best-effort, dependency-free .env loader.

    Neither a plain shell nor NiFi's ExecuteStreamCommand sources .env files
    automatically - "Working Directory" in NiFi only sets the child
    process's CWD, it doesn't inject anything into the environment. So we
    load it ourselves, from (in order): $CAFLOOD_ENV_FILE, a .env next to
    this script, or a .env in the current working directory - first match
    wins. Real environment variables (e.g. a production systemd
    EnvironmentFile=) always take priority: this only fills in variables
    that are not already set.
    """
    candidates: list[Path] = []
    explicit = os.environ.get("CAFLOOD_ENV_FILE")
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path(__file__).resolve().parent / ".env")
    candidates.append(Path.cwd() / ".env")

    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
        # Use print rather than `log` here: callers other than this module's
        # own main() (e.g. check_inputs.py) may not have configured
        # logging yet at this point, and this message should never be lost.
        print(f"Loaded environment overrides from {path}", file=sys.stderr)
        break


@dataclass
class Config:
    stellio_base_url: str
    stellio_token_url: str
    stellio_client_id: str
    stellio_client_secret: str
    stellio_context_url: str | None
    stellio_tenant: str | None
    work_dir: Path
    data_dir: Path
    file_storage_url: str

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()

        def require(name: str) -> str:
            value = os.environ.get(name)
            if not value:
                raise ConfigError(f"Missing required environment variable: {name}")
            return value

        return cls(
            stellio_base_url=require("STELLIO_BASE_URL").rstrip("/"),
            stellio_token_url=require("STELLIO_TOKEN_URL"),
            stellio_client_id=require("STELLIO_CLIENT_ID"),
            stellio_client_secret=require("STELLIO_CLIENT_SECRET"),
            stellio_context_url=os.environ.get("STELLIO_CONTEXT_URL") or None,
            stellio_tenant=os.environ.get("STELLIO_TENANT") or None,
            work_dir=Path(os.environ.get("CAFLOOD_WORK_DIR", ".")).resolve(),
            data_dir=Path(os.environ.get("CAFLOOD_DATA_DIR", ".")).resolve(),
            file_storage_url=require("FILE_STORAGE_URL").rstrip("/"),
        )


# --------------------------------------------------------------------------
# Stellio client
# --------------------------------------------------------------------------


class OAuthTokenProvider:
    """OAuth2 client_credentials token, cached until shortly before expiry."""

    def __init__(
        self,
        session: requests.Session,
        token_url: str,
        client_id: str,
        client_secret: str,
    ):
        self._session = session
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._token_expiry: float = 0.0

    def __call__(self) -> str:
        import time

        if self._token and time.monotonic() < self._token_expiry:
            return self._token

        resp = self._session.post(
            self._token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        # refresh a bit early
        self._token_expiry = time.monotonic() + max(
            payload.get("expires_in", 60) - 15, 5
        )
        return self._token


class StellioClient:
    def __init__(self, config: Config):
        self._config = config
        self._session = requests.Session()
        self._access_token = OAuthTokenProvider(
            self._session,
            config.stellio_token_url,
            config.stellio_client_id,
            config.stellio_client_secret,
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Accept": "application/json",
        }
        if self._config.stellio_context_url:
            headers["Link"] = (
                f"<{self._config.stellio_context_url}>; "
                'rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
            )
        if self._config.stellio_tenant:
            headers["NGSILD-Tenant"] = self._config.stellio_tenant
        return headers

    def get_entity(self, entity_id: str) -> dict[str, Any]:
        url = f"{self._config.stellio_base_url}/ngsi-ld/v1/entities/{entity_id}"
        resp = self._session.get(url, headers=self._headers(), timeout=15)
        if resp.status_code == 404:
            raise InputResolutionError(f"Entity not found in Stellio: {entity_id}")
        resp.raise_for_status()
        return resp.json()


# --------------------------------------------------------------------------
# File storage (S3 behind NiFi endpoints - see check_file_storage.py)
# --------------------------------------------------------------------------


class FileStorageError(Exception):
    """A file-storage upload / fetch / delete did not succeed."""


class FileStorageClient:
    """NiFi-exposed S3 storage endpoint (FILE_STORAGE_URL), one URL routed on
    the HTTP method:
      POST   ?bucket=           raw body -> {"s3.url": "https://<object-store>/<bucket>/<key>"}
      GET    ?bucket=&id=<key>  the file
      DELETE ?bucket=&id=<key>  {"status": "success"}
    id is the bare object key (the full s3.url is refused); fetch() and
    delete() accept either and keep the last path segment. Auth: Keycloak
    bearer token from the STELLIO_* credentials, checked by the endpoint's
    auth.groovy (signature, issuer = the endpoint's realm, sub/iat/exp/jti)."""

    def __init__(self, config: Config):
        self._url = config.file_storage_url
        self._token_url = config.stellio_token_url
        self._session = requests.Session()
        self.access_token = OAuthTokenProvider(
            self._session,
            config.stellio_token_url,
            config.stellio_client_id,
            config.stellio_client_secret,
        )

    def _request(self, method: str, action: str, params: dict[str, str], **kwargs: Any) -> requests.Response:
        headers = {"Authorization": f"Bearer {self.access_token()}", **kwargs.pop("headers", {})}
        resp = self._session.request(
            method, self._url, params=params, headers=headers,
            timeout=300, allow_redirects=False, **kwargs,
        )
        if resp.is_redirect:
            raise FileStorageError(
                f"{action}: HTTP {resp.status_code} redirect to "
                f"{resp.headers.get('Location')!r} - endpoint not routed by the proxy"
            )
        if resp.status_code == 401:
            # NiFi echoes the request body back on auth failure - useless here.
            raise FileStorageError(
                f"{action}: HTTP 401 - token from {self._token_url} rejected by "
                f"{self._url} (not the realm this endpoint trusts?)"
            )
        if resp.status_code == 405:
            raise FileStorageError(
                f"{action}: HTTP 405 - {method} not allowed by {self._url} "
                "(HandleHttpRequest allowed methods?)"
            )
        if not resp.ok:
            # On failure NiFi may echo the request body back (possibly binary):
            # keep a short, ASCII-safe excerpt.
            excerpt = resp.content[:120].decode("ascii", "backslashreplace")
            raise FileStorageError(
                f"{action}: HTTP {resp.status_code} (body excerpt: {excerpt!r}) - if the request "
                "body is echoed back, the NiFi flow failed (e.g. a processor buffer limit for "
                "uploads over ~1 MiB)"
            )
        return resp

    @staticmethod
    def _key(object_id: str) -> str:
        return object_id.rstrip("/").rsplit("/", 1)[-1]

    def upload(self, bucket: str, data: bytes) -> str:
        """Upload `data` to `bucket`; returns the s3.url to fetch it back."""
        resp = self._request(
            "POST", "upload", {"bucket": bucket},
            data=data, headers={"Content-Type": "application/octet-stream"},
        )
        try:
            return resp.json()["s3.url"]
        except (ValueError, KeyError):
            raise FileStorageError(
                f"upload: unexpected response (expected {{\"s3.url\": ...}}): "
                f"{resp.text[:200]!r}"
            ) from None

    def fetch(self, bucket: str, object_id: str) -> bytes:
        """`object_id` is the object key, or the full s3.url from upload()."""
        return self._request("GET", "fetch", {"bucket": bucket, "id": self._key(object_id)}).content

    def delete(self, bucket: str, object_id: str) -> None:
        """`object_id` is the object key, or the full s3.url from upload()."""
        self._request("DELETE", "delete", {"bucket": bucket, "id": self._key(object_id)})



# --------------------------------------------------------------------------
# SSH access to the CAFLOOD VM (caflood_poll.py, caflood_publish.py)
# --------------------------------------------------------------------------

# Run folder names are ServiceExecution ids / their UUID part - anything else
# is refused rather than interpolated into a remote shell command.
_SAFE_FOLDER = re.compile(r"[A-Za-z0-9_.:-]+")


def ssh_settings() -> dict[str, str]:
    load_dotenv()
    env = os.environ.get

    def require(name: str) -> str:
        value = env(name)
        if not value:
            raise ConfigError(f"Missing required environment variable: {name}")
        return value

    return {
        "target": require("CAFLOOD_SSH_TARGET"),
        "port": env("CAFLOOD_SSH_PORT", "22"),
        "key": require("CAFLOOD_SSH_KEY"),
        "known_hosts": require("CAFLOOD_SSH_KNOWN_HOSTS"),
        "runs_dir": require("CAFLOOD_RUNS_DIR"),
        "log_retention_days": env("CAFLOOD_LOG_RETENTION_DAYS", "7"),
        "multiplex": env("CAFLOOD_SSH_MULTIPLEX", "1"),
        "control_dir": env("CAFLOOD_SSH_CONTROL_DIR", tempfile.gettempdir()),
    }


def ssh_argv(settings: dict[str, str], remote_command: str) -> list[str]:
    argv = [
        "ssh",
        "-i", settings["key"],
        "-o", f"UserKnownHostsFile={settings['known_hosts']}",
        "-o", "BatchMode=yes",  # never hang on a password / host-key prompt
    ]
    if settings.get("multiplex", "1") not in ("0", "false", "no"):
        # One shared TCP connection for every call within SSH_PERSIST_S (scan,
        # publish steps, mark-reported): the CAFLOOD VM rate-limits new SSH
        # connections (a 6th within 30 s was refused), and it saves a handshake
        # per call. %C = hash of host/port/user, keeps the socket path short.
        control_dir = settings.get("control_dir") or tempfile.gettempdir()
        argv += [
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={control_dir}/caflood-ssh-%C",
            "-o", f"ControlPersist={SSH_PERSIST_S}",
        ]
    return argv + ["-p", settings["port"], settings["target"], remote_command]


# How long the shared SSH connection stays open after its last use.
SSH_PERSIST_S = 60


def run_ssh(settings: dict[str, str], remote_command: str, input_text: str | None = None) -> str:
    """Run a remote command (optionally feeding `input_text` to its stdin);
    RuntimeError (with ssh's stderr) if it fails."""
    # stderr goes to a file, not a pipe: with ControlPersist, the background
    # master started by the first call may keep the caller's stderr open
    # (older OpenSSH), and reading a pipe to EOF would then wait for it.
    with tempfile.TemporaryFile() as err:
        proc = subprocess.run(
            ssh_argv(settings, remote_command),
            input=input_text, stdout=subprocess.PIPE, stderr=err,
            text=True, errors="replace", timeout=120,
        )
        err.seek(0)
        stderr = err.read().decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ssh exited with {proc.returncode}: {stderr.strip()[:500]}")
    return proc.stdout


def check_folder_name(folder: str) -> None:
    if not _SAFE_FOLDER.fullmatch(folder) or folder in (".", ".."):
        raise ValueError(f"refusing unexpected run folder name {folder!r}")
