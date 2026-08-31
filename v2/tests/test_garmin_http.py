import base64
import json
import time
from pathlib import Path
from unittest import mock

import pytest

from garmin_http import DI_TOKEN_URL, GarminAuthError, GarminTokenStore


def _jwt(claims: dict) -> str:
    def b64(x) -> str:
        return base64.urlsafe_b64encode(json.dumps(x).encode()).rstrip(b"=").decode()
    return f"{b64({'alg':'none'})}.{b64(claims)}.sig"


def _write_store(path: Path, token_claims: dict | None = None,
                 extra: dict | None = None) -> Path:
    claims = token_claims if token_claims is not None else {"exp": int(time.time()) + 7200}
    data = {"di_token": _jwt(claims), "di_refresh_token": "rt", "di_client_id": "cid"}
    if extra:
        data.update(extra)
    path.write_text(json.dumps(data))
    return path


def test_load_parses_tokenstore(tmp_path):
    p = _write_store(tmp_path / "v2.json")
    ts = GarminTokenStore(p, timeout=5.0)
    ts.load()
    assert ts.access_token() == _jwt({"exp": int(time.time()) + 7200})


def test_load_missing_file_raises(tmp_path):
    ts = GarminTokenStore(tmp_path / "nope.json", timeout=5.0)
    with pytest.raises(GarminAuthError):
        ts.load()


def test_load_invalid_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    ts = GarminTokenStore(p, timeout=5.0)
    with pytest.raises(GarminAuthError):
        ts.load()


def test_expires_soon_false_when_far_from_expiry(tmp_path):
    p = _write_store(tmp_path / "v2.json", token_claims={"exp": int(time.time()) + 7200})
    ts = GarminTokenStore(p, timeout=5.0)
    ts.load()
    assert ts.expires_soon() is False
    assert ts.has_valid_token() is True


def test_expires_soon_true_within_threshold(tmp_path):
    p = _write_store(tmp_path / "v2.json", token_claims={"exp": int(time.time()) + 300})
    ts = GarminTokenStore(p, timeout=5.0)
    ts.load()
    assert ts.expires_soon() is True


def test_refresh_posts_grant_and_persists(tmp_path):
    p = _write_store(tmp_path / "v2.json")
    resp = mock.Mock()
    resp.status_code = 200
    resp.json.return_value = {"access_token": "newtok", "refresh_token": "newrt"}
    with mock.patch("garmin_http.requests.post", return_value=resp) as post:
        ts = GarminTokenStore(p, timeout=5.0)
        ts.load()
        assert ts.refresh() is True
    _, kwargs = post.call_args
    assert kwargs["timeout"] == 5.0
    assert kwargs["data"]["grant_type"] == "refresh_token"
    saved = json.loads(p.read_text())
    assert saved["di_token"] == "newtok"
    assert saved["di_refresh_token"] == "newrt"


def test_refresh_non_200_returns_false(tmp_path):
    p = _write_store(tmp_path / "v2.json")
    resp = mock.Mock()
    resp.status_code = 401
    with mock.patch("garmin_http.requests.post", return_value=resp):
        ts = GarminTokenStore(p, timeout=5.0)
        ts.load()
        assert ts.refresh() is False
