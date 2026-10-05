"""OpenAIx resource peer discovery and state retrieval."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from urllib.parse import quote

import httpx


@dataclass(frozen=True, slots=True)
class PeerResourceResult:
    """Describe one peer resource probe outcome and optional public resource state."""

    status: str
    resource: dict | None = None
    error: str | None = None


class PeerResourceClient:
    """Retrieve sanitized resource state from configured OpenAIx providers."""

    def __init__(self, full_config: dict | None = None) -> None:
        self._full_config = full_config if isinstance(full_config, dict) else {}
        self._capabilities: dict[str, bool] = {}

    def is_candidate(self, provider_id: str | None) -> bool:
        """Return whether a provider can be probed as an OpenAIx peer."""
        provider = self._provider_config(provider_id)
        return str(provider.get("api") or "").strip() == "openaix"

    async def probe_resource(
        self,
        provider_id: str | None,
        resource_id: str,
        timeout_ms: int,
    ) -> PeerResourceResult | None:
        """Probe one provider resource, caching only definitive non-peer outcomes."""
        normalized_provider_id = str(provider_id or "").strip()
        if not self.is_candidate(normalized_provider_id):
            return None
        if self._capabilities.get(normalized_provider_id) is False:
            return PeerResourceResult("not_peer")

        provider = self._provider_config(normalized_provider_id)
        base_url = str(provider.get("baseUrl") or "").rstrip("/")
        if not base_url:
            return PeerResourceResult("failed", error="peer provider has no baseUrl")

        encoded_resource = quote(str(resource_id), safe="")
        urls = [
            f"{base_url}/v1/resources/{encoded_resource}",
            f"{base_url}/api/resources/{encoded_resource}",
        ]
        timeout_seconds = max(0.001, timeout_ms / 1000.0)
        saw_not_found = False

        try:
            async with httpx.AsyncClient(
                timeout=timeout_seconds,
                headers=self._auth_headers(provider.get("auth")),
            ) as client:
                for url in urls:
                    try:
                        response = await client.get(url)
                    except httpx.TimeoutException:
                        return PeerResourceResult("failed", error="peer resource request timed out")
                    except httpx.HTTPError as exc:
                        return PeerResourceResult("failed", error=f"peer resource request failed: {exc}")

                    if response.status_code in {401, 403}:
                        self._capabilities[normalized_provider_id] = False
                        return PeerResourceResult("not_peer", error=f"peer authorization returned HTTP {response.status_code}")
                    if response.status_code == 404:
                        saw_not_found = True
                        continue
                    if response.status_code < 200 or response.status_code >= 300:
                        return PeerResourceResult("failed", error=f"peer resource returned HTTP {response.status_code}")
                    try:
                        payload = response.json()
                    except ValueError:
                        self._capabilities[normalized_provider_id] = False
                        return PeerResourceResult("not_peer", error="peer resource response is not JSON")
                    if not self._valid_resource_payload(payload):
                        self._capabilities[normalized_provider_id] = False
                        return PeerResourceResult("not_peer", error="peer resource response has an incompatible schema")
                    self._capabilities[normalized_provider_id] = True
                    return PeerResourceResult("ok", resource=payload)
        except httpx.HTTPError as exc:
            return PeerResourceResult("failed", error=f"peer resource request failed: {exc}")

        if saw_not_found:
            self._capabilities[normalized_provider_id] = False
            return PeerResourceResult("not_peer", error="peer resource endpoint is unavailable")
        return PeerResourceResult("failed", error="peer resource request did not complete")

    def _provider_config(self, provider_id: str | None) -> dict:
        """Return one configured provider object."""
        providers = ((self._full_config.get("models") or {}).get("providers") or {})
        provider = providers.get(str(provider_id or "").strip()) if isinstance(providers, dict) else None
        return provider if isinstance(provider, dict) else {}

    @staticmethod
    def _auth_headers(auth: object) -> dict[str, str]:
        """Build provider authentication headers for a peer request."""
        if not isinstance(auth, dict):
            return {}
        headers = {
            str(key): str(value)
            for key, value in (auth.get("headers") or {}).items()
            if isinstance(key, str) and isinstance(value, str)
        }
        authorization = auth.get("authorization")
        if isinstance(authorization, str) and authorization.strip():
            headers["Authorization"] = authorization.strip()
        auth_type = str(auth.get("type") or "bearer").strip().lower()
        token = auth.get("token")
        if isinstance(token, str) and token.strip() and auth_type in {"", "bearer", "token"}:
            headers["Authorization"] = f"Bearer {token.strip()}"
        if auth_type == "basic":
            username = auth.get("username")
            password = auth.get("password")
            if isinstance(username, str) and isinstance(password, str):
                encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
                headers["Authorization"] = f"Basic {encoded}"
        return headers

    @staticmethod
    def _valid_resource_payload(payload: object) -> bool:
        """Return whether a peer response contains usable resource availability."""
        if not isinstance(payload, dict):
            return False
        availability = payload.get("availability")
        return (
            isinstance(payload.get("id"), str)
            and isinstance(payload.get("limits"), dict)
            and isinstance(availability, dict)
            and isinstance(availability.get("available"), dict)
        )
