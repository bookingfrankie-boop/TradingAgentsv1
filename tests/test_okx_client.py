from tradingagents.crypto.okx import OKXClient


def test_signature_path_is_stable(monkeypatch):
    client = OKXClient(api_key="k", secret_key="s", passphrase="p")
    captured = {}

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"code": "0", "data": []}

    def fake_request(method, url, **kwargs):
        captured.update(
            method=method,
            url=url,
            headers=kwargs["headers"],
        )
        return R()

    monkeypatch.setattr(client.session, "request", fake_request)
    client._request("GET", "/api/v5/account/config", private=True)

    assert captured["method"] == "GET"
    assert captured["url"].endswith("/api/v5/account/config")
    assert captured["headers"]["OK-ACCESS-KEY"] == "k"
    assert captured["headers"]["OK-ACCESS-SIGN"]
    assert captured["headers"]["OK-ACCESS-PASSPHRASE"] == "p"
