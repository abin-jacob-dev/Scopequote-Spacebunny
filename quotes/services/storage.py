from __future__ import annotations

import uuid
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import PurePosixPath

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_LOGO_BYTES = 2 * 1024 * 1024
MAX_LOGO_PIXELS = 4_000_000
ALLOWED_IMAGE_FORMATS = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}
MAX_IMAGE_BYTES = MAX_LOGO_BYTES
MAX_IMAGE_PIXELS = MAX_LOGO_PIXELS
STAGING_ROOT = "quotes/logos/staging"
VERSION_ROOT = "quotes/versions"
ABANDONED_STAGING_AGE = timedelta(hours=24)
PROFILE_ROOT = "profiles"
ABANDONED_PROFILE_AGE = timedelta(hours=24)


@dataclass(frozen=True)
class StoredLogo:
    storage_key: str
    content_type: str
    byte_size: int
    owner_id: uuid.UUID


class MediaCleanupError(RuntimeError):
    pass


def _owner_uuid(owner_id) -> uuid.UUID:
    if hasattr(owner_id, "pk"):
        owner_id = owner_id.pk
    try:
        return owner_id if isinstance(owner_id, uuid.UUID) else uuid.UUID(str(owner_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("A valid logo owner is required.") from exc


def _upload_bytes(upload) -> bytes:
    if upload is None:
        raise ValidationError("Choose a logo image to continue.")
    try:
        declared_size = getattr(upload, "size", None)
        if isinstance(declared_size, int) and declared_size > MAX_LOGO_BYTES:
            raise ValidationError("Logo files must be 2 MB or smaller.")
        if hasattr(upload, "seek"):
            upload.seek(0)
        try:
            data = upload.read(MAX_LOGO_BYTES + 1)
        except TypeError:
            data = upload.read()
    except ValidationError:
        raise
    except (AttributeError, OSError, ValueError) as exc:
        raise ValidationError("The logo could not be read.") from exc
    if not isinstance(data, bytes):
        raise ValidationError("The logo must be a readable image file.")
    if not data:
        raise ValidationError("The logo file is empty.")
    if len(data) > MAX_LOGO_BYTES:
        raise ValidationError("Logo files must be 2 MB or smaller.")
    return data


def _decoded_image(data: bytes) -> Image.Image:
    if data[:256].lstrip().lower().startswith((b"<svg", b"<?xml")):
        raise ValidationError("SVG logo files are not supported.")
    image = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(BytesIO(data))
        image_format = (image.format or "").upper()
        if image_format not in ALLOWED_IMAGE_FORMATS:
            raise ValidationError("Logo files must be PNG, JPEG, or WebP images.")
        if getattr(image, "n_frames", 1) != 1:
            raise ValidationError("Animated logo images are not supported.")
        if image.width * image.height > MAX_LOGO_PIXELS:
            raise ValidationError(
                "Logo images must be no larger than 4 million pixels."
            )
        image.load()
        oriented = ImageOps.exif_transpose(image)
        if oriented.mode not in ("RGB", "RGBA"):
            oriented = oriented.convert("RGBA")
        clean = Image.new(oriented.mode, oriented.size)
        clean.paste(oriented)
        clean.info.clear()
        clean._scopequote_format = image_format
        return clean
    except ValidationError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValidationError("Choose a valid PNG, JPEG, or WebP logo image.") from exc
    finally:
        if image is not None:
            image.close()


def _encoded_image(image: Image.Image, image_format: str) -> bytes:
    output = BytesIO()
    image.info.clear()
    if image_format == "JPEG":
        image.convert("RGB").save(
            output,
            format="JPEG",
            quality=95,
            optimize=True,
            exif=b"",
        )
    elif image_format == "WEBP":
        image.save(
            output,
            format="WEBP",
            quality=95,
            method=6,
            exif=b"",
        )
    else:
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA")
        image.save(output, format="PNG", optimize=True, pnginfo=None)
    return output.getvalue()


def _validated_payload(data: bytes) -> tuple[str, bytes]:
    image = _decoded_image(data)
    image_format = getattr(image, "_scopequote_format", "PNG")
    encoded = _encoded_image(image, image_format)
    if len(encoded) > MAX_LOGO_BYTES:
        raise ValidationError("The re-encoded logo must be 2 MB or smaller.")
    return image_format, encoded


def sanitized_logo_upload(upload, *, owner_id):
    _owner_uuid(owner_id)
    data = _upload_bytes(upload)
    image_format, encoded = _validated_payload(data)
    content_type = ALLOWED_IMAGE_FORMATS.get(image_format, "image/png")
    return ContentFile(
        encoded, name=f"{uuid.uuid4()}.{_content_extension(content_type)}"
    )


prepare_logo_upload = sanitized_logo_upload
sanitize_logo_upload = sanitized_logo_upload


def _content_extension(content_type: str) -> str:
    return {"image/jpeg": "jpg", "image/webp": "webp"}.get(content_type, "png")


def stage_logo(upload, *, owner_id) -> StoredLogo:
    owner = _owner_uuid(owner_id)
    data = _upload_bytes(upload)
    image_format, encoded = _validated_payload(data)
    content_type = ALLOWED_IMAGE_FORMATS.get(image_format, "image/png")
    extension = _content_extension(content_type)
    storage_key = f"{STAGING_ROOT}/{owner}/{uuid.uuid4()}.{extension}"
    saved_key = default_storage.save(storage_key, ContentFile(encoded))
    return StoredLogo(
        storage_key=saved_key,
        content_type=content_type,
        byte_size=len(encoded),
        owner_id=owner,
    )


def _allowed_stored_prefixes(owner: uuid.UUID) -> tuple[str, ...]:
    owner_text = str(owner)
    return (
        f"{STAGING_ROOT}/{owner_text}/",
        f"{VERSION_ROOT}/{owner_text}/",
        f"quotes/logos/legacy/{owner_text}/",
        f"profiles/{owner_text}/",
    )


def _validate_stored_logo(stored_logo: StoredLogo) -> None:
    if not isinstance(stored_logo, StoredLogo):
        raise TypeError("stored_logo must be a StoredLogo")
    if not stored_logo.storage_key:
        raise ValueError("A staged logo storage key is required.")
    owner = _owner_uuid(stored_logo.owner_id)
    key = str(stored_logo.storage_key)
    if ".." in PurePosixPath(key).parts or not any(
        key.startswith(prefix) for prefix in _allowed_stored_prefixes(owner)
    ):
        raise ValueError("The stored logo is not owned by the requested account.")


def inspect_stored_logo(stored_logo: StoredLogo) -> StoredLogo:
    _validate_stored_logo(stored_logo)
    try:
        with default_storage.open(stored_logo.storage_key, "rb") as source:
            data = source.read(MAX_LOGO_BYTES + 1)
    except OSError:
        raise
    except Exception as exc:
        raise ValidationError("The stored logo could not be read.") from exc
    if len(data) > MAX_LOGO_BYTES:
        raise ValidationError("Logo files must be 2 MB or smaller.")
    image_format, encoded = _validated_payload(data)
    content_type = ALLOWED_IMAGE_FORMATS.get(image_format, "image/png")
    return StoredLogo(
        storage_key=stored_logo.storage_key,
        content_type=content_type,
        byte_size=len(encoded),
        owner_id=stored_logo.owner_id,
    )


def _version_uuid(version_id) -> uuid.UUID:
    try:
        return (
            version_id
            if isinstance(version_id, uuid.UUID)
            else uuid.UUID(str(version_id))
        )
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("A valid version identifier is required.") from exc


def commit_version_logo(
    stored_logo: StoredLogo,
    *,
    version_id,
    remove_source: bool = True,
) -> str:
    _validate_stored_logo(stored_logo)
    version = _version_uuid(version_id)
    source_key = stored_logo.storage_key
    destination = None
    try:
        with default_storage.open(source_key, "rb") as source:
            data = source.read(MAX_LOGO_BYTES + 1)
        if len(data) > MAX_LOGO_BYTES:
            raise ValidationError("Logo files must be 2 MB or smaller.")
        image_format, encoded = _validated_payload(data)
        content_type = ALLOWED_IMAGE_FORMATS.get(image_format, "image/png")
        destination = (
            f"{VERSION_ROOT}/{stored_logo.owner_id}/{version}."
            f"{_content_extension(content_type)}"
        )
        destination_key = default_storage.save(destination, ContentFile(encoded))
    except Exception:
        if destination is not None:
            delete_object(destination)
        raise
    if remove_source:
        delete_object(source_key)
    return destination_key


def _queue_cleanup(key: str, attempts: int) -> None:
    try:
        from accounts.models import MediaCleanupTask

        MediaCleanupTask.objects.get_or_create(
            storage_key=key[:1024],
            defaults={
                "attempts": attempts,
                "last_error": "storage delete failed",
            },
        )
    except Exception as exc:
        raise MediaCleanupError(
            "Media cleanup could not be persisted for retry."
        ) from exc


def delete_object(key) -> None:
    if not key:
        return
    key = str(key)[:1024]
    for attempt in range(2):
        try:
            default_storage.delete(key)
            return
        except Exception:
            if attempt == 1:
                _queue_cleanup(key, 2)
                return


def _walk_storage(path):
    try:
        directories, files = default_storage.listdir(path)
    except (OSError, NotImplementedError):
        return
    for filename in files:
        yield str(PurePosixPath(path) / filename) if path else filename
    for directory in directories:
        child = str(PurePosixPath(path) / directory) if path else directory
        yield from _walk_storage(child)


def profile_storage_keys(owner_id):
    owner = _owner_uuid(owner_id)
    yield from _walk_storage(f"{PROFILE_ROOT}/{owner}")


def abandoned_profile_logo_keys(
    *,
    now: datetime | None = None,
    limit: int = 1000,
):
    current = now or timezone.now()
    if timezone.is_naive(current):
        current = current.replace(tzinfo=UTC)
    cutoff = current - ABANDONED_PROFILE_AGE
    from accounts.models import Profile

    found = []
    for key in _walk_storage(PROFILE_ROOT):
        if Profile.objects.filter(logo=key).exists():
            continue
        try:
            modified = default_storage.get_modified_time(key)
        except (AttributeError, OSError, NotImplementedError):
            continue
        modified = (
            modified if timezone.is_aware(modified) else modified.replace(tzinfo=UTC)
        )
        if modified <= cutoff:
            found.append(key)
            if len(found) >= limit:
                break
    return found


def cleanup_abandoned_profile_logos(
    *,
    now: datetime | None = None,
    limit: int = 1000,
) -> int:
    keys = abandoned_profile_logo_keys(now=now, limit=limit)
    for key in keys:
        delete_object(key)
    return len(keys)


def abandoned_staged_logo_keys(*, now: datetime | None = None, limit: int = 1000):
    current = now or timezone.now()
    if timezone.is_naive(current):
        current = current.replace(tzinfo=UTC)
    cutoff = current - ABANDONED_STAGING_AGE
    from quotes.models import Quote

    found = []
    for key in _walk_storage(STAGING_ROOT):
        if Quote.objects.filter(logo_object_key=key).exists():
            continue
        try:
            modified = default_storage.get_modified_time(key)
        except (AttributeError, OSError, NotImplementedError):
            continue
        modified = (
            modified if timezone.is_aware(modified) else modified.replace(tzinfo=UTC)
        )
        if modified <= cutoff:
            found.append(key)
            if len(found) >= limit:
                break
    return found


def cleanup_abandoned_staged_logos(
    *,
    now: datetime | None = None,
    limit: int = 1000,
) -> int:
    keys = abandoned_staged_logo_keys(now=now, limit=limit)
    for key in keys:
        delete_object(key)
    return len(keys)


def retry_media_cleanup_tasks(*, now: datetime | None = None, limit: int = 1000) -> int:
    from accounts.models import MediaCleanupTask

    current = now or timezone.now()
    if timezone.is_naive(current):
        current = current.replace(tzinfo=UTC)
    tasks = MediaCleanupTask.objects.filter(next_attempt_at__lte=current).order_by(
        "next_attempt_at", "created_at"
    )[:limit]
    completed = 0
    for task in tasks:
        try:
            default_storage.delete(task.storage_key)
        except Exception:
            task.attempts += 1
            task.last_error = "storage delete failed"
            task.next_attempt_at = current + timedelta(minutes=5)
            task.save(
                update_fields=(
                    "attempts",
                    "last_error",
                    "next_attempt_at",
                    "updated_at",
                )
            )
        else:
            task.delete()
            completed += 1
    return completed
