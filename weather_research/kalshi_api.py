from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


@dataclass
class KalshiClient:
    base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
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
        return cls(api_key_id=key, private_key_pem=pem)

    def _headers(self, method: str, path: str) -> dict[str, str]:
        if not self.api_key_id or not self.private_key_pem:
            return {}
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
        headers = self._headers("GET", f"/trade-api/v2{path}")
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(url, params=params, headers=headers)
            response.raise_for_status()
            return response.json()

    def discover_incentive_path(self) -> tuple[str, dict[str, Any]]:
        """One authenticated call settles the literal endpoint; retain fallback for compatibility."""
        errors: dict[str, str] = {}
        for path in ("/incentive_programs", "/incentives"):
            try:
                return path, self.get(path, params={"status": "active"})
            except httpx.HTTPStatusError as exc:
                errors[path] = f"HTTP {exc.response.status_code}: {exc.response.text[:200]}"
        raise RuntimeError(json.dumps({"incentive_path_discovery_failed": errors}, sort_keys=True))
