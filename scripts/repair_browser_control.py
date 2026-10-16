"""Reconnect the owning lane's local driver, preserving its existing profile."""
def repair(session):
    from company_setup import rebind_existing_worker
    from replace_proxyshare_worker_environment import api_base, api_call
    port=int(str(session['webdriver_url']).rstrip('/').rsplit(':',1)[1])
    rebind_existing_worker(api_base(),session,port,api_call)
