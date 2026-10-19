import pytest


@pytest.fixture
def offline_api_transport(monkeypatch):
    import openai
    from offline_visual_api import OfflineTransport
    monkeypatch.setattr(openai, 'DefaultHttpxClient', OfflineTransport)
