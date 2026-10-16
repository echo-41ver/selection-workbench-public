from selection_automation.collection_environment import profile_browser_settings


def test_collection_profile_matches_successful_firefox_153_trial():
    assert profile_browser_settings() == {
        'country': 'us',
        'tabs': ['about:blank'],
        'fingerprint_config': {
            'browser_kernel_config': {'type': 'firefox', 'version': '153'},
            'ua': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:153.0) '
                   'Gecko/20100101 Firefox/153.0'),
            'automatic_timezone': '1',
            'language_switch': '1',
            'page_language_switch': '1',
            'screen_resolution': '1920_1080',
            'webrtc': 'disabled',
        },
    }


def test_collection_profiles_do_not_share_mutable_browser_settings():
    first = profile_browser_settings()
    first['tabs'].append('https://example.com/')
    first['fingerprint_config']['browser_kernel_config']['version'] = '144'
    second = profile_browser_settings()
    assert second['tabs'] == ['about:blank']
    assert second['fingerprint_config']['browser_kernel_config']['version'] == '153'
