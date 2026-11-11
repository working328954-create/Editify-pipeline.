"""Reliable asset acquisition for the Editify video-editing pipeline.

The module deliberately keeps provider adapters separate from download and
validation code.  It can be used as a library::

    from asset_fetcher_manager import get_b_roll

    path = get_b_roll("tech startup")

or from the command line::

    python asset_fetcher_manager.py fetch "tech startup" --type video

Network providers are best-effort.  A provider failure, missing API key, or
invalid response is recorded and the manager continues with the next source.
Pinterest is opt-in because pin ownership and licensing vary; a Pinterest
download is always marked ``review_required`` in the manifest.

Only files with recognizable image/video container signatures are made visible
in an asset directory.  Downloads are staged in ``assets/temp_downloads`` and
renamed atomically after validation.
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_module
import ipaddress
import json
import mimetypes
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

try:
    import requests
except ImportError:  # pragma: no cover - exercised only in an unconfigured environment
    requests = None  # type: ignore[assignment]

try:
    from PIL import Image
except ImportError:  # pragma: no cover - Pillow is optional
    Image = None  # type: ignore[assignment]


MODULE_DIR = Path(__file__).resolve().parent
WIKIMEDIA_API_URL = "https://commons.wikimedia.org/w/api.php"
PEXELS_PHOTO_API_URL = "https://api.pexels.com/v1/search"
PEXELS_VIDEO_API_URL = "https://api.pexels.com/videos/search"
PIXABAY_IMAGE_API_URL = "https://pixabay.com/api/"
PIXABAY_VIDEO_API_URL = "https://pixabay.com/api/videos/"
PINTEREST_SEARCH_URL = "https://www.pinterest.com/search/pins/?q={query}"
UNSPLASH_SEARCH_URL = "https://api.unsplash.com/search/photos"

OBSCURA_BIN_ENV = "OBSCURA_BIN"
# Stock providers where Obscura's headless fetch (anti-bot bypass) applies.
STOCK_PROVIDERS = frozenset({"pexels", "pixabay", "unsplash"})
# Image formats Pillow converts to standard JPEG.
CONVERT_TO_JPG = frozenset({".avif", ".webp"})
JITTER_MIN_SECONDS = 3.0
JITTER_MAX_SECONDS = 7.0

ASSET_DIRECTORY_NAMES = (
    "raw_footage",
    "b_roll",
    "images",
    "temp_downloads",
)
IMAGE_EXTENSIONS = {".avif", ".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
VIDEO_EXTENSIONS = {".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ogv", ".ts", ".webm"}
AUDIO_EXTENSIONS = {".aif", ".aiff", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".opus", ".wav"}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
REDIRECT_CODES = {301, 302, 303, 307, 308}
SENSITIVE_QUERY_KEYS = {
    "api_key",
    "apikey",
    "access_key",
    "accesskey",
    "key",
    "sig",
    "signature",
    "token",
    "access_token",
    "auth",
    "authorization",
    "credential",
    "secret",
}


class AssetFetcherError(RuntimeError):
    """Base exception for expected asset-manager failures."""


class ConfigurationError(AssetFetcherError):
    """Raised when configuration is missing or unsafe."""


class SourceError(AssetFetcherError):
    """Raised when a provider cannot be queried or has no usable response."""


class UrlPolicyError(SourceError):
    """Raised when an outbound URL violates the configured network policy."""


class DownloadError(AssetFetcherError):
    """Raised when a candidate cannot be downloaded."""


class MediaValidationError(AssetFetcherError):
    """Raised when a staged file is empty or is not a supported media file."""


class NoAssetFound(AssetFetcherError):
    """Raised after all requested sources fail to produce a valid asset."""


def _obscura_bin() -> str | None:
    """Resolve the Obscura binary from OBSCURA_BIN or PATH."""
    exe = os.getenv(OBSCURA_BIN_ENV) or shutil.which("obscura")
    return os.path.normpath(exe) if exe else None


def _download_with_obscura(url: str, destination: Path, max_bytes: int | None = None) -> None:
    """Download a stock asset via Obscura's raw fetch (anti-bot bypass)."""
    exe = _obscura_bin()
    if not exe:
        raise DownloadError(f"Obscura is not installed; set {OBSCURA_BIN_ENV}")
    try:
        result = subprocess.run(
            [exe, "--stealth", "fetch", "--dump", "original", url],
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DownloadError(f"Obscura fetch failed: {type(exc).__name__}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]
        raise DownloadError(f"Obscura fetch failed ({result.returncode}): {' '.join(detail)}")
    if max_bytes is not None and len(result.stdout) > max_bytes:
        raise DownloadError(
            f"Obscura download exceeded configured size limit: {_redact_url(url)}"
        )
    destination.write_bytes(result.stdout)


def _jitter_sleep() -> None:
    """Random 3-7 s pause between stock download requests (scraper stability)."""
    time.sleep(random.uniform(JITTER_MIN_SECONDS, JITTER_MAX_SECONDS))


def _convert_image_to_jpg(path: Path) -> Path:
    """Convert .avif/.webp images to .jpg via Pillow; leave others unchanged."""
    if Image is None:
        return path
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
    except OSError:
        return path
    webp = header.startswith(b"RIFF") and header[8:12] == b"WEBP"
    avif = len(header) >= 12 and header[4:8] == b"ftyp" and header[8:12].lower() in {
        b"avif", b"avis", b"mif1"
    }
    if not (webp or avif) and path.suffix.lower() not in CONVERT_TO_JPG:
        return path
    try:
        with Image.open(path) as img:
            target = path.with_suffix(".jpg")
            img.convert("RGB").save(target, "JPEG", quality=90)
    except Exception:
        return path  # leave the original in place; validation still applies
    path.unlink(missing_ok=True)
    return target


def utc_now() -> str:
    """Return a stable, timezone-aware UTC timestamp for manifests."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, name: str, *, minimum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if minimum is not None and result < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return result


def _numeric_value(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _as_path(value: str | os.PathLike[str] | None, base: Path) -> Path | None:
    if value is None or str(value).strip() == "":
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _safe_title(value: str, fallback: str = "asset") -> str:
    """Convert untrusted titles into conservative Windows-safe path components."""

    normalized = unicodedata.normalize("NFKC", value or "").strip()
    normalized = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "-", normalized)
    normalized = re.sub(r"\s+", "-", normalized)
    normalized = re.sub(r"[^\w.-]+", "-", normalized, flags=re.UNICODE)
    normalized = normalized.strip(" .-")[:100]
    if not normalized:
        normalized = fallback
    if normalized.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[0-9]", normalized.upper()):
        normalized = f"_{normalized}"
    return normalized


def _redact_url(url: str | None) -> str | None:
    if not url:
        return url
    parsed = urlparse(url)
    if not parsed.query:
        return url
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.lower()
        normalized = re.sub(r"[^a-z0-9]", "", lowered)
        is_sensitive = (
            lowered in SENSITIVE_QUERY_KEYS
            or normalized in {re.sub(r"[^a-z0-9]", "", item) for item in SENSITIVE_QUERY_KEYS}
            or lowered.startswith(("x-amz-", "x-goog-"))
            or any(marker in normalized for marker in ("token", "signature", "secret", "credential", "authorization"))
            or normalized.endswith("apikey")
            or normalized.endswith("accesskey")
        )
        if is_sensitive:
            kept.append((key, "<redacted>"))
        else:
            kept.append((key, value))
    return urlunparse(parsed._replace(query=urlencode(kept)))


def _media_type_from_mime_or_url(mime: str | None, url: str | None) -> str | None:
    if mime:
        value = mime.lower().split(";", 1)[0].strip()
        if value.startswith("image/"):
            return "image"
        if value.startswith("video/"):
            return "video"
        if value.startswith("audio/"):
            return "audio"
    if url:
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            return "image"
        if suffix in VIDEO_EXTENSIONS:
            return "video"
        if suffix in AUDIO_EXTENSIONS:
            return "audio"
    return None


def _extension_from_mime_or_url(mime: str | None, url: str | None, media_type: str) -> str:
    if url:
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix in SUPPORTED_EXTENSIONS:
            return suffix
    if mime:
        guessed = mimetypes.guess_extension(mime.split(";", 1)[0].strip())
        if guessed and guessed.lower() in SUPPORTED_EXTENSIONS:
            return guessed.lower()
    if media_type == "image":
        return ".jpg"
    if media_type == "audio":
        return ".wav"
    return ".mp4"


def _metadata_value(metadata: Mapping[str, Any], key: str) -> str:
    value = metadata.get(key, "")
    if isinstance(value, Mapping):
        value = value.get("value", "")
    return str(value or "").strip()


def is_public_domain_license(*values: str | None) -> bool:
    """Return true only for explicit public-domain/CC0 style evidence.

    A filename, category, or an absent license is never considered proof.  The
    allowlist is intentionally conservative; providers can be extended by
    adding a tested, documented expression here.
    """

    text = " ".join(str(value or "") for value in values).lower()
    if not text or "fair use" in text or "unknown" in text:
        return False
    if re.search(r"\b(?:not|non|no|without)\b.{0,24}\bpublic[- ]?domain\b", text):
        return False
    if re.search(r"\b(?:not|non|no|without)\b.{0,24}\bcc[- ]?0\b", text):
        return False
    explicit_markers = (
        "public domain",
        "public-domain",
        "publicdomain",
        "creativecommons.org/publicdomain",
        "cc0",
        "cc zero",
        "pd-us",
        "pd-old",
        "pd-self",
        "pd-art",
        "pd-anon",
        "pd-ineligible",
        "pd-text",
    )
    return any(marker in text for marker in explicit_markers)


def sniff_media_type(header: bytes, path: Path | None = None) -> str | None:
    """Identify a supported image/video container from magic bytes.

    Content-Type and file extensions are treated only as hints.  This function
    intentionally does not accept HTML, JSON, or arbitrary octet streams.
    """

    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image"
    if header.startswith(b"\xff\xd8\xff"):
        return "image"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image"
    if header.startswith(b"BM"):
        return "image"
    if header.startswith(b"II*\x00") or header.startswith(b"MM\x00*"):
        return "image"
    if header.startswith(b"RIFF") and len(header) >= 12:
        riff_type = header[8:12]
        if riff_type == b"WEBP":
            return "image"
        if riff_type == b"WAVE":
            return "audio"
        if riff_type == b"AVI ":
            return "video"
    if header.startswith(b"FLV"):
        return "video"
    if header.startswith(b"fLaC") or header.startswith(b"ID3"):
        return "audio"
    if len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0:
        # MPEG audio frame sync (mp3 without an ID3 tag).
        return "audio"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        # EBML covers WebM and Matroska video containers.
        return "video"
    if header.startswith(b"OggS"):
        lowered = header.lower()
        if b"theora" in lowered or b"video" in lowered:
            return "video"
        if b"vorbis" in lowered or b"opus" in lowered or b"flac" in lowered:
            return "audio"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        brand = header[8:12].lower()
        if brand in {b"avif", b"avis", b"heic", b"heix", b"mif1"}:
            return "image"
        if brand in {b"m4a ", b"m4b ", b"mp4a", b"ac-3", b"ec-3"}:
            return None
        return "video"
    # MPEG transport streams have a sync byte at every 188-byte packet.
    if len(header) >= 376 and header[0] == 0x47 and header[188] == 0x47:
        return "video"
    if path is not None and path.suffix.lower() in SUPPORTED_EXTENSIONS:
        # Do not use an extension by itself as validation evidence.
        return None
    return None


def validate_media_file(path: Path, expected_type: str = "any", max_bytes: int | None = None) -> str:
    """Validate size and recognizable media headers, returning image/video."""

    path = Path(path)
    if not path.is_file():
        raise MediaValidationError(f"Media file does not exist: {path}")
    size = path.stat().st_size
    if size <= 0:
        raise MediaValidationError(f"Media file is empty: {path}")
    if max_bytes is not None and size > max_bytes:
        raise MediaValidationError(f"Media file exceeds the configured size limit: {path}")
    with path.open("rb") as handle:
        header = handle.read(4096)
    actual_type = sniff_media_type(header, path)
    if actual_type is None:
        raise MediaValidationError(f"Unsupported or invalid media header: {path}")
    if expected_type != "any" and actual_type != expected_type:
        raise MediaValidationError(
            f"Expected {expected_type} media but detected {actual_type}: {path}"
        )
    return actual_type


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class AssetFetcherConfig:
    """Runtime configuration loaded from environment or a JSON config file."""

    workspace_root: Path = field(default_factory=lambda: MODULE_DIR)
    asset_root: Path | None = None
    local_fallback_dirs: tuple[Path, ...] = ()
    pexels_api_key: str | None = field(default=None, repr=False)
    pixabay_api_key: str | None = field(default=None, repr=False)
    unsplash_api_key: str | None = field(default=None, repr=False)
    pinterest_cookies_file: Path | None = None
    request_timeout: tuple[float, float] = (10.0, 60.0)
    request_retries: int = 2
    retry_backoff_seconds: float = 0.5
    max_redirects: int = 5
    max_download_bytes: int = 2 * 1024 * 1024 * 1024
    max_results_per_source: int = 8
    require_https: bool = True
    block_private_ips: bool = True
    default_source_order: tuple[str, ...] = ("pexels", "pixabay", "wikimedia", "local")
    wikimedia_public_domain_only: bool = True
    user_agent: str = "EditifyAssetFetcher/1.0 (+local automation; contact project owner)"
    manifest_path: Path | None = None

    def __post_init__(self) -> None:
        self.workspace_root = Path(self.workspace_root).expanduser().resolve()
        self.asset_root = (self.asset_root or self.workspace_root / "assets").expanduser().resolve()
        self.pinterest_cookies_file = (
            Path(self.pinterest_cookies_file).expanduser().resolve()
            if self.pinterest_cookies_file
            else None
        )
        if not self.local_fallback_dirs:
            self.local_fallback_dirs = (self.asset_root,)
        else:
            self.local_fallback_dirs = tuple(Path(item).expanduser().resolve() for item in self.local_fallback_dirs)
        if self.manifest_path is None:
            self.manifest_path = self.asset_root / "asset_manifest.json"
        else:
            self.manifest_path = Path(self.manifest_path).expanduser().resolve()
        if self.request_retries < 0 or self.max_redirects < 0:
            raise ConfigurationError("request_retries and max_redirects must be non-negative")
        if self.max_download_bytes <= 0:
            raise ConfigurationError("max_download_bytes must be positive")

    @property
    def temp_downloads(self) -> Path:
        return self.asset_root / "temp_downloads"

    def ensure_directories(self) -> None:
        for name in ASSET_DIRECTORY_NAMES:
            (self.asset_root / name).mkdir(parents=True, exist_ok=True)
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]

    @classmethod
    def from_env(
        cls,
        workspace_root: str | os.PathLike[str] | None = None,
        config_path: str | os.PathLike[str] | None = None,
    ) -> "AssetFetcherConfig":
        """Load JSON values first, then let explicitly set environment values win."""

        config_file = Path(config_path).expanduser().resolve() if config_path else None
        file_values: dict[str, Any] = {}
        config_base = Path.cwd()
        if config_file:
            config_base = config_file.parent
            try:
                file_values = json.loads(config_file.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise ConfigurationError(f"Config file not found: {config_file}") from exc
            except json.JSONDecodeError as exc:
                raise ConfigurationError(f"Config file is not valid JSON: {config_file}") from exc
            if not isinstance(file_values, dict):
                raise ConfigurationError("Config JSON must contain an object")

        root_value = workspace_root or os.getenv("EDITIFY_WORKSPACE") or file_values.get("workspace_root")
        root = _as_path(root_value, config_base) or MODULE_DIR

        def env_or_file(name: str, file_key: str, default: Any = None) -> Any:
            return os.getenv(name, file_values.get(file_key, default))

        fallback_value = env_or_file("EDITIFY_LOCAL_FALLBACK_DIRS", "local_fallback_dirs")
        if isinstance(fallback_value, str):
            fallback_values: Sequence[Any] = [item for item in fallback_value.split(os.pathsep) if item.strip()]
        else:
            fallback_values = fallback_value or []
        fallback_dirs = tuple(_as_path(item, root) for item in fallback_values)

        source_order_value = env_or_file("EDITIFY_SOURCE_ORDER", "default_source_order")
        if isinstance(source_order_value, str):
            source_order = tuple(item.strip().lower() for item in source_order_value.split(",") if item.strip())
        else:
            source_order = tuple(str(item).lower() for item in (source_order_value or cls.default_source_order))

        cookies = env_or_file("PINTEREST_COOKIES_FILE", "pinterest_cookies_file")
        cookies_path = _as_path(cookies, root)
        asset_root = _as_path(env_or_file("EDITIFY_ASSET_ROOT", "asset_root"), root)
        manifest_path = _as_path(env_or_file("EDITIFY_MANIFEST_PATH", "manifest_path"), root)

        timeout_value = env_or_file("EDITIFY_TIMEOUT_SECONDS", "request_timeout")
        if isinstance(timeout_value, (int, float, str)):
            try:
                timeout_seconds = float(timeout_value)
                if timeout_seconds <= 0:
                    raise ValueError
                request_timeout = (timeout_seconds, timeout_seconds)
            except (TypeError, ValueError) as exc:
                raise ConfigurationError("EDITIFY_TIMEOUT_SECONDS must be numeric") from exc
        else:
            request_timeout = (10.0, 60.0)

        return cls(
            workspace_root=root,
            asset_root=asset_root,
            local_fallback_dirs=tuple(item for item in fallback_dirs if item is not None),
            pexels_api_key=env_or_file("PEXELS_API_KEY", "pexels_api_key"),
            pixabay_api_key=env_or_file("PIXABAY_API_KEY", "pixabay_api_key"),
            unsplash_api_key=env_or_file("UNSPLASH_API_KEY", "unsplash_api_key"),
            pinterest_cookies_file=cookies_path,
            request_timeout=request_timeout,
            request_retries=_as_int(env_or_file("EDITIFY_RETRIES", "request_retries", 2), "EDITIFY_RETRIES", minimum=0),
            max_download_bytes=_as_int(
                env_or_file("EDITIFY_MAX_DOWNLOAD_BYTES", "max_download_bytes", 2 * 1024 * 1024 * 1024),
                "EDITIFY_MAX_DOWNLOAD_BYTES",
                minimum=1,
            ),
            require_https=_as_bool(env_or_file("EDITIFY_REQUIRE_HTTPS", "require_https", True), True),
            block_private_ips=_as_bool(env_or_file("EDITIFY_BLOCK_PRIVATE_IPS", "block_private_ips", True), True),
            default_source_order=source_order,
            wikimedia_public_domain_only=_as_bool(
                env_or_file("EDITIFY_WIKIMEDIA_PUBLIC_DOMAIN_ONLY", "wikimedia_public_domain_only", True), True
            ),
            manifest_path=manifest_path,
        )


@dataclass
class AssetCandidate:
    source: str
    title: str
    page_url: str | None = None
    download_url: str | None = None
    extractor_url: str | None = None
    media_type: str = "any"
    extension: str | None = None
    license_name: str | None = None
    license_url: str | None = None
    attribution: str | None = None
    author: str | None = None
    rights_status: str = "review_required"
    alternate_urls: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.media_type not in {"image", "video", "audio", "any"}:
            raise ValueError(f"Unsupported media type: {self.media_type}")
        if self.download_url is None and self.extractor_url is None and not self.metadata.get("local_path"):
            raise ValueError("AssetCandidate requires a download URL, extractor URL, or local_path")

    @property
    def all_download_urls(self) -> tuple[str, ...]:
        values = [self.download_url, *self.alternate_urls]
        return tuple(dict.fromkeys(value for value in values if value))


@dataclass
class DownloadResult:
    path: Path
    record: dict[str, Any]


class HttpClient:
    """Small requests wrapper with bounded redirects, retries, and size limits."""

    def __init__(self, config: AssetFetcherConfig, session: Any | None = None) -> None:
        if requests is None:
            raise ConfigurationError("requests is required; install requirements.lock in the project environment")
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": config.user_agent, "Accept": "*/*"})

    def _validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise UrlPolicyError(f"Only HTTP(S) URLs are supported: {_redact_url(url)}")
        if parsed.username or parsed.password:
            raise UrlPolicyError("URLs containing embedded credentials are not allowed")
        if self.config.require_https and parsed.scheme != "https":
            raise UrlPolicyError(f"HTTPS is required for provider downloads: {_redact_url(url)}")
        if self.config.block_private_ips:
            try:
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
            except ValueError as exc:
                raise UrlPolicyError(f"Invalid URL port: {_redact_url(url)}") from exc
            try:
                addresses = [ipaddress.ip_address(parsed.hostname)]
            except ValueError:
                try:
                    resolved = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
                except OSError as exc:
                    raise UrlPolicyError(f"Could not resolve download host: {parsed.hostname}") from exc
                addresses = []
                for item in resolved:
                    try:
                        addresses.append(ipaddress.ip_address(item[4][0]))
                    except (IndexError, ValueError):
                        continue
            if not addresses:
                raise UrlPolicyError(f"Could not resolve download host: {parsed.hostname}")
            if any(not address.is_global for address in addresses):
                raise UrlPolicyError(f"Private/loopback download hosts are blocked: {parsed.hostname}")

    def request(self, method: str, url: str, *, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None, stream: bool = False) -> Any:
        current_url = url
        current_params = params
        for redirect_number in range(self.config.max_redirects + 1):
            self._validate_url(current_url)
            last_error: Exception | None = None
            response: Any | None = None
            for attempt in range(self.config.request_retries + 1):
                try:
                    response = self.session.request(
                        method,
                        current_url,
                        params=current_params,
                        headers=headers,
                        timeout=self.config.request_timeout,
                        allow_redirects=False,
                        stream=stream,
                    )
                    current_params = None
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < self.config.request_retries:
                        retry_after = response.headers.get("Retry-After", "")
                        response.close()
                        try:
                            delay = min(float(retry_after), 10.0) if retry_after else self.config.retry_backoff_seconds * (2**attempt)
                        except ValueError:
                            delay = self.config.retry_backoff_seconds * (2**attempt)
                        time.sleep(max(0.0, delay))
                        continue
                    break
                except Exception as exc:  # requests raises several adapter-specific subclasses
                    last_error = exc
                    if attempt >= self.config.request_retries:
                        raise SourceError(
                            f"HTTP request failed for {_redact_url(url)}: {type(exc).__name__}"
                        ) from exc
                    time.sleep(max(0.0, self.config.retry_backoff_seconds * (2**attempt)))
            if response is None:
                raise SourceError(f"No response from {_redact_url(url)}: {last_error}")
            if response.status_code in REDIRECT_CODES:
                location = response.headers.get("Location")
                response.close()
                if not location or redirect_number >= self.config.max_redirects:
                    raise SourceError(f"Too many or invalid redirects for {_redact_url(url)}")
                current_url = urljoin(current_url, location)
                continue
            if response.status_code >= 400:
                status = response.status_code
                response.close()
                raise SourceError(f"HTTP {status} from {_redact_url(url)}")
            self._validate_url(str(getattr(response, "url", None) or current_url))
            return response
        raise SourceError(f"Too many redirects for {_redact_url(url)}")

    def get_json(self, url: str, *, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None) -> dict[str, Any]:
        response = self.request("GET", url, params=params, headers=headers, stream=False)
        try:
            value = response.json()
        except (ValueError, TypeError) as exc:
            raise SourceError(f"Provider returned invalid JSON: {_redact_url(url)}") from exc
        finally:
            response.close()
        if not isinstance(value, dict):
            raise SourceError(f"Provider JSON response was not an object: {_redact_url(url)}")
        return value

    def get_text(self, url: str, *, headers: Mapping[str, str] | None = None) -> str:
        response = self.request("GET", url, headers=headers, stream=False)
        try:
            return response.text
        finally:
            response.close()

    def stream_to_file(self, url: str, destination: Path, *, headers: Mapping[str, str] | None = None) -> str | None:
        response = self.request("GET", url, headers=headers, stream=True)
        content_type = response.headers.get("Content-Type")
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > self.config.max_download_bytes:
                    response.close()
                    raise DownloadError(f"Remote file exceeds configured size limit: {_redact_url(url)}")
            except ValueError:
                pass
        total = 0
        try:
            with Path(destination).open("wb") as handle:
                try:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > self.config.max_download_bytes:
                            raise DownloadError(f"Remote file exceeded configured size limit: {_redact_url(url)}")
                        handle.write(chunk)
                except DownloadError:
                    raise
                except Exception as exc:
                    raise DownloadError(
                        f"Streaming download failed for {_redact_url(url)}: {type(exc).__name__}"
                    ) from exc
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            response.close()
        return content_type


class AssetSource:
    """Provider adapter interface."""

    name = "source"

    def __init__(self, http: HttpClient, config: AssetFetcherConfig) -> None:
        self.http = http
        self.config = config

    def search(
        self,
        keyword: str,
        media_type: str,
        limit: int,
        *,
        source_url: str | None = None,
    ) -> list[AssetCandidate]:
        raise NotImplementedError


class WikimediaCommonsSource(AssetSource):
    """Wikimedia Commons API adapter with explicit public-domain filtering."""

    name = "wikimedia"

    def search(
        self,
        keyword: str,
        media_type: str,
        limit: int,
        *,
        source_url: str | None = None,
    ) -> list[AssetCandidate]:
        if not keyword.strip():
            raise SourceError("Wikimedia search requires a keyword")
        candidates: list[AssetCandidate] = []
        continuation: dict[str, Any] = {}
        # A few continuation pages allow the license filter to find useful
        # results without making an unbounded API crawl.
        for _ in range(3):
            params: dict[str, Any] = {
                "action": "query",
                "format": "json",
                "generator": "search",
                "gsrsearch": f"filetype:audio {keyword}" if media_type == "audio" else keyword,
                "gsrnamespace": 6,
                "gsrlimit": min(max(limit * 3, 10), 50),
                "prop": "imageinfo",
                "iiprop": "url|mime|size|extmetadata",
            }
            params.update(continuation)
            payload = self.http.get_json(WIKIMEDIA_API_URL, params=params)
            pages = payload.get("query", {}).get("pages", {})
            if isinstance(pages, Mapping):
                page_values = pages.values()
            else:
                page_values = []
            for page in page_values:
                if not isinstance(page, Mapping):
                    continue
                imageinfo = page.get("imageinfo", [])
                if not isinstance(imageinfo, list) or not imageinfo:
                    continue
                info = imageinfo[0]
                if not isinstance(info, Mapping):
                    continue
                extmetadata = info.get("extmetadata", {})
                if not isinstance(extmetadata, Mapping):
                    extmetadata = {}
                original_url = str(info.get("url") or "").strip()
                mime = str(info.get("mime") or "").strip()
                detected_type = _media_type_from_mime_or_url(mime, original_url)
                if not original_url or detected_type is None or (media_type != "any" and detected_type != media_type):
                    continue
                license_name = _metadata_value(extmetadata, "LicenseShortName")
                usage_terms = _metadata_value(extmetadata, "UsageTerms")
                license_url = _metadata_value(extmetadata, "LicenseUrl")
                if self.config.wikimedia_public_domain_only and not is_public_domain_license(
                    license_name, usage_terms, license_url
                ):
                    continue
                title = str(page.get("title") or "Wikimedia Commons asset").removeprefix("File:")
                page_url = f"https://commons.wikimedia.org/wiki/{quote(str(page.get('title') or ''), safe=':_()') }"
                author = _metadata_value(extmetadata, "Artist") or _metadata_value(extmetadata, "Credit")
                candidates.append(
                    AssetCandidate(
                        source=self.name,
                        title=title,
                        page_url=page_url,
                        download_url=original_url,
                        media_type=detected_type,
                        extension=_extension_from_mime_or_url(mime, original_url, detected_type),
                        license_name=license_name or usage_terms,
                        license_url=license_url or None,
                        attribution=author or None,
                        author=author or None,
                        rights_status="public_domain",
                        metadata={
                            "commons_page_id": page.get("pageid"),
                            "mime": mime,
                            "license_evidence": {
                                "license_short_name": license_name,
                                "usage_terms": usage_terms,
                                "license_url": license_url,
                            },
                        },
                    )
                )
                if len(candidates) >= limit:
                    return _dedupe_candidates(candidates)[:limit]
            next_continuation = payload.get("continue")
            if not isinstance(next_continuation, Mapping):
                break
            continuation = dict(next_continuation)
        if not candidates:
            raise SourceError("Wikimedia returned no matching public-domain media")
        return _dedupe_candidates(candidates)[:limit]


class PexelsSource(AssetSource):
    """Pexels photo/video search adapter."""

    name = "pexels"

    def search(
        self,
        keyword: str,
        media_type: str,
        limit: int,
        *,
        source_url: str | None = None,
    ) -> list[AssetCandidate]:
        if not self.config.pexels_api_key:
            raise SourceError("PEXELS_API_KEY is not configured")
        if media_type == "image":
            endpoint = PEXELS_PHOTO_API_URL
        else:
            # Pexels has separate endpoints.  "any" prefers video for the
            # asset manager's B-roll use case and can be requested as image
            # explicitly when needed.
            endpoint = PEXELS_VIDEO_API_URL
        payload = self.http.get_json(
            endpoint,
            params={"query": keyword, "per_page": min(max(limit, 1), 80), "page": 1},
            headers={"Authorization": self.config.pexels_api_key},
        )
        candidates: list[AssetCandidate] = []
        records = payload.get("photos" if media_type == "image" else "videos", [])
        if not isinstance(records, list):
            raise SourceError("Pexels returned an unexpected result shape")
        for item in records:
            if not isinstance(item, Mapping):
                continue
            if media_type == "image":
                source_urls = item.get("src", {})
                if not isinstance(source_urls, Mapping):
                    continue
                url = str(source_urls.get("original") or source_urls.get("large2") or source_urls.get("large") or "")
                detected_type = "image"
                title = str(item.get("alt") or f"Pexels photo {item.get('id', '')}")
                page_url = str(item.get("url") or "")
                author = str(item.get("photographer") or "")
                metadata = {"provider_id": item.get("id"), "width": item.get("width"), "height": item.get("height")}
            else:
                files = item.get("video_files", [])
                if not isinstance(files, list):
                    continue
                usable_files = [
                    file
                    for file in files
                    if isinstance(file, Mapping)
                    and str(file.get("link") or "")
                    and str(file.get("file_type") or "video/").startswith("video/")
                ]
                if not usable_files:
                    continue
                selected = max(
                    usable_files,
                    key=lambda file: (_numeric_value(file.get("width")), _numeric_value(file.get("height"))),
                )
                url = str(selected.get("link") or "")
                detected_type = "video"
                title = f"Pexels video {item.get('id', '')}".strip()
                page_url = str(item.get("url") or "")
                author = str(item.get("user", {}).get("name") or "") if isinstance(item.get("user"), Mapping) else ""
                metadata = {
                    "provider_id": item.get("id"),
                    "width": selected.get("width"),
                    "height": selected.get("height"),
                    "duration": item.get("duration"),
                }
            if not url:
                continue
            candidates.append(
                AssetCandidate(
                    source=self.name,
                    title=title,
                    page_url=page_url or None,
                    download_url=url,
                    media_type=detected_type,
                    extension=_extension_from_mime_or_url(None, url, detected_type),
                    license_name="Pexels License",
                    license_url="https://www.pexels.com/license/",
                    attribution=author or None,
                    author=author or None,
                    rights_status="provider_license",
                    metadata=metadata,
                )
            )
        if not candidates:
            raise SourceError("Pexels returned no usable media")
        return _dedupe_candidates(candidates)[:limit]


class PixabaySource(AssetSource):
    """Pixabay image/video search adapter."""

    name = "pixabay"

    def search(
        self,
        keyword: str,
        media_type: str,
        limit: int,
        *,
        source_url: str | None = None,
    ) -> list[AssetCandidate]:
        if not self.config.pixabay_api_key:
            raise SourceError("PIXABAY_API_KEY is not configured")
        is_image = media_type == "image"
        endpoint = PIXABAY_IMAGE_API_URL if is_image else PIXABAY_VIDEO_API_URL
        params: dict[str, Any] = {
            "key": self.config.pixabay_api_key,
            "q": keyword,
            "per_page": min(max(limit, 1), 200),
            "page": 1,
            "safesearch": "true",
        }
        if is_image:
            params.update({"image_type": "photo", "orientation": "all"})
        payload = self.http.get_json(endpoint, params=params)
        hits = payload.get("hits", [])
        if not isinstance(hits, list):
            raise SourceError("Pixabay returned an unexpected result shape")
        candidates: list[AssetCandidate] = []
        for item in hits:
            if not isinstance(item, Mapping):
                continue
            if is_image:
                url = str(item.get("largeImageURL") or item.get("webformatURL") or "")
                title = f"Pixabay image {item.get('id', '')}".strip()
                metadata = {
                    "provider_id": item.get("id"),
                    "width": item.get("imageWidth"),
                    "height": item.get("imageHeight"),
                    "tags": item.get("tags"),
                }
            else:
                video_options = item.get("videos", {})
                if not isinstance(video_options, Mapping):
                    continue
                usable = [
                    value
                    for value in video_options.values()
                    if isinstance(value, Mapping) and value.get("url")
                ]
                if not usable:
                    continue
                selected = max(
                    usable,
                    key=lambda value: (_numeric_value(value.get("width")), _numeric_value(value.get("height"))),
                )
                url = str(selected.get("url") or "")
                title = f"Pixabay video {item.get('id', '')}".strip()
                metadata = {
                    "provider_id": item.get("id"),
                    "width": selected.get("width"),
                    "height": selected.get("height"),
                    "duration": item.get("duration"),
                    "tags": item.get("tags"),
                }
            if not url:
                continue
            author = str(item.get("user") or "")
            candidates.append(
                AssetCandidate(
                    source=self.name,
                    title=title,
                    page_url=f"https://pixabay.com/{'photos' if is_image else 'videos'}/id-{item.get('id', '')}/",
                    download_url=url,
                    media_type="image" if is_image else "video",
                    extension=_extension_from_mime_or_url(None, url, "image" if is_image else "video"),
                    license_name="Pixabay Content License",
                    license_url="https://pixabay.com/service/license-summary/",
                    attribution=author or None,
                    author=author or None,
                    rights_status="provider_license",
                    metadata=metadata,
                )
            )
        if not candidates:
            raise SourceError("Pixabay returned no usable media")
        return _dedupe_candidates(candidates)[:limit]


def _clean_embedded_url(value: str) -> str:
    value = html_module.unescape(value)
    value = value.replace("\\u002F", "/").replace("\\/", "/").replace("\\u003F", "?").replace("\\u003D", "=")
    value = value.strip(" \t\r\n\"'<>(),;")
    return value


def _upgrade_pinimg_url(url: str) -> str:
    """Prefer Pinterest's original-size path when the URL is a CDN image."""

    parsed = urlparse(url)
    if parsed.hostname and parsed.hostname.lower().endswith("pinimg.com"):
        path = re.sub(r"/(?:75x|136x|236x|474x|564x|736x|1200x)/", "/originals/", parsed.path, count=1)
        return urlunparse(parsed._replace(path=path))
    return url


def _extract_pinterest_urls(page_html: str) -> list[str]:
    """Extract public CDN URLs without trying to bypass login/rate limits."""

    normalized = html_module.unescape(page_html)
    normalized = normalized.replace("\\u002F", "/").replace("\\/", "/")
    found: list[str] = []
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(page_html, "html.parser")
        for tag in soup.find_all(["meta", "img", "source", "video"]):
            for attribute in ("content", "src", "data-src", "data-lazy-src", "poster"):
                value = tag.get(attribute)
                if value:
                    found.append(str(value))
    except ImportError:
        # The regular-expression fallback keeps the core module usable when
        # optional HTML parsing dependencies are not installed.
        pass
    found.extend(re.findall(r"https?://[^\"'<>\\\s]+", normalized))
    result: list[str] = []
    for raw in found:
        url = _clean_embedded_url(raw)
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path_lower = parsed.path.lower()
        if not (
            host == "pinimg.com"
            or host.endswith(".pinimg.com")
            or host == "pinterest.com"
            or host.endswith(".pinterest.com")
        ):
            continue
        if not (".jpg" in path_lower or ".jpeg" in path_lower or ".png" in path_lower or ".webp" in path_lower or ".gif" in path_lower or ".mp4" in path_lower):
            continue
        result.append(url)
    return list(dict.fromkeys(result))


def parse_pinterest_page(page_html: str, page_url: str, media_type: str, limit: int) -> list[AssetCandidate]:
    candidates: list[AssetCandidate] = []
    for raw_url in _extract_pinterest_urls(page_html):
        direct_url = _upgrade_pinimg_url(raw_url)
        detected_type = _media_type_from_mime_or_url(None, direct_url)
        if detected_type is None or (media_type != "any" and detected_type != media_type):
            continue
        candidates.append(
            AssetCandidate(
                source="pinterest",
                title="Pinterest pin media",
                page_url=page_url,
                download_url=direct_url,
                media_type=detected_type,
                extension=_extension_from_mime_or_url(None, direct_url, detected_type),
                license_name="Unknown — review required",
                license_url=None,
                attribution=None,
                rights_status="review_required",
                alternate_urls=(raw_url,) if raw_url != direct_url else (),
                metadata={"rights_note": "Pinterest does not establish reuse rights for a pin."},
            )
        )
        if len(candidates) >= limit:
            break
    return _dedupe_candidates(candidates)[:limit]


class PinterestSource(AssetSource):
    """Best-effort public Pinterest page adapter.

    Pinterest pages are dynamic and can be login/rate-limit gated.  This
    adapter never attempts to evade those controls.  It first parses public
    OpenGraph/embedded CDN URLs and leaves a yt-dlp extractor candidate as a
    fallback for a supplied pin URL.  A cookie file may be explicitly supplied
    through ``PINTEREST_COOKIES_FILE``; it is never read implicitly.
    """

    name = "pinterest"

    @staticmethod
    def _validate_pin_url(url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not (
            host == "pinterest.com" or host.endswith(".pinterest.com") or host == "pin.it"
        ):
            raise SourceError("Pinterest source requires a pinterest.com or pin.it URL")

    def search(
        self,
        keyword: str,
        media_type: str,
        limit: int,
        *,
        source_url: str | None = None,
    ) -> list[AssetCandidate]:
        page_url = source_url or PINTEREST_SEARCH_URL.format(query=quote(keyword))
        self._validate_pin_url(page_url)
        try:
            page_html = self.http.get_text(page_url, headers={"Accept": "text/html,application/xhtml+xml"})
            candidates = parse_pinterest_page(page_html, page_url, media_type, limit)
        except UrlPolicyError:
            raise
        except SourceError:
            if not source_url:
                raise
            candidates = []
        if candidates:
            if source_url:
                candidates.append(
                    AssetCandidate(
                        source=self.name,
                        title="Pinterest pin via yt-dlp",
                        page_url=source_url,
                        extractor_url=source_url,
                        media_type=media_type,
                        rights_status="review_required",
                        metadata={"rights_note": "Pinterest does not establish reuse rights for a pin."},
                    )
                )
            return _dedupe_candidates(candidates)[: limit + 1]
        if source_url:
            return [
                AssetCandidate(
                    source=self.name,
                    title="Pinterest pin via yt-dlp",
                    page_url=source_url,
                    extractor_url=source_url,
                    media_type=media_type,
                    rights_status="review_required",
                    metadata={"rights_note": "Pinterest does not establish reuse rights for a pin."},
                )
            ]
        raise SourceError(
            "Pinterest search returned no public media URLs; use an explicit pin URL or an approved Pinterest API/auth flow"
        )


class LocalFallbackSource(AssetSource):
    """Search configured local directories for a validated filename match."""

    name = "local"

    def search(
        self,
        keyword: str,
        media_type: str,
        limit: int,
        *,
        source_url: str | None = None,
    ) -> list[AssetCandidate]:
        query_tokens = [token.lower() for token in re.findall(r"[\w-]+", keyword) if token]
        matches: list[tuple[int, Path, str]] = []
        seen: set[Path] = set()
        for root in self.config.local_fallback_dirs:
            if not root.exists():
                continue
            if root.is_file():
                paths: Iterable[Path] = (root,)
            else:
                paths = root.rglob("*")
            for path in paths:
                try:
                    resolved = path.resolve()
                except OSError:
                    continue
                if resolved in seen or not resolved.is_file() or resolved.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                seen.add(resolved)
                try:
                    detected_type = validate_media_file(resolved, media_type, self.config.max_download_bytes)
                except MediaValidationError:
                    continue
                stem = resolved.stem.lower()
                score = sum(1 for token in query_tokens if token in stem)
                if query_tokens and score == 0:
                    continue
                matches.append((score, resolved, detected_type))
        matches.sort(key=lambda item: (-item[0], str(item[1]).lower()))
        candidates = [
            AssetCandidate(
                source=self.name,
                title=path.stem,
                page_url=None,
                media_type=detected_type,
                extension=path.suffix.lower(),
                rights_status="local_review_required",
                metadata={"local_path": str(path), "match_score": score},
            )
            for score, path, detected_type in matches[:limit]
        ]
        if not candidates:
            raise SourceError("No validated local fallback asset matched the keyword")
        return candidates


def _dedupe_candidates(candidates: Iterable[AssetCandidate]) -> list[AssetCandidate]:
    seen: set[str] = set()
    result: list[AssetCandidate] = []
    for candidate in candidates:
        key = candidate.metadata.get("provider_id") or candidate.download_url or candidate.extractor_url or candidate.metadata.get("local_path")
        key = f"{candidate.source}:{key or candidate.title}"
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


class UnsplashSource(AssetSource):
    """Unsplash photo search adapter (photos only)."""

    name = "unsplash"

    def search(
        self,
        keyword: str,
        media_type: str,
        limit: int,
        *,
        source_url: str | None = None,
    ) -> list[AssetCandidate]:
        if media_type == "video":
            raise SourceError("Unsplash provides photos only")
        if not self.config.unsplash_api_key:
            raise SourceError("UNSPLASH_API_KEY is not configured")
        payload = self.http.get_json(
            UNSPLASH_SEARCH_URL,
            params={"query": keyword, "per_page": min(max(limit, 1), 30)},
            headers={"Authorization": f"Client-ID {self.config.unsplash_api_key}"},
        )
        results = payload.get("results")
        if not isinstance(results, list):
            raise SourceError("Unsplash returned an unexpected result shape")
        candidates: list[AssetCandidate] = []
        for item in results:
            if not isinstance(item, Mapping):
                continue
            urls = item.get("urls")
            photo_url = str(urls.get("full") or "") if isinstance(urls, Mapping) else ""
            if not photo_url:
                continue
            user = item.get("user") or {}
            author = str(user.get("name") or "") if isinstance(user, Mapping) else ""
            sep = "&" if "?" in photo_url else "?"
            candidates.append(
                AssetCandidate(
                    source=self.name,
                    title=str(
                        item.get("alt_description")
                        or item.get("description")
                        or f"Unsplash photo {item.get('id', '')}"
                    ),
                    page_url=str(item.get("links", {}).get("html") or "") or None,
                    download_url=f"{photo_url}{sep}auto=format&fit=max&w=1920",
                    media_type="image",
                    extension=".jpg",
                    license_name="Unsplash License",
                    license_url="https://unsplash.com/license",
                    attribution=author or None,
                    author=author or None,
                    rights_status="provider_license",
                    metadata={"provider_id": item.get("id")},
                )
            )
        if not candidates:
            raise SourceError("Unsplash returned no usable photos")
        return _dedupe_candidates(candidates)[:limit]


class AssetManager:
    """Coordinate provider search, safe staging, validation, and manifests."""

    _manifest_lock = threading.Lock()

    def __init__(
        self,
        config: AssetFetcherConfig | None = None,
        *,
        source_overrides: Mapping[str, AssetSource] | None = None,
    ) -> None:
        self.config = config or AssetFetcherConfig.from_env()
        self.config.ensure_directories()
        self.http = HttpClient(self.config)
        self.sources: dict[str, AssetSource] = {
            "wikimedia": WikimediaCommonsSource(self.http, self.config),
            "pexels": PexelsSource(self.http, self.config),
            "pixabay": PixabaySource(self.http, self.config),
            "pinterest": PinterestSource(self.http, self.config),
            "local": LocalFallbackSource(self.http, self.config),
        }
        if self.config.unsplash_api_key:
            self.sources["unsplash"] = UnsplashSource(self.http, self.config)
        if source_overrides:
            self.sources.update(source_overrides)
        # Obscura (headless browser) bypasses anti-bot/Cloudflare for stock
        # CDN downloads; used first when present.
        self.obscura_bin = _obscura_bin()
        self.use_obscura = self.obscura_bin is not None

    def asset_directory(self, destination: str) -> Path:
        aliases = {"raw": "raw_footage", "raw_footage": "raw_footage", "b-roll": "b_roll", "b_roll": "b_roll", "image": "images", "images": "images", "audio": "audio", "music": "audio"}
        directory_name = aliases.get(destination.lower())
        if directory_name is None:
            raise ConfigurationError(
                f"Destination must be raw_footage, b_roll, images, or audio; received {destination!r}"
            )
        directory = self.config.asset_root / directory_name
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _source_order(self, source_order: Sequence[str] | None, reference_url: str | None) -> tuple[str, ...]:
        if source_order:
            order = tuple(item.strip().lower() for item in source_order if item and item.strip())
        elif reference_url:
            # An explicit URL should not unexpectedly trigger a broad keyword
            # search before the requested reference pull.
            order = ("yt-dlp", "local")
        else:
            order = self.config.default_source_order
        valid = set(self.sources) | {"yt-dlp"}
        unknown = [item for item in order if item not in valid]
        if unknown:
            raise ConfigurationError(f"Unknown asset source(s): {', '.join(unknown)}")
        return order

    def _candidate_for_reference(self, reference_url: str, media_type: str) -> AssetCandidate:
        parsed = urlparse(reference_url)
        if parsed.scheme in {"http", "https"}:
            # Apply the same initial URL policy before handing an explicit URL
            # to yt-dlp.  yt-dlp remains an external extractor and is limited
            # to the supplied, non-playlist URL below.
            self.http._validate_url(reference_url)
            host = (parsed.hostname or "").lower()
            source_name = "pinterest" if host == "pin.it" or host == "pinterest.com" or host.endswith(".pinterest.com") else "yt-dlp"
            return AssetCandidate(
                source=source_name,
                title="reference clip",
                page_url=reference_url,
                extractor_url=reference_url,
                media_type=media_type,
                rights_status="review_required",
                metadata={"rights_note": "Reference URL rights must be reviewed before editorial use."},
            )
        local_path = Path(reference_url).expanduser().resolve()
        if not local_path.is_file():
            raise ConfigurationError(f"Reference URL/path is not an HTTP URL or local file: {reference_url}")
        detected = validate_media_file(local_path, media_type, self.config.max_download_bytes)
        return AssetCandidate(
            source="local",
            title=local_path.stem,
            extension=local_path.suffix.lower(),
            media_type=detected,
            rights_status="local_review_required",
            metadata={"local_path": str(local_path), "rights_note": "Local source rights must be reviewed."},
        )

    def _stage_direct_download(self, candidate: AssetCandidate, stage_path: Path) -> tuple[Path, str | None, str]:
        errors: list[str] = []
        use_obscura = self.use_obscura and candidate.source in STOCK_PROVIDERS
        for index, url in enumerate(candidate.all_download_urls):
            if index:
                _jitter_sleep()  # scraper stability between download requests
            try:
                if use_obscura:
                    _download_with_obscura(url, stage_path, self.config.max_download_bytes)
                    content_type = None
                else:
                    content_type = self.http.stream_to_file(url, stage_path)
                detected = validate_media_file(stage_path, candidate.media_type, self.config.max_download_bytes)
                return stage_path, content_type, detected
            except (AssetFetcherError, OSError) as exc:
                errors.append(str(exc))
                stage_path.unlink(missing_ok=True)
        raise DownloadError(
            f"All direct download URLs failed for {candidate.source}/{candidate.title}: "
            + (errors[-1] if errors else "no URL")
        )

    def _stage_local_copy(self, candidate: AssetCandidate, stage_path: Path) -> tuple[Path, str | None, str]:
        source_value = candidate.metadata.get("local_path")
        if not source_value:
            raise DownloadError("Local candidate did not include a local_path")
        source_path = Path(str(source_value)).expanduser().resolve()
        detected = validate_media_file(source_path, candidate.media_type, self.config.max_download_bytes)
        with source_path.open("rb") as source, stage_path.open("wb") as destination:
            total = 0
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                total += len(chunk)
                if total > self.config.max_download_bytes:
                    raise DownloadError(f"Local file exceeds configured size limit: {source_path}")
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        validate_media_file(stage_path, detected, self.config.max_download_bytes)
        return stage_path, None, detected

    def _stage_with_ytdlp(self, candidate: AssetCandidate, stage_directory: Path) -> tuple[Path, str | None, str]:
        try:
            import yt_dlp  # type: ignore
        except ImportError as exc:
            raise DownloadError(
                "yt-dlp is required for Pinterest/reference URL fallback; install requirements.lock"
            ) from exc
        if not candidate.extractor_url:
            raise DownloadError("yt-dlp candidate has no extractor URL")
        output_template = str(stage_directory / "asset.%(ext)s")
        options: dict[str, Any] = {
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": self.config.request_retries,
            "socket_timeout": self.config.request_timeout[0],
            "max_filesize": self.config.max_download_bytes,
            "overwrites": False,
            "continuedl": True,
            "restrictfilenames": True,
        }
        if candidate.media_type == "image":
            options["format"] = "best"
        else:
            options["format"] = "bv*+ba/best"
            options["merge_output_format"] = "mp4"
        if self.config.pinterest_cookies_file:
            if candidate.source != "pinterest":
                raise DownloadError("Pinterest cookies may only be used for Pinterest candidates")
            if not self.config.pinterest_cookies_file.is_file():
                raise DownloadError(f"Configured Pinterest cookie file does not exist: {self.config.pinterest_cookies_file}")
            options["cookiefile"] = str(self.config.pinterest_cookies_file)
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                downloader.download([candidate.extractor_url])
        except Exception as exc:
            raise DownloadError(
                f"yt-dlp could not download the reference: {type(exc).__name__}"
            ) from exc

        staged_files = [path for path in stage_directory.iterdir() if path.is_file() and not path.name.endswith(".part")]
        valid_files: list[tuple[Path, str]] = []
        for path in staged_files:
            try:
                detected = validate_media_file(path, candidate.media_type, self.config.max_download_bytes)
            except MediaValidationError:
                continue
            valid_files.append((path, detected))
        if not valid_files:
            raise DownloadError("yt-dlp produced no valid image/video container")
        selected, detected = max(valid_files, key=lambda item: item[0].stat().st_size)
        return selected, None, detected

    def _identity_for_candidate(self, candidate: AssetCandidate) -> str:
        identity = (
            candidate.metadata.get("provider_id")
            or candidate.page_url
            or candidate.download_url
            or candidate.extractor_url
            or candidate.metadata.get("local_path")
            or candidate.title
        )
        return f"{candidate.source}:{identity}"

    def _destination_path(self, directory: Path, candidate: AssetCandidate, detected_type: str, extension: str) -> Path:
        digest = hashlib.sha256(self._identity_for_candidate(candidate).encode("utf-8")).hexdigest()[:12]
        base = f"{_safe_title(candidate.title)}-{digest}"
        extension = extension if extension in SUPPORTED_EXTENSIONS else (".jpg" if detected_type == "image" else ".mp4")
        first = directory / f"{base}{extension}"
        if not first.exists():
            return first
        try:
            if validate_media_file(first, detected_type, self.config.max_download_bytes) == detected_type:
                return first
        except MediaValidationError:
            pass
        for number in range(1, 1000):
            candidate_path = directory / f"{base}-{number}{extension}"
            if not candidate_path.exists():
                return candidate_path
        raise DownloadError(f"Could not allocate a non-overwriting destination for {base}")

    def _manifest_relative_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.config.workspace_root).as_posix()
        except ValueError:
            return path.resolve().as_posix()

    def _manifest_record(
        self,
        candidate: AssetCandidate,
        path: Path,
        detected_type: str,
        status: str,
    ) -> dict[str, Any]:
        metadata = {
            str(key): value
            for key, value in candidate.metadata.items()
            if key != "local_path" and isinstance(value, (str, int, float, bool, list, dict, type(None)))
        }
        record: dict[str, Any] = {
            "schema_version": 1,
            "retrieved_at": utc_now(),
            "status": status,
            "source": candidate.source,
            "title": candidate.title,
            "page_url": _redact_url(candidate.page_url),
            "download_url": _redact_url(candidate.download_url),
            "relative_path": self._manifest_relative_path(path),
            "media_type": detected_type,
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            "license_name": candidate.license_name,
            "license_url": _redact_url(candidate.license_url),
            "author": candidate.author,
            "attribution": candidate.attribution,
            "rights_status": candidate.rights_status,
            "metadata": metadata,
        }
        if candidate.metadata.get("local_path"):
            record["local_source_path"] = str(candidate.metadata["local_path"])
        if candidate.extractor_url:
            record["extractor_url"] = _redact_url(candidate.extractor_url)
        return record

    def _append_manifest(self, record: dict[str, Any]) -> None:
        manifest_path = self.config.manifest_path
        if manifest_path is None:
            return
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with self._manifest_lock:
            existing: list[Any] = []
            if manifest_path.exists():
                try:
                    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise DownloadError(f"Asset manifest is unreadable and was not changed: {manifest_path}") from exc
                if not isinstance(loaded, list):
                    raise DownloadError(f"Asset manifest must contain a JSON list: {manifest_path}")
                existing = loaded
            existing.append(record)
            temporary = manifest_path.with_name(f".{manifest_path.name}.{uuid.uuid4().hex}.part")
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                    json.dump(existing, handle, indent=2, ensure_ascii=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, manifest_path)
            finally:
                temporary.unlink(missing_ok=True)

    def download_candidate(self, candidate: AssetCandidate, destination: str = "b_roll") -> DownloadResult:
        """Download/copy one candidate and return its absolute path plus metadata."""

        target_directory = self.asset_directory(destination)
        self.config.temp_downloads.mkdir(parents=True, exist_ok=True)
        stage_directory = Path(tempfile.mkdtemp(prefix="editify-", dir=str(self.config.temp_downloads)))
        stage_path = stage_directory / "download.part"
        target_path: Path | None = None
        new_target = False
        try:
            if candidate.metadata.get("local_path"):
                _, _, detected_type = self._stage_local_copy(candidate, stage_path)
            elif candidate.all_download_urls:
                _, _, detected_type = self._stage_direct_download(candidate, stage_path)
            elif candidate.extractor_url:
                _, _, detected_type = self._stage_with_ytdlp(candidate, stage_directory)
            else:
                raise DownloadError(f"Candidate has no usable media source: {candidate.title}")
            staged_candidates = [
                path for path in stage_directory.iterdir() if path.is_file() and path.stat().st_size > 0
            ]
            valid_staged = []
            for path in staged_candidates:
                try:
                    actual = validate_media_file(path, candidate.media_type, self.config.max_download_bytes)
                except MediaValidationError:
                    continue
                valid_staged.append((path, actual))
            if not valid_staged:
                raise MediaValidationError(f"No valid staged media for {candidate.title}")
            staged_path, detected_type = max(valid_staged, key=lambda item: item[0].stat().st_size)
            if detected_type == "image":
                staged_path = _convert_image_to_jpg(staged_path)
            extension = candidate.extension or staged_path.suffix.lower()
            if extension not in SUPPORTED_EXTENSIONS:
                extension = ".jpg" if detected_type == "image" else ".mp4"
            target_path = self._destination_path(target_directory, candidate, detected_type, extension)
            if target_path.exists():
                # A stable provider identity makes repeated requests idempotent.
                record = self._manifest_record(candidate, target_path, detected_type, "reused")
                self._append_manifest(record)
                return DownloadResult(path=target_path.resolve(), record=record)
            os.replace(staged_path, target_path)
            new_target = True
            record = self._manifest_record(candidate, target_path, detected_type, "downloaded")
            try:
                self._append_manifest(record)
            except Exception:
                if new_target:
                    target_path.unlink(missing_ok=True)
                raise
            return DownloadResult(path=target_path.resolve(), record=record)
        finally:
            shutil.rmtree(stage_directory, ignore_errors=True)

    def fetch_asset(
        self,
        keyword: str,
        *,
        media_type: str = "video",
        destination: str = "b_roll",
        source_order: Sequence[str] | None = None,
        reference_url: str | None = None,
        candidate_limit: int | None = None,
    ) -> DownloadResult:
        """Search sources in order and return the first validated local asset."""

        if media_type not in {"image", "video", "audio", "any"}:
            raise ConfigurationError("media_type must be image, video, audio, or any")
        if not keyword.strip() and not reference_url:
            raise ConfigurationError("A keyword or reference_url is required")
        self.asset_directory(destination)
        order = self._source_order(source_order, reference_url)
        limit = max(1, min(candidate_limit or self.config.max_results_per_source, 50))
        failures: list[str] = []
        for source_name in order:
            try:
                if source_name == "yt-dlp":
                    if not reference_url:
                        raise SourceError("yt-dlp fallback requires --reference-url")
                    candidates = [self._candidate_for_reference(reference_url, media_type)]
                else:
                    source = self.sources[source_name]
                    candidates = source.search(keyword, media_type, limit, source_url=reference_url if source_name == "pinterest" else None)
                for candidate in candidates:
                    try:
                        if media_type != "any" and candidate.media_type not in {media_type, "any"}:
                            continue
                        return self.download_candidate(candidate, destination)
                    except (AssetFetcherError, OSError) as exc:
                        failures.append(f"{source_name}: {exc}")
            except (AssetFetcherError, OSError) as exc:
                failures.append(f"{source_name}: {exc}")
        details = "; ".join(failures[-12:]) or "no source returned a candidate"
        raise NoAssetFound(f"No valid asset found for {keyword or reference_url!r}. {details}")

    def fetch_reference_clip(
        self,
        reference_url: str,
        *,
        destination: str = "raw_footage",
        media_type: str = "video",
    ) -> Path:
        """Pull a reference clip with yt-dlp, or copy a validated local file."""

        return self.fetch_asset(
            Path(reference_url).stem if not urlparse(reference_url).scheme else "reference clip",
            media_type=media_type,
            destination=destination,
            source_order=("yt-dlp", "local"),
            reference_url=reference_url,
        ).path


def get_asset(
    keyword: str,
    *,
    media_type: str = "video",
    destination: str = "b_roll",
    source_order: Sequence[str] | None = None,
    reference_url: str | None = None,
    workspace_root: str | os.PathLike[str] | None = None,
    config_path: str | os.PathLike[str] | None = None,
) -> str:
    """Library entry point returning an absolute local path as a string."""

    manager = AssetManager(AssetFetcherConfig.from_env(workspace_root, config_path))
    result = manager.fetch_asset(
        keyword,
        media_type=media_type,
        destination=destination,
        source_order=source_order,
        reference_url=reference_url,
    )
    return str(result.path.resolve())


def get_b_roll(
    keyword: str,
    *,
    source_order: Sequence[str] | None = None,
    workspace_root: str | os.PathLike[str] | None = None,
    config_path: str | os.PathLike[str] | None = None,
) -> str:
    """Fetch a video B-roll asset and return its absolute path."""

    return get_asset(
        keyword,
        media_type="video",
        destination="b_roll",
        source_order=source_order,
        workspace_root=workspace_root,
        config_path=config_path,
    )


def get_image(
    keyword: str,
    *,
    source_order: Sequence[str] | None = None,
    workspace_root: str | os.PathLike[str] | None = None,
    config_path: str | os.PathLike[str] | None = None,
) -> str:
    """Fetch an image asset and return its absolute path."""

    return get_asset(
        keyword,
        media_type="image",
        destination="images",
        source_order=source_order,
        workspace_root=workspace_root,
        config_path=config_path,
    )


def get_raw_footage(
    keyword: str,
    *,
    source_order: Sequence[str] | None = None,
    workspace_root: str | os.PathLike[str] | None = None,
    config_path: str | os.PathLike[str] | None = None,
) -> str:
    """Fetch a video source clip and return its absolute path."""

    return get_asset(
        keyword,
        media_type="video",
        destination="raw_footage",
        source_order=source_order,
        workspace_root=workspace_root,
        config_path=config_path,
    )


def pull_reference_clip(
    reference_url: str,
    *,
    destination: str = "raw_footage",
    media_type: str = "video",
    workspace_root: str | os.PathLike[str] | None = None,
    config_path: str | os.PathLike[str] | None = None,
) -> str:
    """Use yt-dlp or a validated local file and return an absolute path."""

    manager = AssetManager(AssetFetcherConfig.from_env(workspace_root, config_path))
    return str(
        manager.fetch_reference_clip(
            reference_url,
            destination=destination,
            media_type=media_type,
        ).resolve()
    )


def _split_sources(values: Sequence[str] | None) -> tuple[str, ...] | None:
    if not values:
        return None
    result: list[str] = []
    for value in values:
        result.extend(item.strip().lower() for item in value.split(",") if item.strip())
    return tuple(result) or None


def _add_context_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workspace",
        help="Workspace root; defaults to EDITIFY_WORKSPACE or this script's directory",
        default=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        help="Optional JSON configuration file (API keys should normally be environment variables)",
        default=argparse.SUPPRESS,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search, validate, and safely download Editify image/video assets."
    )
    _add_context_arguments(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create the assets/raw_footage, b_roll, images, and temp directories")
    _add_context_arguments(init_parser)

    fetch_parser = subparsers.add_parser("fetch", help="Find an asset by keyword and return its absolute local path")
    _add_context_arguments(fetch_parser)
    fetch_parser.add_argument("keyword", help="Search phrase, for example: tech startup")
    fetch_parser.add_argument("--type", choices=("image", "video", "any"), default="video", dest="media_type")
    fetch_parser.add_argument(
        "--destination",
        choices=("raw_footage", "b_roll", "images"),
        default="b_roll",
        help="Logical asset folder",
    )
    fetch_parser.add_argument(
        "--source",
        action="append",
        help="Source order; repeat or comma-separate: pexels,pixabay,wikimedia,pinterest,local",
    )
    fetch_parser.add_argument(
        "--reference-url",
        help="Optional Pinterest/YouTube/etc. URL or local file for the yt-dlp/local fallback",
    )
    fetch_parser.add_argument(
        "--candidate-limit",
        type=int,
        default=8,
        help="Maximum candidates considered per provider (default: 8)",
    )
    fetch_parser.add_argument("--json", action="store_true", help="Print the complete manifest record as JSON")

    reference_parser = subparsers.add_parser(
        "pull-reference",
        help="Use yt-dlp or a local file to pull a reference clip into raw_footage",
    )
    _add_context_arguments(reference_parser)
    reference_parser.add_argument("url", help="Reference URL or local media path")
    reference_parser.add_argument(
        "--destination",
        choices=("raw_footage", "b_roll", "images"),
        default="raw_footage",
    )
    reference_parser.add_argument("--type", choices=("image", "video"), default="video", dest="media_type")
    reference_parser.add_argument("--json", action="store_true", help="Print the complete manifest record as JSON")

    subparsers.add_parser("sources", help="List supported source adapters")
    return parser


def _config_from_args(args: argparse.Namespace) -> AssetFetcherConfig:
    return AssetFetcherConfig.from_env(
        workspace_root=getattr(args, "workspace", None),
        config_path=getattr(args, "config_path", None),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "sources":
            print("wikimedia\npexels\npixabay\npinterest\nlocal\nyt-dlp")
            return 0
        config = _config_from_args(args)
        manager = AssetManager(config)
        if args.command == "init":
            directories = {name: str((config.asset_root / name).resolve()) for name in ASSET_DIRECTORY_NAMES}
            print(json.dumps(directories, indent=2))
            return 0
        if args.command == "pull-reference":
            result = manager.fetch_asset(
                Path(args.url).stem if not urlparse(args.url).scheme else "reference clip",
                media_type=args.media_type,
                destination=args.destination,
                source_order=("yt-dlp", "local"),
                reference_url=args.url,
            )
        else:
            result = manager.fetch_asset(
                args.keyword,
                media_type=args.media_type,
                destination=args.destination,
                source_order=_split_sources(args.source),
                reference_url=args.reference_url,
                candidate_limit=args.candidate_limit,
            )
        if getattr(args, "json", False):
            print(json.dumps(result.record, indent=2, ensure_ascii=False))
        else:
            print(str(result.path.resolve()))
        return 0
    except AssetFetcherError as exc:
        print(f"asset-fetcher: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("asset-fetcher: interrupted", file=sys.stderr)
        return 130


__all__ = [
    "AssetCandidate",
    "AssetFetcherConfig",
    "AssetFetcherError",
    "AssetManager",
    "AssetSource",
    "ConfigurationError",
    "DownloadError",
    "DownloadResult",
    "HttpClient",
    "LocalFallbackSource",
    "MediaValidationError",
    "NoAssetFound",
    "PexelsSource",
    "PixabaySource",
    "PinterestSource",
    "UrlPolicyError",
    "WikimediaCommonsSource",
    "get_asset",
    "get_b_roll",
    "get_image",
    "get_raw_footage",
    "is_public_domain_license",
    "parse_pinterest_page",
    "pull_reference_clip",
    "sniff_media_type",
    "validate_media_file",
]


if __name__ == "__main__":  # pragma: no cover - exercised through CLI smoke tests
    raise SystemExit(main())
