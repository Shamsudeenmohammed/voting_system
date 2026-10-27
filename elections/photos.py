"""Server-side validation for candidate passport photographs.

The ballot relies on this feature for a professional, verifiable ballot: every
photograph uploaded by election staff is checked here before it is stored or
linked to a candidate. The checks are deliberately conservative:

* only JPEG, PNG and WebP images are accepted (detected from the file
  content itself, never from the client-supplied filename);
* the upload must be under a configurable size limit;
* the upload must actually decode as an image (a real photograph, not a
  renamed executable or document).

On success the upload's filename extension is rewritten to match the format
Pillow detected so that the stored file cannot masquerade as a different
type (e.g. ``portrait.png`` containing JPEG data becomes ``portrait.jpg``).
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from PIL import Image, UnidentifiedImageError

# Format Pillow reports -> canonical extension used when storing the file.
_FORMAT_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}

# Human-readable names of the accepted formats (used in error messages).
_ACCEPTED_FORMATS = "JPEG, PNG or WebP"

# Fallback size limit if settings.CANDIDATE_PHOTO_MAX_UPLOAD_SIZE is missing
# (e.g. a test or custom settings module that does not define it).
DEFAULT_MAX_UPLOAD_SIZE = 5 * 1024 * 1024


def max_upload_size() -> int:
    """Maximum candidate photograph upload size in bytes."""
    return int(
        getattr(
            settings,
            "CANDIDATE_PHOTO_MAX_UPLOAD_SIZE",
            DEFAULT_MAX_UPLOAD_SIZE,
        )
    )


def validate_candidate_photo(uploaded_file: UploadedFile) -> UploadedFile:
    """Validate a candidate passport photograph upload.

    Returns the (possibly renamed) upload on success; raises
    ``ValidationError`` with a clear, user-facing message on failure. This is
    called from ``Candidate.clean()`` / ``CandidateForm.clean_photo()`` so the
    same rules apply wherever photographs are uploaded.
    """
    if not isinstance(uploaded_file, UploadedFile):
        # Not a fresh upload (e.g. the path of an already-stored photo);
        # nothing to validate here.
        return uploaded_file

    size = getattr(uploaded_file, "size", 0) or 0
    limit = max_upload_size()
    if size > limit:
        raise ValidationError(
            "The passport photograph is too large (%(size).1f MB). "
            "Maximum allowed size is %(limit).1f MB. "
            "Please resize or compress the image and try again.",
            code="candidate_photo_too_large",
            params={
                "size": size / (1024 * 1024),
                "limit": limit / (1024 * 1024),
            },
        )

    # Decode the file header to determine the real image format. This is the
    # authoritative check: the client-supplied content type and filename are
    # never trusted.
    try:
        with Image.open(uploaded_file) as image:
            image_format = (image.format or "").upper()
    except UnidentifiedImageError:
        raise ValidationError(
            "The uploaded file is not a readable image. "
            f"Please upload a passport photograph in {_ACCEPTED_FORMATS} format.",
            code="candidate_photo_unreadable",
        ) from None
    except OSError:
        raise ValidationError(
            "The passport photograph appears to be corrupted or truncated "
            f"and could not be read. Please re-export it as {_ACCEPTED_FORMATS} "
            "and upload it again.",
            code="candidate_photo_corrupted",
        ) from None

    extension = _FORMAT_EXTENSIONS.get(image_format)
    if extension is None:
        raise ValidationError(
            "Unsupported image format (%(format)s). Please upload a passport "
            f"photograph in {_ACCEPTED_FORMATS} format.",
            code="candidate_photo_unsupported_format",
            params={"format": image_format or "unknown"},
        )

    # Normalise the stored filename so its extension matches the real format.
    stem = Path(uploaded_file.name or "photo").stem or "photo"
    uploaded_file.name = stem + extension

    # Rewind so later readers (storage, form previews) start from the top.
    try:
        uploaded_file.seek(0)
    except (AttributeError, OSError):
        pass

    return uploaded_file
