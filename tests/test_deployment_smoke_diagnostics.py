"""Mock-only diagnostics regression; no browser, model, profile or paid API calls."""
from __future__ import annotations

import importlib.util
import io
import itertools
import json
from pathlib import Path
import re
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from selection_automation.adspower_diagnostics import provider_error
from selection_automation.collection_environment import profile_browser_settings

spec = importlib.util.spec_from_file_location('deployment_smoke_diagnostics_test', ROOT / 'scripts/deployment_smoke.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class DeploymentSmokeDiagnosticsTests(unittest.TestCase):
    def run_mock_smoke(self, error, *, failed_check='check_flower'):
        with tempfile.TemporaryDirectory(prefix='mock-smoke-diagnostics-only-') as directory:
            with patch.object(smoke, 'activate'), patch.object(smoke, 'check_model', return_value={'ready': True}), \
                    patch.object(smoke, 'check_excel', return_value={'ready': True}), \
                    patch.object(smoke, 'check_chromium', return_value={'ready': True}), \
                    patch.object(smoke, 'check_flower', return_value={'ready': True}), \
                    patch.object(smoke, failed_check, side_effect=error):
                report = smoke.run(Path(directory))
                saved = json.loads((Path(directory) / 'runtime/deployment-smoke.json').read_text('utf-8'))
            self.assertEqual(report, saved)
            return report

    def test_safe_diagnostic_survives_without_raw_provider_data(self):
        error = provider_error('/api/v1/user/create?api_key=SECRET_URL', -1,
                               'permission denied; key=SECRET_KEY; password=SECRET_PASSWORD; https://PRIVATE_HOST/?token=SECRET_QUERY')
        error.diagnostic.update(provider_message='SECRET_MESSAGE', request_url='https://PRIVATE_URL/?token=SECRET_TOKEN')
        report = self.run_mock_smoke(error)
        self.assertFalse(report['ready'])
        self.assertEqual(report['failed_stage'], 'flower_binding')
        self.assertEqual(report['error_type'], 'AdsPowerAPIError')
        self.assertEqual(report['adspower_diagnostic'], {'operation': 'PROFILE_CREATE', 'category': 'AUTHORIZATION',
                         'provider_code': -1, 'http_status': None, 'retryable': False})
        serialized = json.dumps(report)
        self.assertNotIn('SECRET', serialized)
        self.assertNotIn('PRIVATE', serialized)
        self.assertNotIn('https://', serialized)
        self.assertNotIn('provider_message', serialized)
        self.assertEqual(report['paid_analysis_requests'], 0)

    def test_v1_and_status_operations_are_identified_without_queries(self):
        for path, operation in (('/status', 'API_STATUS'), ('/api/v1/user/create?key=SECRET', 'PROFILE_CREATE'),
                                ('/api/v1/user/delete?user_id=PRIVATE', 'PROFILE_DELETE')):
            with self.subTest(path=path):
                error = provider_error(path, 'SECRET_PROVIDER_CODE', 'unrecognized SECRET_PROVIDER_MESSAGE')
                self.assertEqual(error.diagnostic['operation'], operation)
                self.assertIsNone(error.diagnostic['provider_code'])
                self.assertNotIn('SECRET', json.dumps(error.diagnostic) + str(error))
                self.assertNotIn('PRIVATE', json.dumps(error.diagnostic) + str(error))

    def test_flower_create_matches_business_profile_and_rejection_stays_failed(self):
        calls = []

        def reject_create(base, path, **kwargs):
            calls.append((base, path, kwargs))
            raise provider_error(path, -1, 'webrtc must be forward,proxy,local,disabled')

        def unexpected(*args, **kwargs):
            self.fail('No browser, launcher or port operations are expected after create rejection')

        modules = {
            'company_setup': SimpleNamespace(ensure_adspower_api=lambda company: None, free_block=unexpected),
            'replace_proxyshare_worker_environment': SimpleNamespace(api_call=reject_create, stop_profile_and_wait=unexpected),
            'selection_automation.secrets': SimpleNamespace(load=lambda path: {}),
            'selection_automation.background_launch': SimpleNamespace(run_background_launcher=unexpected),
            'psutil': SimpleNamespace(Error=RuntimeError, Process=unexpected),
        }
        with tempfile.TemporaryDirectory(prefix='mock-flower-payload-only-') as directory:
            with patch.dict(sys.modules, modules), patch.object(smoke, 'activate'), \
                    patch.object(smoke, 'check_model', return_value={'ready': True}), \
                    patch.object(smoke, 'check_excel', return_value={'ready': True}), \
                    patch.object(smoke, 'check_chromium', return_value={'ready': True}):
                report = smoke.run(Path(directory))
                saved = json.loads((Path(directory) / 'runtime/deployment-smoke.json').read_text('utf-8'))
        self.assertEqual(report, saved)
        self.assertEqual(len(calls), 1)
        base, path, kwargs = calls[0]
        self.assertEqual(base, 'http://127.0.0.1:50325')
        self.assertEqual(path, '/api/v1/user/create')
        self.assertEqual(kwargs['method'], 'POST')
        self.assertEqual(kwargs['timeout'], 30)
        body = kwargs['body']
        shared = profile_browser_settings()
        self.assertEqual({key: body[key] for key in shared}, shared)
        self.assertEqual(body['fingerprint_config']['webrtc'], 'disabled')
        self.assertEqual(body['fingerprint_config']['browser_kernel_config'], {'type': 'firefox', 'version': '153'})
        self.assertIn('Firefox/153.0', body['fingerprint_config']['ua'])
        self.assertTrue(body['name'].startswith('deployment-smoke-'))
        self.assertEqual(body['group_id'], '0')
        self.assertEqual(body['user_proxy_config'], {'proxy_soft': 'no_proxy'})
        self.assertFalse(report['ready'])
        self.assertEqual(report['failed_stage'], 'flower_binding')
        self.assertEqual(report['error_type'], 'AdsPowerAPIError')
        self.assertEqual(report['adspower_diagnostic']['operation'], 'PROFILE_CREATE')
        self.assertEqual(report['adspower_diagnostic']['provider_code'], -1)
        self.assertEqual(report['paid_analysis_requests'], 0)

    def test_webdriver_http_errors_keep_only_command_status_and_standard_error(self):
        for suffix, command in (('/url', 'NAVIGATE'), ('/execute/sync', 'DOM_READ')):
            for status, identifier in ((400, 'invalid argument'), (404, 'no such window'), (500, 'unknown error')):
                with self.subTest(command=command, status=status):
                    payload = {'value': {'error': identifier, 'message': 'password=SECRET_MESSAGE',
                               'stacktrace': 'SECRET_STACK', 'data': {'api_key': 'SECRET_DATA'}}}
                    error = HTTPError('http://PRIVATE_HOST/session/SECRET_SESSION' + suffix, status,
                                      'SECRET_REASON', {'Authorization': 'Bearer SECRET_HEADER'},
                                      io.BytesIO(json.dumps(payload).encode('utf-8')))
                    transport = SimpleNamespace(open=Mock(side_effect=error))
                    with self.assertRaises(smoke.WebDriverSmokeError) as raised:
                        smoke._webdriver_request(transport, 'http://PRIVATE_HOST/session/SECRET_SESSION', suffix,
                                                 {'url': 'SECRET_REQUEST_BODY'})
                    report = self.run_mock_smoke(raised.exception)
                    self.assertEqual(report['webdriver_diagnostic'], {'command': command, 'http_status': status,
                                                                     'error': identifier})
                    self.assertFalse(report['ready'])
                    self.assertEqual(report['failed_stage'], 'flower_binding')
                    self.assertEqual(report['error_type'], 'WebDriverSmokeError')
                    serialized = json.dumps(report) + str(raised.exception)
                    self.assertNotIn('SECRET', serialized)
                    self.assertNotIn('PRIVATE', serialized)
                    self.assertNotIn('message', serialized)
                    self.assertNotIn('stacktrace', serialized)
                    self.assertEqual(transport.open.call_count, 1)
                    self.assertEqual(transport.open.call_args.kwargs, {'timeout': 15})

    def test_webdriver_error_body_is_bounded_and_unknown_values_are_not_echoed(self):
        for payload in (b'not JSON SECRET_BODY',
                        b'{"value":{"error":"SECRET_ERROR","message":"SECRET_MESSAGE"}}',
                        b'{"value":{"message":"' + b'X' * 20000 + b'"}}'):
            with self.subTest(length=len(payload)):
                body = io.BytesIO(payload)
                body.read = Mock(wraps=body.read)
                error = HTTPError('http://PRIVATE_HOST/session/SECRET_SESSION/url', 500, 'SECRET_REASON', {}, body)
                transport = SimpleNamespace(open=Mock(side_effect=error))
                with self.assertRaises(smoke.WebDriverSmokeError) as raised:
                    smoke._webdriver_request(transport, 'http://PRIVATE_HOST/session/SECRET_SESSION', '/url', {})
                self.assertEqual(body.read.call_args_list[0].args, (16 * 1024,))
                self.assertEqual(body.read.call_count, 1)
                self.assertEqual(raised.exception.diagnostic,
                                 {'command': 'NAVIGATE', 'http_status': 500, 'error': 'UNKNOWN_DRIVER_ERROR'})

    def assert_local_page_closed(self, url):
        parsed = urlsplit(url)
        with socket.socket() as client:
            client.settimeout(1)
            self.assertNotEqual(client.connect_ex((parsed.hostname, parsed.port)), 0)

    def test_local_marker_page_is_loopback_unique_and_closes_after_success(self):
        opener = build_opener(ProxyHandler({}))
        with smoke._local_marker_page('fixture-unique-marker') as url:
            parsed = urlsplit(url)
            self.assertEqual(parsed.scheme, 'http')
            self.assertEqual(parsed.hostname, '127.0.0.1')
            self.assertGreater(parsed.port, 0)
            self.assertRegex(parsed.path, r'^/[a-f0-9]{32}$')
            with opener.open(url, timeout=2) as response:
                page = response.read().decode('utf-8')
            self.assertIn('<div id="proof">fixture-unique-marker</div>', page)
            self.assertNotIn('src=', page)
            self.assertNotIn('href=', page)
            for unknown in (url + '/unknown', url + '?unknown=1'):
                with self.assertRaises(HTTPError) as raised:
                    opener.open(unknown, timeout=2)
                self.assertEqual(raised.exception.code, 404)
                raised.exception.close()
        self.assert_local_page_closed(url)

    def test_local_marker_page_closes_after_validation_failure(self):
        with self.assertRaisesRegex(ValueError, 'fixture-failure'):
            with smoke._local_marker_page('fixture-marker') as url:
                raise ValueError('fixture-failure')
        self.assert_local_page_closed(url)

    def test_local_page_cleanup_failure_keeps_original_webdriver_diagnostic(self):
        server = SimpleNamespace(server_address=('127.0.0.1', 12345), serve_forever=Mock(), shutdown=Mock(),
                                 server_close=Mock(side_effect=OSError('SECRET_CLEANUP_FAILURE')))
        worker, stopper = Mock(), Mock()
        worker.is_alive.return_value = stopper.is_alive.return_value = False
        original = smoke.WebDriverSmokeError('NAVIGATE', 500, 'unsupported operation')
        with patch.object(smoke, 'ThreadingHTTPServer', return_value=server), \
                patch.object(smoke.threading, 'Thread', side_effect=[worker, stopper]):
            with self.assertRaises(smoke.WebDriverSmokeError) as raised:
                with smoke._local_marker_page('fixture-marker'):
                    raise original
        self.assertIs(raised.exception, original)
        report = self.run_mock_smoke(raised.exception)
        self.assertEqual(report['webdriver_diagnostic'],
                         {'command': 'NAVIGATE', 'http_status': 500, 'error': 'unsupported operation'})
        self.assertEqual(report['cleanup_error_code'], 'LOCAL_SMOKE_SERVER_SHUTDOWN_FAILED')
        self.assertNotIn('SECRET', json.dumps(report))
        server.server_close.assert_called_once()
        worker.join.assert_called_once_with(timeout=2)
        stopper.join.assert_called_once_with(timeout=2)

    def test_flower_navigates_local_http_and_requires_exact_dom_marker(self):
        # Real local HTTP only; profile, browser and WebDriver are isolated stubs.
        for marker_matches, delete_fails in ((True, False), (False, False), (True, True), (False, True)):
            with self.subTest(marker_matches=marker_matches, delete_fails=delete_fails), tempfile.TemporaryDirectory(prefix='mock-flower-http-only-') as directory:
                root = Path(directory)
                driver = root / 'geckodriver.exe'
                driver.touch()
                api_paths, navigations = [], []
                local_opener = build_opener(ProxyHandler({}))
                marker = None

                def api(base, path, **kwargs):
                    api_paths.append(path)
                    if path == '/api/v1/user/create':
                        return {'data': {'id': 'fixture-profile'}}
                    if path.startswith('/api/v1/browser/start?'):
                        return {'data': {'webdriver': str(driver)}}
                    if path.startswith('/api/v1/browser/active?'):
                        return {'data': {'status': 'inactive'}}
                    if path == '/api/v2/browser-profile/delete' and delete_fails:
                        raise provider_error(path, -1, 'permission denied SECRET_CLEANUP')
                    if path.startswith('/api/v1/user/list?'):
                        return {'data': {'list': []}}
                    return {'code': 0}

                def stop_wait(base, profile, *, api, timeout):
                    api(base, '/api/v1/browser/stop?user_id=' + profile)
                    self.assertEqual(api(base, '/api/v1/browser/active?user_id=' + profile)['data']['status'], 'inactive')

                def launcher(command, **kwargs):
                    folder = root / 'runtime' / command[command.index('-WorkerName') + 1]
                    folder.mkdir(parents=True)
                    (folder / 'flower_session_id.txt').write_text('fixture-session', encoding='utf-8')
                    (folder / 'flower_bidi_url.txt').write_text('ws://127.0.0.1/fixture', encoding='utf-8')
                    (folder / 'binding.json').write_text('{"foreground":{"activated":true}}', encoding='utf-8')
                    (folder / 'driver.json').write_text(json.dumps({'pid': 123, 'profile_id': 'fixture-profile',
                        'port': 41000, 'started_ticks': 621355968010000000}), encoding='utf-8')
                    return SimpleNamespace(returncode=0)

                def driver_request(request, **kwargs):
                    nonlocal marker
                    suffix = request.full_url.split('/session/fixture-session', 1)[1]
                    if suffix == '/url':
                        url = json.loads(request.data)['url']
                        navigations.append(url)
                        self.assertEqual(urlsplit(url).hostname, '127.0.0.1')
                        self.assertTrue(url.startswith('http://127.0.0.1:'))
                        with local_opener.open(url, timeout=2) as response:
                            marker = re.search(r'<div id="proof">([^<]+)</div>', response.read().decode('utf-8'))[1]
                        value = None
                    else:
                        self.assertEqual(suffix, '/execute/sync')
                        value = marker if marker_matches else marker + '-mismatch'
                    return io.BytesIO(json.dumps({'value': value}).encode('utf-8'))

                modules = {
                    'company_setup': SimpleNamespace(ensure_adspower_api=lambda company: None, free_block=lambda *a, **k: [41000]),
                    'replace_proxyshare_worker_environment': SimpleNamespace(api_call=api, stop_profile_and_wait=stop_wait),
                    'selection_automation.secrets': SimpleNamespace(load=lambda path: {}),
                    'selection_automation.background_launch': SimpleNamespace(run_background_launcher=launcher),
                    'psutil': SimpleNamespace(NoSuchProcess=ProcessLookupError, Process=lambda pid: SimpleNamespace(
                        create_time=lambda: 1.0, exe=lambda: str(driver), terminate=lambda: None, wait=lambda timeout: None)),
                }
                with patch.dict(sys.modules, modules), patch.object(smoke, 'build_opener', return_value=SimpleNamespace(open=driver_request)), \
                        patch.object(smoke.time, 'monotonic', side_effect=itertools.chain((0, 1), itertools.repeat(16))), patch.object(smoke.time, 'sleep'):
                    if marker_matches and not delete_fails:
                        result = smoke.check_flower(root)
                        self.assertTrue(all(result[key] for key in ('ready', 'webdriver', 'bidi', 'foreground', 'dom_read')))
                        self.assertTrue(result['cleanup']['ready'])
                        self.assertEqual(result['cleanup']['readback_api_calls'], 1)
                    else:
                        expected = 'DEPLOYMENT_PROFILE_CLEANUP_INCOMPLETE' if marker_matches else 'FIREFOX_DOM_READ_FAILED'
                        with self.assertRaisesRegex(RuntimeError, '^' + expected + '$') as raised:
                            smoke.check_flower(root)
                        failed = self.run_mock_smoke(raised.exception)
                        self.assertEqual(failed['flower_cleanup']['ready'], not delete_fails)
                        if delete_fails:
                            self.assertEqual(failed['flower_cleanup']['adspower_diagnostic']['category'], 'AUTHORIZATION')
                        self.assertNotIn('SECRET', json.dumps(failed))
                self.assertEqual(len(navigations), 1)
                self.assertIn('/api/v1/browser/stop?user_id=fixture-profile', api_paths)
                self.assertIn('/api/v1/browser/active?user_id=fixture-profile', api_paths)
                self.assertIn('/api/v2/browser-profile/delete', api_paths)
                self.assert_local_page_closed(navigations[0])

    def cleanup_fixture(self, directory, events):
        root = Path(directory)
        folder = root / 'state'
        folder.mkdir()
        driver = root / 'geckodriver.exe'
        driver.touch()
        record = {'pid': 123, 'profile_id': 'PRIVATE_PROFILE', 'port': 41000,
                  'started_ticks': 621355968010000000}
        (folder / 'driver.json').write_text(json.dumps(record), encoding='utf-8')
        process = Mock()
        process.create_time.return_value = 1.0
        process.exe.return_value = str(driver)
        process.terminate.side_effect = lambda: events.append('DRIVER_RELEASE')
        process.wait.side_effect = lambda timeout: events.append('DRIVER_EXIT_CONFIRMED')
        psutil = SimpleNamespace(Process=Mock(return_value=process), NoSuchProcess=ProcessLookupError,
                                 AccessDenied=PermissionError)
        return folder, driver, process, psutil

    def test_cleanup_releases_driver_then_waits_for_inactive_api_and_process_exit(self):
        # Import the existing business helper; all process/API observations are stubs.
        spec = importlib.util.spec_from_file_location('cleanup_shared_stop_test', ROOT / 'scripts/replace_proxyshare_worker_environment.py')
        replacement = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(replacement)
        events = []
        with tempfile.TemporaryDirectory(prefix='mock-flower-cleanup-order-') as directory:
            folder, driver, process, psutil = self.cleanup_fixture(directory, events)
            statuses, alive = iter(('Active', 'Inactive', 'Inactive')), iter((True, True, False))
            def processes(*args):
                return [SimpleNamespace(info={'name': 'FlowerBrowser.exe',
                        'cmdline': ['--user-data-dir=C:/fixture/PRIVATE_PROFILE_flower']})] if next(alive) else []
            psutil.process_iter = processes
            def api(base, path, **kwargs):
                if '/browser/stop?' in path:
                    events.append('STOP')
                elif '/browser/active?' in path:
                    status = next(statuses)
                    events.append(status)
                    return {'data': {'status': status}}
                elif path == '/api/v2/browser-profile/delete':
                    self.assertEqual(kwargs['body'], {'profile_id': ['PRIVATE_PROFILE']})
                    events.append('DELETE')
                else:
                    self.assertIn('user_id=PRIVATE_PROFILE', path)
                    events.append('READBACK')
                    return {'data': {'list': []}}
                return {'code': 0}
            from selection_automation import profile_single_use
            with patch.dict(sys.modules, {'psutil': psutil}), patch.object(profile_single_use, 'psutil', psutil), patch.object(smoke.time, 'sleep'):
                result = smoke._cleanup_flower_profile(folder, 'PRIVATE_PROFILE', driver, 41000, 'loopback',
                                                        api, replacement.stop_profile_and_wait, psutil)
        self.assertTrue(result['ready'])
        self.assertEqual(events, ['DRIVER_RELEASE', 'DRIVER_EXIT_CONFIRMED', 'STOP', 'Active', 'Inactive', 'Inactive', 'DELETE', 'READBACK'])
        self.assertEqual(result['delete_api_calls'], 1)
        self.assertEqual(result['readback_api_calls'], 1)
        self.assertEqual(result['events'][-1]['confirmation'], 'READBACK_ABSENT')
        self.assertNotIn('PRIVATE', json.dumps(result))
        process.wait.assert_called_once_with(timeout=5)

    def test_cleanup_refuses_missing_or_mismatched_owned_driver_identity(self):
        for mismatch in ('missing', 'profile', 'port', 'created', 'exe'):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory(prefix='mock-cleanup-identity-') as directory:
                folder, driver, process, psutil = self.cleanup_fixture(directory, [])
                record_path = folder / 'driver.json'
                record = json.loads(record_path.read_text('utf-8'))
                if mismatch == 'missing':
                    record_path.unlink()
                elif mismatch == 'exe':
                    process.exe.return_value = str(driver.parent / 'other.exe')
                else:
                    key, value = {'profile': ('profile_id', 'OTHER'), 'port': ('port', 41001),
                                  'created': ('started_ticks', record['started_ticks'] + 1000000)}[mismatch]
                    record[key] = value
                    record_path.write_text(json.dumps(record), encoding='utf-8')
                api, stop = Mock(), Mock()
                result = smoke._cleanup_flower_profile(folder, 'PRIVATE_PROFILE', driver, 41000, 'loopback', api, stop, psutil)
                self.assertFalse(result['ready'])
                self.assertEqual(result['failed_phase'], 'RELEASE_OWNED_DRIVER')
                process.terminate.assert_not_called()
                api.assert_not_called()
                stop.assert_not_called()

    def test_cleanup_delete_known_transient_is_bounded_and_other_errors_stop(self):
        for category, recover in (('PROFILE_IN_USE', True), ('PROFILE_IN_USE', False), ('AUTHORIZATION', False), ('PROVIDER_REJECTED', False)):
            with self.subTest(category=category, recover=recover), tempfile.TemporaryDirectory(prefix='mock-cleanup-delete-') as directory:
                folder, driver, process, psutil = self.cleanup_fixture(directory, [])
                calls = []
                def api(base, path, **kwargs):
                    calls.append(path)
                    if '/user/list?' in path:
                        return {'data': {'list': []}}
                    if recover and len(calls) > 1:
                        return {'code': 0}
                    raise smoke.AdsPowerAPIError(path, category, 'SECRET_PROVIDER_MESSAGE', provider_code=-1)
                with patch.object(smoke.time, 'sleep'):
                    result = smoke._cleanup_flower_profile(folder, 'PRIVATE_PROFILE', driver, 41000, 'loopback', api,
                                                            lambda *a, **k: None, psutil)
                self.assertEqual(result['ready'], recover)
                self.assertEqual(result['delete_api_calls'], 2 if recover else 3 if category == 'PROFILE_IN_USE' else 1)
                self.assertLessEqual(result['delete_api_calls'], 3)
                self.assertNotIn('SECRET', json.dumps(result))
                if not recover:
                    self.assertEqual(result['failed_phase'], 'DELETE_PROFILE')
                    self.assertEqual(result['adspower_diagnostic']['category'], category)
                    report = self.run_mock_smoke(smoke.FlowerCleanupError(result))
                    self.assertFalse(report['ready'])
                    self.assertEqual(report['failed_stage'], 'flower_binding')
                    self.assertEqual(report['error_code'], 'DEPLOYMENT_PROFILE_CLEANUP_INCOMPLETE')

    def test_cleanup_uncertain_delete_reads_exact_id_before_retry(self):
        for first_read, expected, deletes in (([], True, 1), ([{'user_id': 'PRIVATE_PROFILE'}], True, 2),
                ([{'user_id': 'OTHER_PROFILE'}], False, 1), ([{}], False, 1),
                ([{'user_id': 'PRIVATE_PROFILE', 'id': 'OTHER'}], False, 1), (None, False, 1)):
            with self.subTest(first_read=first_read), tempfile.TemporaryDirectory(prefix='mock-cleanup-readback-') as directory:
                folder, driver, process, psutil = self.cleanup_fixture(directory, [])
                calls, reads = [], []
                def api(base, path, **kwargs):
                    if path == '/api/v2/browser-profile/delete':
                        calls.append(path)
                        if len(calls) == 1:
                            raise smoke.AdsPowerAPIError(path, 'CONNECTIVITY', 'SECRET_OUTCOME_UNKNOWN', retryable=False)
                        return {'code': 0}
                    self.assertIn('user_id=PRIVATE_PROFILE', path)
                    reads.append(path)
                    return {'data': {'list': first_read if len(reads) == 1 else []}}
                with patch.object(smoke.time, 'sleep'):
                    result = smoke._cleanup_flower_profile(folder, 'PRIVATE_PROFILE', driver, 41000, 'loopback', api,
                                                            lambda *a, **k: None, psutil)
                self.assertEqual(result['ready'], expected)
                self.assertEqual(result['delete_api_calls'], deletes)
                self.assertEqual(len(calls), deletes)
                self.assertNotIn('PRIVATE', json.dumps(result))
                self.assertNotIn('SECRET', json.dumps(result))

    def test_cleanup_delete_ack_without_confirmed_absence_still_fails(self):
        for items in ([{'user_id': 'PRIVATE_PROFILE'}], [{'user_id': 'OTHER'}], None):
            with self.subTest(items=items), tempfile.TemporaryDirectory(prefix='mock-cleanup-ack-') as directory:
                folder, driver, process, psutil = self.cleanup_fixture(directory, [])
                def api(base, path, **kwargs):
                    return {'code': 0} if path == '/api/v2/browser-profile/delete' else {'data': {'list': items}}
                result = smoke._cleanup_flower_profile(folder, 'PRIVATE_PROFILE', driver, 41000, 'loopback', api,
                                                        lambda *a, **k: None, psutil)
                self.assertFalse(result['ready'])
                self.assertEqual(result['delete_api_calls'], 1)
                self.assertEqual(result['readback_api_calls'], 1)

    def test_cleanup_stop_wait_failure_never_deletes(self):
        with tempfile.TemporaryDirectory(prefix='mock-cleanup-stop-') as directory:
            folder, driver, process, psutil = self.cleanup_fixture(directory, [])
            api = Mock()
            stop = Mock(side_effect=smoke.AdsPowerAPIError('/api/v1/browser/stop', 'AUTHORIZATION', 'SECRET_DENIED'))
            result = smoke._cleanup_flower_profile(folder, 'PRIVATE_PROFILE', driver, 41000, 'loopback', api, stop, psutil)
        self.assertFalse(result['ready'])
        self.assertEqual(result['failed_phase'], 'STOP_AND_WAIT')
        self.assertEqual(result['adspower_diagnostic']['operation'], 'BROWSER_STOP')
        self.assertNotIn('SECRET', json.dumps(result))
        api.assert_not_called()
        stop.assert_called_once_with('loopback', 'PRIVATE_PROFILE', api=api, timeout=25)

    def test_other_failure_stage_and_existing_error_code_are_unchanged(self):
        report = self.run_mock_smoke(RuntimeError('CHROMIUM_LAUNCH_FAILED'), failed_check='check_chromium')
        self.assertEqual(report['failed_stage'], 'chromium')
        self.assertEqual(report['error_type'], 'RuntimeError')
        self.assertEqual(report['error_code'], 'CHROMIUM_LAUNCH_FAILED')
        self.assertNotIn('adspower_diagnostic', report)
        self.assertFalse(report['ready'])


if __name__ == '__main__':
    unittest.main()
