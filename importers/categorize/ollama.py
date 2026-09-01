"""A loopback-only client for a locally running Ollama server.

This is the *only* place in the categorization stack that sends a raw merchant
description anywhere, and the destination is deliberately constrained so that
"anywhere" can only mean this machine:

* the URL scheme must be ``http`` or ``https`` and the host must be a loopback
  address (``127.0.0.0/8``, ``::1``) or ``localhost``;
* the host is re-checked on **every** request, not just at construction, so a
  redirected or rewritten base URL cannot quietly become remote;
* the opener is built with an empty :class:`~urllib.request.ProxyHandler`, so an
  ambient ``HTTP_PROXY``/``ALL_PROXY`` in the environment cannot forward a
  prompt to a corporate or cloud proxy;
* redirects are refused outright -- a 3xx from the model server is an error, not
  a hop to follow.

Nothing here reads or writes the private data directory, and nothing here logs a
prompt. The transport is injectable so tests exercise the whole client without a
socket.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

from importers.rebuild.decisions import DecisionError
from importers.rebuild.safety import plan_fingerprint

#: Ollama's own default bind address. Loopback, and never overridden silently.
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

#: A mixture-of-experts model small enough to answer a per-merchant question
#: quickly on consumer hardware while still knowing what a merchant sells.
DEFAULT_MODEL = "qwen3:30b-a3b"

#: A single categorization question is short. A minute is generous for a cold
#: model and still bounded, so a stalled server fails the command rather than
#: hanging a plan forever.
DEFAULT_TIMEOUT_SECONDS = 180.0

#: Context window requested per call. The prompt is one merchant plus a taxonomy,
#: so this is comfortable headroom rather than a tuning knob.
DEFAULT_NUM_CTX = 8192

#: Deterministic decoding. The same evidence must produce the same decision, or
#: the sealed cache and the sealed plan mean nothing.
DEFAULT_TEMPERATURE = 0.0
DEFAULT_SEED = 0

#: ``(method, url, body, timeout) -> (status, payload)``.
Transport = Callable[[str, str, bytes | None, float], tuple[int, bytes]]


def _is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname.strip("[]")).is_loopback
    except ValueError:
        return False


def require_loopback_url(url: str) -> str:
    """Return ``url`` normalized, or refuse if it could leave this machine."""
    target = urlparse(str(url or "").strip())
    if target.scheme not in {"http", "https"}:
        raise DecisionError("the local model endpoint must be an http(s) URL")
    if not _is_loopback(target.hostname):
        raise DecisionError(
            "the local model endpoint must be a loopback address; merchant "
            "descriptions are never sent off this machine"
        )
    if target.username or target.password or target.query or target.fragment:
        raise DecisionError("the local model endpoint must be a bare origin URL")
    return urlunparse((target.scheme, target.netloc, "", "", "", "")).rstrip("/")


def _default_transport() -> Transport:
    """A urllib transport with proxies disabled and redirects refused."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise DecisionError(
                "the local model endpoint attempted a redirect; refusing to follow"
            )

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect()
    )

    def transport(
        method: str, url: str, body: bytes | None, timeout: float
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        try:
            with opener.open(request, timeout=timeout) as response:
                return int(response.status), response.read()
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read()
        except urllib.error.URLError as exc:
            raise DecisionError(
                f"local model endpoint is unreachable: {exc.reason}"
            ) from None
        except OSError as exc:
            raise DecisionError(f"local model endpoint failed: {exc}") from None

    return transport


@dataclass(frozen=True)
class OllamaHealth:
    """Whether a local model server can answer, and with which model."""

    base_url: str
    reachable: bool
    version: str = ""
    installed_models: tuple[str, ...] = ()
    model: str = ""
    model_present: bool = False
    model_digest: str = ""
    parameter_size: str = ""
    quantization: str = ""
    detail: str = ""

    @property
    def ready(self) -> bool:
        return self.reachable and self.model_present

    def as_document(self) -> dict[str, Any]:
        return {
            "baseUrl": self.base_url,
            "loopback": True,
            "reachable": self.reachable,
            "version": self.version,
            "model": self.model,
            "modelPresent": self.model_present,
            "modelDigest": self.model_digest,
            "parameterSize": self.parameter_size,
            "quantization": self.quantization,
            "installedModelCount": len(self.installed_models),
            "detail": self.detail,
        }

    def summary(self) -> str:
        return (
            f"ollama endpoint={self.base_url} reachable={'yes' if self.reachable else 'no'} "
            f"version={self.version or 'unknown'} model={self.model} "
            f"present={'yes' if self.model_present else 'no'} "
            f"installed={len(self.installed_models)}"
        )


class OllamaClient:
    """Minimal, deterministic, loopback-only Ollama HTTP client.

    Only three endpoints are ever called: ``/api/version`` and ``/api/tags`` for
    health, ``/api/show`` for the model identity that gets sealed into a plan,
    and ``/api/chat`` for a single structured-output question at a time. No
    streaming, no embeddings, no model pulls -- a harness that could pull a model
    could also reach the network.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        *,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        num_ctx: int = DEFAULT_NUM_CTX,
        temperature: float = DEFAULT_TEMPERATURE,
        seed: int = DEFAULT_SEED,
        transport: Transport | None = None,
    ) -> None:
        self.base_url = require_loopback_url(base_url)
        self.model = str(model or "").strip()
        if not self.model:
            raise DecisionError("a local model name is required")
        if not (timeout > 0):
            raise DecisionError("the local model timeout must be positive")
        self.timeout = float(timeout)
        self.num_ctx = int(num_ctx)
        self.temperature = float(temperature)
        self.seed = int(seed)
        self._transport = transport or _default_transport()

    # -- transport ---------------------------------------------------------

    def _call(self, method: str, path: str, payload: Any | None = None) -> Any:
        url = f"{self.base_url}{path}"
        # Re-check on every request: the base URL is validated at construction,
        # but this keeps a mutated attribute from becoming an exfiltration path.
        require_loopback_url(url)
        body = (
            json.dumps(payload, separators=(",", ":")).encode()
            if payload is not None
            else None
        )
        status, raw = self._transport(method, url, body, self.timeout)
        if status == 404:
            raise DecisionError(f"local model endpoint has no {path} route")
        if status >= 400:
            raise DecisionError(
                f"local model endpoint returned HTTP {status} for {path}"
            )
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DecisionError(
                f"local model endpoint returned non-JSON for {path}"
            ) from None

    # -- reads -------------------------------------------------------------

    def version(self) -> str:
        payload = self._call("GET", "/api/version")
        if not isinstance(payload, dict):
            raise DecisionError("local model version response is not an object")
        return str(payload.get("version") or "")

    def installed_models(self) -> list[dict[str, Any]]:
        payload = self._call("GET", "/api/tags")
        if not isinstance(payload, dict) or not isinstance(
            payload.get("models"), list
        ):
            raise DecisionError("local model tag listing has an unexpected shape")
        return [row for row in payload["models"] if isinstance(row, dict)]

    def show(self) -> dict[str, Any]:
        payload = self._call("POST", "/api/show", {"model": self.model})
        if not isinstance(payload, dict):
            raise DecisionError("local model show response is not an object")
        return payload

    def health(self) -> OllamaHealth:
        """Probe reachability and model availability without raising."""
        try:
            version = self.version()
            rows = self.installed_models()
        except DecisionError as exc:
            return OllamaHealth(
                base_url=self.base_url,
                reachable=False,
                model=self.model,
                detail=str(exc),
            )
        names = tuple(sorted(str(row.get("name") or "") for row in rows))
        # ``ollama pull qwen3:30b-a3b`` stores that exact tag; ``ollama pull
        # qwen3`` stores ``qwen3:latest``. Nothing looser is accepted, because a
        # near-miss like ``qwen3:30b-a3b-instruct`` is a different model whose
        # decisions would be sealed under the wrong identity.
        accepted = {self.model, f"{self.model}:latest"}
        match = next(
            (row for row in rows if str(row.get("name") or "") in accepted), None
        )
        details = (match or {}).get("details")
        details = details if isinstance(details, dict) else {}
        return OllamaHealth(
            base_url=self.base_url,
            reachable=True,
            version=version,
            installed_models=names,
            model=self.model,
            model_present=match is not None,
            model_digest=str((match or {}).get("digest") or ""),
            parameter_size=str(details.get("parameter_size") or ""),
            quantization=str(details.get("quantization_level") or ""),
            detail="" if match is not None else "model is not installed",
        )

    def model_fingerprint(self) -> dict[str, str]:
        """The sealed identity of the model that produced a decision.

        A category suggestion is only auditable if the exact model weights that
        produced it are named. The digest comes from the server's own manifest,
        so re-pulling a moved tag changes the fingerprint and invalidates every
        cached decision that cited it.
        """
        health = self.health()
        if not health.reachable:
            raise DecisionError(f"local model endpoint is unavailable: {health.detail}")
        if not health.model_present:
            raise DecisionError(
                f"local model {self.model} is not installed on the endpoint"
            )
        material = {
            "model": self.model,
            "digest": health.model_digest,
            "parameterSize": health.parameter_size,
            "quantization": health.quantization,
            "serverVersion": health.version,
        }
        return {**material, "fingerprint": plan_fingerprint(material)}

    # -- structured generation ---------------------------------------------

    def chat_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        options: dict[str, Any] | None = None,
    ) -> str:
        """Ask one question and return the raw JSON string the model produced.

        ``format`` carries the JSON schema, which is Ollama's structured-output
        contract: the server constrains decoding to the schema instead of hoping
        a prompt is obeyed. The response is still validated by the caller --
        schema-constrained decoding limits shape, not truthfulness.
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": schema,
            "think": False,
            "options": {
                "temperature": self.temperature,
                "seed": self.seed,
                "num_ctx": self.num_ctx,
                **(options or {}),
            },
        }
        response = self._call("POST", "/api/chat", payload)
        if not isinstance(response, dict):
            raise DecisionError("local model chat response is not an object")
        message = response.get("message")
        if not isinstance(message, dict):
            raise DecisionError("local model chat response has no message")
        return str(message.get("content") or "")
