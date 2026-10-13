"""Browser settings shared by initial and replacement collection profiles."""

from .adspower_kernel import REQUIRED_KERNEL


def profile_browser_settings():
    """Return independent profile fields matching the successful Firefox trial."""
    version = REQUIRED_KERNEL['version']
    return {
        'country': 'us',
        'tabs': ['about:blank'],
        'fingerprint_config': {
            'browser_kernel_config': dict(REQUIRED_KERNEL),
            'ua': (f'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{version}.0) '
                   f'Gecko/20100101 Firefox/{version}.0'),
            'automatic_timezone': '1',
            'language_switch': '1',
            'page_language_switch': '1',
            'screen_resolution': '1920_1080',
            'webrtc': 'disabled',
        },
    }
