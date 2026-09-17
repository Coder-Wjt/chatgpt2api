from types import SimpleNamespace
from services import oauth_login_service as oauth


def test_oauth_verifies_tls_even_with_insecure_proxy_setting(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        oauth.proxy_settings,
        "build_session_kwargs",
        lambda **kw: {**kw, "verify": False},
    )

    class Session:
        def __init__(self, **kw):
            captured.update(kw)

        def post(self, *a, **kw):
            return SimpleNamespace(
                status_code=200,
                text="fixture",
                json=lambda: {
                    "access_token": "fixture-access",
                    "refresh_token": "fixture-refresh",
                    "id_token": "fixture-id",
                },
            )

        def close(self):
            pass

    monkeypatch.setattr(oauth.requests, "Session", Session)
    oauth.OAuthLoginService._exchange_code(
        "fixture-code", "fixture-verifier", "https://example.com"
    )
    assert captured["verify"] is True
