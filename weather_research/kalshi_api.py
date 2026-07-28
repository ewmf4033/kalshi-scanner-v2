from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Iterable

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


@dataclass
class KalshiClient:
    base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    websocket_url: str = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
    api_key_id: str | None = None
    private_key_pem: str | None = None
    timeout: float = 15.0

    @classmethod
    def from_env(cls) -> "KalshiClient":
        key = os.environ.get("KALSHI_API_KEY_ID")
        pem = os.environ.get("KALSHI_PRIVATE_KEY_PEM")
        path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        if not pem and path:
            with open(path, "r", encoding="utf-8") as fh:
                pem = fh.read()
        return cls(
            base_url=os.environ.get("KALSHI_REST_URL", cls.base_url),
            websocket_url=os.environ.get("KALSHI_WS_URL", cls.websocket_url),
            api_key_id=key,
            private_key_pem=pem,
        )

    def auth_headers(self, method: str, path: str) -> dict[str, str]:
        if not self.api_key_id or not self.private_key_pem:
            raise RuntimeError("Kalshi API credentials are required")
        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method.upper()}{path.split('?', 1)[0]}".encode()
        key = serialization.load_pem_private_key(self.private_key_pem.encode(), password=None)
        signature = key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = self.auth_headers("GET", f"/trade-api/v2{path}")
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(url, params=params, headers=headers)
            response.raise_for_status()
            return response.json()

    def websocket_headers(self) -> dict[str, str]:
        return self.auth_headers("GET", "/trade-api/ws/v2")

    def list_markets(
        self,
        *,
        series_ticker: str,
        statuses: Iterable[str] = ("open", "unopened"),
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Return every requested-status market for a series, following Kalshi cursors."""
        if not series_ticker:
            raise ValueError("series_ticker is required")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        normalized_statuses = tuple(dict.fromkeys(str(status) for status in statuses))
        if not normalized_statuses:
            raise ValueError("at least one status is required")
        markets: list[dict[str, Any]] = []
        seen_tickers: set[str] = set()
        for status in normalized_statuses:
            cursor = ""
            while True:
                params: dict[str, Any] = {
                    "series_ticker": series_ticker,
                    "status": status,
                    "limit": limit,
                    "mve_filter": "exclude",
                }
                if cursor:
                    params["cursor"] = cursor
                page = self.get("/markets", params=params)
                for market in page.get("markets", []):
                    ticker = str(market.get("ticker", ""))
                    if ticker and ticker not in seen_tickers:
                        seen_tickers.add(ticker)
                        markets.append(market)
                cursor = str(page.get("cursor") or "")
                if not cursor:
                    break
        return markets

    def discover_incentive_path(self) -> tuple[str, dict[str, Any]]:
        errors: dict[str, str] = {}
        for path in ("/incentive_programs", "/incentives"):
            try:
                return path, self.get(path, params={"status": "active"})
            except httpx.HTTPStatusError as exc:
                errors[path] = f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
        raise RuntimeError(json.dumps({"incentive_path_discovery_failed": errors}, sort_keys=True))
