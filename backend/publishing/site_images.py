"""Bounded, re-encoded image assets for isolated local site previews."""
import hashlib
import hmac
import io
import re
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from .models import HERO_FIT_CHOICES, HERO_POSITION_CHOICES, JournalSiteDraft, OrganisationPortalDraft, SiteImageAsset

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_STORED_BYTES = 8 * 1024 * 1024
MAX_DIMENSION = 4096
MAX_PIXELS = 12_000_000
MAX_SITE_IMAGES = 50
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


class InvalidImage(ValueError):
    pass


def normalise_image(upload):
    """Decode a bounded single-frame raster and re-encode without metadata."""
    if not upload or upload.size < 1 or upload.size > MAX_UPLOAD_BYTES:
        raise InvalidImage("size")
    data = upload.read(MAX_UPLOAD_BYTES + 1)
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise InvalidImage("size")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in ALLOWED_FORMATS or getattr(source, "n_frames", 1) != 1:
                    raise InvalidImage("format")
                width, height = source.size
                if (width < 1 or height < 1 or width > MAX_DIMENSION or height > MAX_DIMENSION or
                        width * height > MAX_PIXELS):
                    raise InvalidImage("dimensions")
                source.load()
                image = ImageOps.exif_transpose(source)
                has_alpha = "A" in image.getbands() or "transparency" in image.info
                mode = "RGBA" if has_alpha else "RGB"
                converted = image.convert(mode)
                clean = Image.new(mode, converted.size)
                clean.paste(converted)
                output = io.BytesIO()
                if has_alpha:
                    clean.save(output, format="PNG")
                    media_type = "image/png"
                else:
                    clean.save(output, format="JPEG", quality=88, optimize=True)
                    media_type = "image/jpeg"
                result = output.getvalue()
                width, height = clean.size
    except (UnidentifiedImageError, OSError, ValueError, EOFError, SyntaxError, OverflowError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise InvalidImage("invalid") from exc
    if not result or len(result) > MAX_STORED_BYTES:
        raise InvalidImage("encoded_size")
    return {"image_data":result, "media_type":media_type, "width":width, "height":height,
            "sha256":hashlib.sha256(result).hexdigest()}


def asset_is_intact(asset):
    data = bytes(asset.image_data)
    signature = ((asset.media_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n")) or
                 (asset.media_type == "image/jpeg" and data.startswith(b"\xff\xd8\xff")))
    return bool(signature and 0 < len(data) <= MAX_STORED_BYTES and
                1 <= asset.width <= MAX_DIMENSION and 1 <= asset.height <= MAX_DIMENSION and
                asset.width * asset.height <= MAX_PIXELS and re.fullmatch(r"[0-9a-f]{64}", asset.sha256 or "") and
                hmac.compare_digest(hashlib.sha256(data).hexdigest(), asset.sha256))


def valid_display(fit, position):
    return fit in dict(HERO_FIT_CHOICES) and position in dict(HERO_POSITION_CHOICES)


def hero_reference_valid(site, asset_id, alt, fit, position):
    """Enforce exact ownership and content integrity for draft or release."""
    if not valid_display(fit, position):
        return False
    if asset_id is None:
        return alt == ""
    if not isinstance(alt, str) or not alt.strip() or len(alt) > 200:
        return False
    if isinstance(site, JournalSiteDraft):
        owner_field = "journal_site"
    elif isinstance(site, OrganisationPortalDraft):
        owner_field = "portal_site"
    else:
        return False
    asset = SiteImageAsset.objects.filter(pk=asset_id, **{owner_field:site}).first()
    return bool(asset and asset_is_intact(asset))
