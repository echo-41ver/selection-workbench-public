"""Local input admission before any paid vision request."""

API_IMAGE_FORMATS = frozenset({'JPEG', 'PNG', 'WEBP'})


class ImageInputError(ValueError):
    """Invalid or unsupported local input; no provider call is needed."""


def validate_api_image(image):
    # An explicit project transport allowlist; never silently convert another
    # format or send corrupt bytes hoping the provider can decode them.
    if image.format not in API_IMAGE_FORMATS:
        raise ImageInputError('IMAGE_FORMAT_UNSUPPORTED')
    image.load()
