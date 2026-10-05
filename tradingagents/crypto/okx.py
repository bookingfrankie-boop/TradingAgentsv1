from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import requests


class OKXError(RuntimeError):
    """Raised when OKX returns a non-zero API code or a transport error."""


@dataclass(frozen=True)
class Instrument:
    inst_id: str
    base_ccy: str
    quote_ccy: str
    min_size: Decimal
    lot_size: Decimal
    tick_size: Decimal
    state: str


def _decimal(value: str | int | float | Decimal) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise OKXError(f"Invalid decimal returned by OKX: {value!r}") from exc


def _iso_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _join_path(path: str, params: dict[str, str] | None) -> str:
    if not params:
        return path
    return f"{path}?{urlencode(params)}"


class OKXClient:
    """Small dependency-light OKX V5 REST client.

    Public market-data calls require no secrets. Private calls use the V5 HMAC
    signing scheme. For EEA accounts the default host is eea.okx.com; override
    it with OKX_BASE_URL when the account region requires another host.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        secret_key: str | None = None,
        passphrase: str | None = None,
        simulated: bool = False,
        timeout: float = 15.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("OKX_BASE_URL") or "https://eea.okx.com").rstrip("/")
        self.api_key = api_key or os.getenv("OKX_API_KEY")
        self.secret_key = secret_key or os.getenv("OKX_API_SECRET")
        self.passphrase = passphrase or os.getenv("OKX_API_PASSPHRASE")
        self.simulated = simulated or os.getenv("OKX_ENV", "live").lower() == "demo"
        self.timeout = timeout
        self.session = session or requests.Session()

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        body: str = "",
        private: bool = False,
    ) -> dict:
        method = method.upper()
        request_path = _join_path(path, params)
        url = f"{self.base_url}{request_path}"
        headers = {"Content-Type": "application/json"}

        if private:
            if not all((self.api_key, self.secret_key, self.passphrase)):
                raise OKXError(
                    "Private OKX endpoint requires OKX_API_KEY, OKX_API_SECRET and "
                    "OKX_API_PASSPHRASE"
                )
            timestamp = _iso_timestamp()
            prehash = f"{timestamp}{method}{request_path}{body}"
            digest = hmac.new(
                self.secret_key.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256
            ).digest()
            headers.update(
                {
                    "OK-ACCESS-KEY": self.api_key,
                    "OK-ACCESS-SIGN": base64.b64encode(digest).decode("ascii"),
                    "OK-ACCESS-TIMESTAMP": timestamp,
                    "OK-ACCESS-PASSPHRASE": self.passphrase,
                }
            )
        if self.simulated:
            headers["x-simulated-trading"] = "1"

        try:
            response = self.session.request(
                method,
                url,
                params=None,
                data=body or None,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise OKXError(f"OKX transport error: {exc}") from exc
        except ValueError as exc:
            raise OKXError("OKX returned non-JSON response") from exc

        if payload.get("code") != "0":
            raise OKXError(f"OKX error {payload.get('code')}: {payload.get('msg', 'unknown error')}")
        return payload

    def get_instruments(self, quote_ccy: str | None = "USDC") -> list[Instrument]:
        params = {"instType": "SPOT"}
        if quote_ccy:
            params["quoteCcy"] = quote_ccy.upper()
        data = self._request("GET", "/api/v5/public/instruments", params=params)["data"]
        return [
            Instrument(
                inst_id=row["instId"],
                base_ccy=row.get("baseCcy", ""),
                quote_ccy=row.get("quoteCcy", ""),
                min_size=_decimal(row.get("minSz", "0")),
                lot_size=_decimal(row.get("lotSz", row.get("minSz", "0"))),
                tick_size=_decimal(row.get("tickSz", "0")),
                state=row.get("state", ""),
            )
            for row in data
        ]

    def get_ticker(self, inst_id: str) -> dict:
        rows = self._request("GET", "/api/v5/market/ticker", params={"instId": inst_id})["data"]
        if not rows:
            raise OKXError(f"No ticker returned for {inst_id}")
        return rows[0]

    def get_candles(self, inst_id: str, bar: str = "15m", limit: int = 300) -> list[dict]:
        limit = max(1, min(int(limit), 300))
        rows = self._request(
            "GET",
            "/api/v5/market/candles",
            params={"instId": inst_id, "bar": bar, "limit": str(limit)},
        )["data"]
        rows = list(reversed(rows))
        result = []
        for row in rows:
            if len(row) < 9:
                continue
            result.append(
                {
                    "ts": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "confirm": row[8],
                }
            )
        return result

    def get_account_balance(self) -> list[dict]:
        return self._request("GET", "/api/v5/account/balance", private=True)["data"]

    def get_account_config(self) -> list[dict]:
        return self._request("GET", "/api/v5/account/config", private=True)["data"]

    def place_spot_limit_order(
        self,
        inst_id: str,
        side: str,
        price: str,
        size: str,
        client_order_id: str | None = None,
    ) -> dict:
        if os.getenv("OKX_TRADING_ENABLED", "false").lower() not in {"true", "1", "yes"}:
            raise OKXError(
                "Order path is locked: set OKX_TRADING_ENABLED=true explicitly"
            )
        if side not in {"buy", "sell"}:
            raise OKXError("side must be buy or sell")

        payload = {
            "instId": inst_id,
            "tdMode": "cash",
            "side": side,
            "ordType": "limit",
            "px": price,
            "sz": size,
        }
        if client_order_id:
            payload["clOrdId"] = client_order_id

        import json

        body = json.dumps(payload, separators=(",", ":"))
        return self._request("POST", "/api/v5/trade/order", body=body, private=True)["data"]
