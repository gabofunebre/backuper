import os
import sys
import pytest
import requests

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from orchestrator.services.client import BackupClient


class DummyResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if not 200 <= self.status_code < 300:
            raise requests.HTTPError(self.status_code)

    def json(self):
        return self._json


def test_check_capabilities_ok(monkeypatch):
    def fake_get(url, headers, timeout):
        assert url == "http://example/backup/capabilities"
        return DummyResponse({"version": "v1", "types": ["db"], "est_seconds": 1, "est_size": 2})

    monkeypatch.setattr(requests, "get", fake_get)
    client = BackupClient("http://example", "token")
    assert client.check_capabilities() is True


def test_check_capabilities_missing_field(monkeypatch):
    def fake_get(url, headers, timeout):
        return DummyResponse({"version": "v1"})

    monkeypatch.setattr(requests, "get", fake_get)
    client = BackupClient("http://example", "token")
    with pytest.raises(ValueError):
        client.check_capabilities()


def test_check_capabilities_bad_version(monkeypatch):
    def fake_get(url, headers, timeout):
        return DummyResponse({"version": "v2", "types": ["db"]})

    monkeypatch.setattr(requests, "get", fake_get)
    client = BackupClient("http://example", "token")
    with pytest.raises(ValueError):
        client.check_capabilities()
