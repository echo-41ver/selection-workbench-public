import importlib.util
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock

spec=importlib.util.spec_from_file_location('entry_trials',Path(__file__).resolve().parents[1]/'scripts/run_entry_trials.py')
trials=importlib.util.module_from_spec(spec)
spec.loader.exec_module(trials)

class EntryTrialsTests(unittest.TestCase):
    def test_continuation_uses_source_keywords_and_confirms_the_other_sort_first(self):
        queries=['fuel transfer pump','portable fuel transfer pump','electric siphon pump']
        self.assertEqual(trials.continuation_plan(queries[1],'recent',queries),[(queries[1],'sales'),(queries[2],'recent')])
        with self.assertRaises(ValueError):trials.continuation_plan(queries[0],'sales',queries[:1])
        for progress in ({'sales_sort':{'state':'UNCONFIRMED'}},{'sales_sort':{'state':'UI_CONFIRMED'},'manual_action_required':True}):
            self.assertFalse(trials.successful_step('KEYWORD_LIMIT_REACHED',progress,0))

    def test_all_source_queries_cover_both_sorts_in_circular_source_order(self):
        queries=['first','second','third']
        plan=trials.continuation_plan('second','recent',queries,True)
        self.assertEqual(plan,[('second','sales'),('third','recent'),('third','sales'),('first','recent'),('first','sales')])
        steps=[{'query':query,'sort':sort,'passed':True} for query,sort in plan]
        self.assertFalse(trials.successful_flow(plan,steps[:2],0))
        self.assertTrue(trials.successful_flow(plan,steps,0))
        self.assertFalse(trials.successful_flow(plan,steps,1))
        steps[-1]['query']='second'
        self.assertFalse(trials.successful_flow(plan,steps,0))

    def test_full_source_flow_stops_short_on_a_failed_step(self):
        plan=trials.continuation_plan('first','sales',['first','second'],True)
        steps=[{'query':query,'sort':sort,'passed':True} for query,sort in plan]
        steps[-1]['passed']=False
        self.assertFalse(trials.successful_flow(plan,steps,0))

    def test_followup_preserves_the_active_driver_and_separates_output(self):
        with TemporaryDirectory() as folder:
            out=Path(folder);stepdir=out/'followup';(stepdir/'data').mkdir(parents=True)
            (stepdir/'data/progress.json').write_text(json.dumps({'status':'KEYWORD_LIMIT_REACHED','unique_goods':400,'sales_sort':{'state':'UI_CONFIRMED'}}))
            process=Mock(pid=123);process.poll.return_value=0
            command=['node','collector','--query','first','--sort','sales','--output','primary','--stop-file','primary-stop','--webdriver-url','same-driver','--session-id-file','same-session']
            args=SimpleNamespace(case_seconds=60,deadline_timestamp=trials.time.time()+60)
            with patch.object(trials.subprocess,'Popen',return_value=process) as launch,patch.object(trials,'api_call',side_effect=AssertionError('followup must not create a profile')):
                result=trials.run_followup(args,out,stepdir,command,'second','recent','same-driver','same-session',Mock())
            self.assertTrue(result['passed'])
            actual=launch.call_args.args[0]
            self.assertEqual(actual[actual.index('--webdriver-url')+1],'same-driver')
            self.assertEqual(actual[actual.index('--session-id-file')+1],'same-session')
            self.assertEqual(actual[actual.index('--output')+1],str(stepdir/'data'))
            self.assertEqual(command[command.index('--query')+1],'first')

    def test_followup_stops_at_a_gate_and_never_claims_a_completed_flow(self):
        with TemporaryDirectory() as folder:
            out=Path(folder);stepdir=out/'followup';(stepdir/'data').mkdir(parents=True)
            (stepdir/'data/progress.json').write_text(json.dumps({'status':'RUNNING','unique_goods':25,'sales_sort':{'state':'UI_CONFIRMED'}}))
            process=Mock(pid=123);process.poll.side_effect=[None,None,2]
            args=SimpleNamespace(case_seconds=60,deadline_timestamp=trials.time.time()+60)
            command=['node','collector','--query','first','--sort','sales','--output','primary','--stop-file','primary-stop']
            with patch.object(trials.subprocess,'Popen',return_value=process),patch.object(trials,'driver_call',return_value={'url':'https://www.temu.com/bgn_verification.html'}),patch.object(trials.time,'sleep') as sleep:
                result=trials.run_followup(args,out,stepdir,command,'second','recent','driver','session',lambda page:trials.gate(page))
            self.assertEqual(result['reason'],'CAPTCHA')
            self.assertFalse(result['passed'])
            self.assertTrue((stepdir/'STOP').is_file())
            process.wait.assert_called_once_with(20)
            sleep.assert_not_called()

    def test_focused_retests_keep_the_requested_route_order_and_real_query(self):
        chosen=trials.selected_routes('electric siphon pump',True,'blank_home,startup_keyword_percent20')
        self.assertEqual([r[0] for r in chosen],['blank_home','startup_keyword_percent20'])
        self.assertIn('electric%20siphon%20pump',chosen[1][1])
        self.assertEqual(trials.selected_routes('pump'),trials.routes('pump'))
        for names in ('unknown','blank_home,blank_home','',',','startup_keyword_percent20'):
            with self.assertRaises(ValueError):trials.selected_routes('pump',False,names)

    def test_transient_empty_page_during_navigation_remains_pollable(self):
        with patch.object(trials.urllib.request,'build_opener') as opener:
            opener.return_value.open.return_value=io.BytesIO(b'{"value":null}')
            page=trials.driver_call('http://localhost','test','/execute/sync',{})
        self.assertEqual(page,{'unavailable':True})
        self.assertIsNone(trials.gate(page))
        self.assertNotIn('ready',page)

    def test_real_kernel_and_uri_encoding_are_explicit_trial_factors(self):
        routes=trials.routes('fuel transfer pump',True)
        self.assertIn('fuel+transfer+pump',routes[0][1])
        self.assertIn('fuel%20transfer%20pump',routes[-1][1])
        fingerprint=trials.trial_fingerprint('153','configured')
        self.assertEqual(fingerprint['browser_kernel_config'],{'type':'firefox','version':'153'})
        self.assertIn('Firefox/153.0',fingerprint['ua'])
        self.assertEqual(trials.trial_fingerprint('153','defaults'),{'browser_kernel_config':{'type':'firefox','version':'153'}})
    def test_attached_driver_is_released_before_vendor_browser_close(self):
        actions=[];info={'profile_id':'owned-trial'}
        with patch.object(trials,'owned_terminate',side_effect=lambda *a:actions.append('driver')),patch.object(trials,'api_base',return_value='local'),patch.object(trials,'stop_profile_and_wait',side_effect=lambda *a,**k:actions.append('stop')),patch.object(trials,'delete_profile_after_release',side_effect=lambda *a,**k:actions.append('delete')):
            trials.cleanup_profile(info,{'pid':123,'created':1})
        self.assertEqual(actions,['driver','stop','delete'])
        self.assertTrue(info['profile_closed_and_deleted'])

    def test_unreleased_profile_is_retained_instead_of_deleted(self):
        info={'profile_id':'owned-trial'}
        with patch.object(trials.time,'sleep'),patch.object(trials,'owned_terminate'),patch.object(trials,'api_base',return_value='local'),patch.object(trials,'stop_profile_and_wait',side_effect=RuntimeError('not released')),patch.object(trials,'delete_profile_after_release') as deletion:
            trials.cleanup_profile(info,None)
        deletion.assert_not_called()
        self.assertEqual(info['cleanup_phase'],'STOP_OWN_PROFILE')
        self.assertNotIn('profile_closed_and_deleted',info)
        self.assertEqual(info['cleanup_attempts'],3)

    def test_delayed_profile_shutdown_is_retried_without_creating_or_reopening(self):
        info={'profile_id':'owned-trial'}
        with patch.object(trials.time,'sleep') as delay,patch.object(trials,'owned_terminate') as driver,patch.object(trials,'api_base',return_value='local'),patch.object(trials,'stop_profile_and_wait',side_effect=[RuntimeError('still closing'),None]) as stop,patch.object(trials,'delete_profile_after_release') as deletion:
            trials.cleanup_profile(info,None)
        self.assertEqual(stop.call_count,2)
        driver.assert_called_once()
        deletion.assert_called_once_with('local','owned-trial',attempts=3)
        delay.assert_called_once_with(2)
        self.assertTrue(info['profile_closed_and_deleted'])
        self.assertNotIn('cleanup_error_type',info)

    def test_completed_process_is_not_misreported_as_a_successful_route(self):
        self.assertEqual(trials.terminal_reason('COLLECTOR_EXIT',{'status':'STOPPED_LOGIN_REQUIRED'}),'LOGIN_WALL')
        self.assertEqual(trials.terminal_reason('COLLECTOR_EXIT',{'status':'KEYWORD_LIMIT_REACHED'}),'KEYWORD_LIMIT_REACHED')

    def test_unexpected_exit_does_not_report_last_waiting_snapshot_or_success(self):
        for status in ('WAITING_HOME_SEARCH_STABLE','RUNNING','KEYWORD_LIMIT_REACHED'):
            self.assertEqual(trials.terminal_reason('COLLECTOR_EXIT',{'status':status},1),'INFRASTRUCTURE_ERROR')
        self.assertEqual(trials.terminal_reason('COLLECTOR_EXIT',{'status':'WAITING_HOME_SEARCH_STABLE'},0),'COLLECTOR_EXIT_WITHOUT_FINAL_PROGRESS')
        self.assertEqual(trials.terminal_reason('COLLECTOR_EXIT',{'status':'STOPPED_LOGIN_REQUIRED'},1),'LOGIN_WALL')
        self.assertEqual(trials.terminal_reason('BOUNDED_TIMEOUT_OR_STOP',{'status':'RUNNING'},2),'BOUNDED_TIMEOUT_OR_STOP')

    def test_explicit_network_stop_survives_expected_nonzero_exit(self):
        for status in ('STOPPED_PROXY_OR_NETWORK_FAILURE','STOPPED_PROXY_CONNECTION'):
            self.assertEqual(trials.terminal_reason('COLLECTOR_EXIT',{'status':status},2),'NETWORK_FAILURE')

    def test_explicit_browser_timeout_survives_expected_nonzero_exit(self):
        self.assertEqual(trials.terminal_reason('COLLECTOR_EXIT',{'status':'STOPPED_BROWSER_COMMAND_TIMEOUT'},2),
                         'STOPPED_BROWSER_COMMAND_TIMEOUT')

    def test_initial_products_timeout_is_not_reported_as_a_crash(self):
        self.assertEqual(trials.terminal_reason('COLLECTOR_EXIT',{'status':'STOPPED_INITIAL_PRODUCTS_TIMEOUT'},2),'STOPPED_INITIAL_PRODUCTS_TIMEOUT')
        self.assertFalse(trials.successful_step('STOPPED_INITIAL_PRODUCTS_TIMEOUT',{'sales_sort':{'state':'UI_CONFIRMED'}},2))

    def test_driver_owner_requires_profile_process_name_and_creation_identity(self):
        process=Mock(pid=42);process.create_time.return_value=1234567890.125;process.name.return_value='geckodriver.exe'
        driver={'pid':42,'profile_id':'owned','started_ticks':621355968000000000+12345678901250000}
        with patch.object(trials.psutil,'Process',return_value=process):
            self.assertEqual(trials.bound_driver_owner(driver,'owned'),{'pid':42,'created':1234567890.125})
            for invalid in ({**driver,'profile_id':'other'},{**driver,'started_ticks':driver['started_ticks']+10000000}):
                with self.assertRaisesRegex(RuntimeError,'OWNERSHIP_MISMATCH'):trials.bound_driver_owner(invalid,'owned')
            process.name.return_value='unrelated.exe'
            with self.assertRaisesRegex(RuntimeError,'OWNERSHIP_MISMATCH'):trials.bound_driver_owner(driver,'owned')

    def test_exited_driver_returns_a_bounded_identity_error(self):
        with patch.object(trials.psutil,'Process',side_effect=trials.psutil.NoSuchProcess(42)):
            with self.assertRaisesRegex(RuntimeError,'ISOLATED_DRIVER_UNAVAILABLE'):
                trials.bound_driver_owner({'pid':42,'profile_id':'owned'},'owned')

    def test_startup_keyword_is_a_configured_initial_tab_not_a_later_navigation(self):
        route=trials.routes('portable fuel transfer pump')[0]
        self.assertIn('search_key=portable+fuel+transfer+pump',route[1])
        self.assertEqual(route[2],[])
        self.assertEqual(route[3],'direct_search')

    def test_keyword_switch_uses_the_selected_ordinary_entry(self):
        self.assertEqual(trials.keyword_switch_navigation('home','fuel pump','electric pump'),(trials.HOME,'home_search'))
        self.assertEqual(trials.keyword_switch_navigation('empty','fuel pump','electric pump'),(trials.EMPTY,'empty_search'))
        self.assertEqual(trials.keyword_switch_navigation('direct','fuel pump','electric pump'),(trials.EMPTY+'?search_key=electric+pump','direct_search'))
        with self.assertRaises(ValueError):trials.keyword_switch_navigation('unknown','fuel pump','electric pump')

    def test_same_keyword_sorting_does_not_repeat_entry_navigation(self):
        for method in ('current','home','empty','direct'):
            self.assertIsNone(trials.keyword_switch_navigation(method,'fuel pump','fuel pump'))
        self.assertIsNone(trials.keyword_switch_navigation('current','fuel pump','electric pump'))

    def test_gate_during_keyword_entry_prevents_collector_start(self):
        args=SimpleNamespace(keyword_switch_entry='home',deadline_timestamp=9999999999)
        command=['node','collector','--query','fuel pump','--sort','sales','--output','old','--stop-file','old-stop','--entry-mode','home_search']
        with TemporaryDirectory() as folder,patch.object(trials,'driver_call',side_effect=[{}, {'ready':'complete'}]) as driver,patch.object(trials.subprocess,'Popen') as launch:
            step=trials.run_followup(args,Path(folder),Path(folder)/'step',command,'electric pump','sales','base','sid',lambda page:'CAPTCHA',previous_query='fuel pump')
            launch.assert_not_called()
            self.assertEqual(step['reason'],'CAPTCHA')
            self.assertFalse(step['passed'])
            self.assertEqual(driver.call_args_list[0].args[-1],{'url':trials.HOME})

    def test_navigation_order_and_submission_factors_are_distinct(self):
        routes={r[0]:r for r in trials.routes('fuel pump')}
        self.assertEqual(routes['empty_home'][1:4],(trials.EMPTY,[trials.HOME],'home_search'))
        self.assertEqual(routes['home_empty'][1:4],(trials.HOME,[trials.EMPTY],'empty_search'))
        self.assertEqual(routes['empty_enter'][4],'enter')
        self.assertEqual(routes['startup_empty_settled'][5],10)

    def test_gate_at_any_phase_ends_trial_including_login_overlay_on_search(self):
        self.assertEqual(trials.gate({'url':trials.HOME+'bgn_verification.html','goods':50}),'CAPTCHA')
        self.assertEqual(trials.gate({'url':trials.EMPTY,'goods':50,'loginFormVisible':True}),'LOGIN_WALL')
        self.assertEqual(trials.gate({'url':trials.HOME+'login.html'}),'LOGIN_WALL')
        self.assertEqual(trials.gate({'url':trials.EMPTY,'text':'Access denied'}),'ACCESS_DENIED')
        self.assertEqual(trials.gate({'url':trials.EMPTY,'text':'normal card','goods':50}),None)

if __name__=='__main__':unittest.main()
