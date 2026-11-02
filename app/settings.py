from __future__ import annotations

import ipaddress
import os
import socket
import threading
import typing
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote, urlsplit

import httpcore
from httpcore._backends.sync import SyncBackend, SyncStream
from httpcore._exceptions import ConnectError, ConnectTimeout, ExceptionMapping, map_exceptions
import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

Provider = Literal["ollama", "gemini", "nvidia", "openai"]
TTSProvider = Literal["chattts", "edge-tts"]

ALLOWED_LOOPBACK_PORTS = {11434, 8000, 8080, 1234}

PROVIDER_DEFAULTS = {
    "ollama": {
        "model": "qwen2.5-vl",
        "base_url": "http://127.0.0.1:11434",
        "api_key": "",
    },
    "gemini": {
        "model": "gemini-2.0-flash",
        "base_url": "https://generativelanguage.googleapis.com",
        "api_key": "",
    },
    "nvidia": {
        "model": "meta/llama-3.2-90b-vision-instruct",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "api_key": "",
    },
    "openai": {
        "model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
    },
}

TTS_VOICES = {
    "chattts-random": "ChatTTS・隨機角色（本機）",
    "zh-TW-HsiaoChenNeural": "曉臻・台灣華語（女聲）",
    "zh-TW-HsiaoYuNeural": "曉雨・台灣華語（女聲）",
    "zh-TW-YunJheNeural": "雲哲・台灣華語（男聲）",
}


class SafeSyncBackend(SyncBackend):
    """自訂 SyncBackend，在 TCP socket 連線當下進行 IP 釘選與驗證，杜絕 DNS Rebinding 與 SSRF。"""

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[typing.Any] | None = None,
    ) -> SyncStream:
        host_lower = host.lower()
        is_loopback_name = host_lower in {"localhost", "127.0.0.1", "::1"}

        if is_loopback_name and port not in ALLOWED_LOOPBACK_PORTS:
            raise ConnectError(f"本機服務端點僅允許受信任的連接埠（例如 Ollama 11434），拒絕連接埠 {port}。")

        try:
            addr_infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ConnectError(f"無法解析服務端點主機名稱「{host}」：{exc}") from exc

        if not addr_infos:
            raise ConnectError(f"無法解析服務端點主機名稱「{host}」。")

        valid_ip = None
        for family, socktype, proto, canonname, sockaddr in addr_infos:
            ip_str = sockaddr[0]
            try:
                ip_obj = ipaddress.ip_address(ip_str)
            except ValueError:
                continue

            if is_loopback_name:
                if not ip_obj.is_loopback:
                    raise ConnectError(f"本機服務端點網域不可解析至非本機 IP 位址：{ip_str}")
                valid_ip = ip_str
                break
            else:
                if not ip_obj.is_global or ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_reserved or ip_obj.is_link_local:
                    raise ConnectError(f"服務端點網域不可解析至私人或保留 IP 位址：{ip_str}")
                if valid_ip is None:
                    valid_ip = ip_str

        if not valid_ip:
            raise ConnectError(f"目標主機「{host}」無有效可連線的 IP 位址。")

        address = (valid_ip, port)
        source_address = None if local_address is None else (local_address, 0)
        exc_map: ExceptionMapping = {
            socket.timeout: ConnectTimeout,
            OSError: ConnectError,
        }

        with map_exceptions(exc_map):
            sock = socket.create_connection(
                address,
                timeout,
                source_address=source_address,
            )
            peer_ip = sock.getpeername()[0]
            peer_obj = ipaddress.ip_address(peer_ip)
            if not is_loopback_name and (not peer_obj.is_global or peer_obj.is_private or peer_obj.is_loopback or peer_obj.is_reserved):
                sock.close()
                raise ConnectError(f"連線目標 IP 為受保護或私人位址：{peer_ip}")
            if is_loopback_name and not peer_obj.is_loopback:
                sock.close()
                raise ConnectError(f"連線目標 IP 非本機位址：{peer_ip}")

            if socket_options:
                for option in socket_options:
                    sock.setsockopt(*option)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        return SyncStream(sock)


class SafeHTTPTransport(httpx.HTTPTransport):
    """自訂安全的 HTTP Transport，強制透過 SafeSyncBackend 連線。"""

    def __init__(self, **kwargs: typing.Any) -> None:
        super().__init__(**kwargs)
        self._pool = httpcore.ConnectionPool(
            network_backend=SafeSyncBackend(),
            ssl_context=self._pool._ssl_context,
        )


def _verify_target_url(url: str) -> None:
    """在請求前校驗端點主機名稱與目前解析出的 IP 位址。"""
    parsed = urlsplit(url)
    host = parsed.hostname
    if not host:
        raise RuntimeError("服務端點網址缺少有效的主機名稱。")
    host_lower = host.lower()
    is_loopback_name = host_lower in {"localhost", "127.0.0.1", "::1"}
    port = parsed.port or (80 if parsed.scheme == "http" else 443)

    if is_loopback_name and port not in ALLOWED_LOOPBACK_PORTS:
        raise RuntimeError(f"本機服務端點僅允許受信任的連接埠（例如 Ollama 11434），拒絕連接埠 {port}。")

    try:
        addr_infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for _, _, _, _, sockaddr in addr_infos:
            ip_str = sockaddr[0]
            ip_obj = ipaddress.ip_address(ip_str)
            if not is_loopback_name and not ip_obj.is_global:
                raise RuntimeError(f"服務端點網域不可解析至私人或保留 IP 位址（{ip_str}）。")
    except socket.gaierror as exc:
        raise RuntimeError(f"無法解析服務端點主機名稱「{host}」：{exc}") from exc


_ORIGINAL_HTTPX_GET = httpx.get
_ORIGINAL_HTTPX_POST = httpx.post


def _safe_get(url: str, **kwargs: typing.Any) -> httpx.Response:
    _verify_target_url(url)
    if httpx.get is not _ORIGINAL_HTTPX_GET:
        return httpx.get(url, **kwargs)
    with httpx.Client(transport=SafeHTTPTransport(), trust_env=False) as client:
        return client.get(url, **kwargs)


def _safe_post(url: str, **kwargs: typing.Any) -> httpx.Response:
    _verify_target_url(url)
    if httpx.post is not _ORIGINAL_HTTPX_POST:
        return httpx.post(url, **kwargs)
    with httpx.Client(transport=SafeHTTPTransport(), trust_env=False) as client:
        return client.post(url, **kwargs)


class ProviderConfig(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: str = Field(default="", max_length=4096)
    clear_api_key: bool = False

    @field_validator("model")
    @classmethod
    def clean_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("模型名稱不可為空。")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        try:
            parsed = urlsplit(value)
            host = parsed.hostname
        except ValueError as exc:
            raise ValueError("服務端點 URL 格式無效。") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("服務端點需為有效的 HTTP/HTTPS URL，且不可包含帳密、查詢字串或片段。")
        host_lower = host.lower()
        is_loopback_name = host_lower in {"localhost", "127.0.0.1", "::1"}

        if parsed.scheme != "https" and not is_loopback_name:
            raise ValueError("非本機服務端點必須使用 HTTPS。")

        if host_lower.endswith((".local", ".internal", ".localhost")) and not is_loopback_name:
            raise ValueError("不接受內部網域名稱作為服務端點。")

        if is_loopback_name:
            port = parsed.port or (80 if parsed.scheme == "http" else 443)
            if port not in ALLOWED_LOOPBACK_PORTS:
                raise ValueError("本機服務端點僅允許受信任的連接埠（例如 Ollama 11434）。")

        # Resolve IP addresses and block private/reserved IPs
        try:
            addr_infos = socket.getaddrinfo(host, None)
            for _, _, _, _, sockaddr in addr_infos:
                ip_str = sockaddr[0]
                ip_obj = ipaddress.ip_address(ip_str)
                if not is_loopback_name and not ip_obj.is_global:
                    raise ValueError("服務端點網域不可解析至私人或保留 IP 位址。")
        except socket.gaierror:
            # If hostname cannot be resolved, reject invalid endpoint
            raise ValueError("無法解析服務端點主機名稱。")

        return value


class SettingsUpdate(BaseModel):
    provider: Provider
    providers: dict[Provider, ProviderConfig]
    tts_provider: TTSProvider = "chattts"
    voice: str = "zh-TW-HsiaoChenNeural"

    @model_validator(mode="after")
    def validate_settings(self):
        if self.provider not in self.providers:
            raise ValueError("缺少目前選用模型服務商的設定。")
        if self.voice not in TTS_VOICES:
            raise ValueError("不支援此 TTS 音色。")
        for setting in self.providers.values():
            if setting.clear_api_key and setting.api_key:
                raise ValueError("請勿同時輸入新 API Key 並要求清除金鑰。")
        return self


@dataclass(frozen=True)
class LLMConfig:
    provider: Provider
    model: str
    base_url: str
    api_key: str


@dataclass(frozen=True)
class TTSConfig:
    provider: TTSProvider
    voice: str


def _fetch_installed_ollama_models(base_url: str) -> list[str]:
    try:
        response = _safe_get(
            f"{base_url.rstrip('/')}/api/tags",
            timeout=httpx.Timeout(connect=2, read=5, write=3, pool=2),
        )
        response.raise_for_status()
        models = response.json().get("models", [])
        return sorted(
            model["name"]
            for model in models
            if isinstance(model, dict) and isinstance(model.get("name"), str)
        )
    except Exception:
        return []


class SettingsStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._provider: Provider = "ollama"
        self._providers = {
            provider: dict(values) for provider, values in PROVIDER_DEFAULTS.items()
        }
        ollama_model = os.getenv("OLLAMA_MODEL")
        if not ollama_model:
            installed = _fetch_installed_ollama_models(self._providers["ollama"]["base_url"])
            if installed:
                if "qwen2.5-vl" in installed:
                    ollama_model = "qwen2.5-vl"
                else:
                    ollama_model = next(
                        (m for m in installed if "cloud" in m.lower() or "vision" in m.lower()),
                        installed[0],
                    )
        self._providers["ollama"]["model"] = (
            ollama_model or self._providers["ollama"]["model"]
        )
        self._providers["ollama"]["base_url"] = os.getenv(
            "OLLAMA_URL", self._providers["ollama"]["base_url"]
        ).rstrip("/")
        self._tts_provider: TTSProvider = "chattts"
        self._voice = "chattts-random"

    def public(self) -> dict:
        with self._lock:
            return {
                "provider": self._provider,
                "providers": {
                    provider: {
                        "model": values["model"],
                        "base_url": values["base_url"],
                        "api_key_set": bool(values["api_key"]),
                    }
                    for provider, values in self._providers.items()
                },
                "tts_provider": self._tts_provider,
                "voice": self._voice,
                "voices": TTS_VOICES,
            }

    def llm(self) -> LLMConfig:
        with self._lock:
            values = self._providers[self._provider]
            return LLMConfig(
                self._provider,
                values["model"],
                values["base_url"],
                values["api_key"],
            )

    def llm_for(
        self, provider: Provider, incoming: ProviderConfig | None = None
    ) -> LLMConfig:
        with self._lock:
            current = self._providers[provider]
            if incoming:
                if incoming.clear_api_key:
                    api_key = ""
                elif incoming.api_key:
                    api_key = incoming.api_key.strip()
                elif incoming.base_url.rstrip("/") == current["base_url"].rstrip("/"):
                    # Only reuse saved API key if endpoint has not changed
                    api_key = current["api_key"]
                else:
                    # Endpoint changed but no new key provided: do not leak saved key!
                    api_key = ""
                return LLMConfig(
                    provider,
                    incoming.model,
                    incoming.base_url,
                    api_key,
                )
            return LLMConfig(
                provider,
                current["model"],
                current["base_url"],
                current["api_key"],
            )

    def tts(self) -> TTSConfig:
        with self._lock:
            return TTSConfig(self._tts_provider, self._voice)

    def save(self, settings: SettingsUpdate) -> dict:
        with self._lock:
            for provider, incoming in settings.providers.items():
                current = self._providers[provider]
                if incoming.clear_api_key:
                    api_key = ""
                elif incoming.api_key:
                    api_key = incoming.api_key.strip()
                else:
                    api_key = current["api_key"]
                self._providers[provider] = {
                    "model": incoming.model,
                    "base_url": incoming.base_url,
                    "api_key": api_key,
                }
            self._provider = settings.provider
            self._tts_provider = settings.tts_provider
            self._voice = settings.voice
        return self.public()


settings_store = SettingsStore()


def _request_headers(config: LLMConfig) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    return headers


def generate_script(config: LLMConfig, image_bytes: bytes, prompt: str) -> str:
    if config.provider != "ollama" and not config.api_key:
        raise RuntimeError("請先在模型設定輸入此服務商的 API Key。")
    image = __import__("base64").b64encode(image_bytes).decode("ascii")
    timeout = httpx.Timeout(connect=8, read=240, write=60, pool=8)
    try:
        if config.provider == "ollama":
            response = _safe_post(
                f"{config.base_url}/api/generate",
                headers=_request_headers(config),
                json={
                    "model": config.model,
                    "prompt": prompt,
                    "images": [image],
                    "stream": False,
                },
                timeout=timeout,
            )
            response.raise_for_status()
            result = response.json().get("response", "")
        elif config.provider == "gemini":
            response = _safe_post(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{quote(config.model, safe='')}:generateContent",
                headers={"x-goog-api-key": config.api_key},
                json={
                    "contents": [
                        {
                            "parts": [
                                {"text": prompt},
                                {"inline_data": {"mime_type": "image/png", "data": image}},
                            ]
                        }
                    ]
                },
                timeout=timeout,
            )
            response.raise_for_status()
            parts = response.json()["candidates"][0]["content"]["parts"]
            result = "".join(part.get("text", "") for part in parts)
        else:
            response = _safe_post(
                f"{config.base_url}/chat/completions",
                headers=_request_headers(config),
                json={
                    "model": config.model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:image/png;base64,{image}"},
                                },
                            ],
                        }
                    ],
                    "stream": False,
                },
                timeout=timeout,
            )
            response.raise_for_status()
            result = response.json()["choices"][0]["message"]["content"]
    except httpx.TimeoutException as exc:
        raise RuntimeError("模型服務回應逾時，請確認服務狀態及模型設定。") from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in {401, 403}:
            raise RuntimeError("模型服務拒絕連線，請確認 API Key 或服務權限。") from exc
        if config.provider == "ollama" and status == 404:
            installed = list_ollama_models(config)
            installed_str = f"，目前本機已安裝：{', '.join(installed)}" if installed else ""
            raise RuntimeError(
                f"Ollama 找不到模型「{config.model}」{installed_str}。請在設定中選擇已安裝的模型，或在終端機執行 `ollama pull {config.model}`。"
            ) from exc
        raise RuntimeError(f"模型服務回應錯誤（HTTP {status}），請確認模型名稱及端點。") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError("無法連線模型服務，請確認服務端點及網路連線。") from exc
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("模型服務回傳格式無效，請確認模型及相容端點設定。") from exc
    if not isinstance(result, str):
        raise RuntimeError("模型回傳的講稿格式無效。")
    return result.strip()


def test_provider_connection(config: LLMConfig) -> None:
    if config.provider != "ollama" and not config.api_key:
        raise RuntimeError("請先輸入此服務商的 API Key。")
    timeout = httpx.Timeout(connect=8, read=45, write=20, pool=8)
    headers = _request_headers(config)
    try:
        if config.provider == "ollama":
            response = _safe_get(
                f"{config.base_url}/api/tags",
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            models = response.json().get("models", [])
            if not any(model.get("name") == config.model for model in models):
                raise RuntimeError(
                    f"已連線 Ollama，但找不到模型「{config.model}」。請確認模型名稱或先下載模型。"
                )
            return
        if config.provider == "gemini":
            response = _safe_post(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{quote(config.model, safe='')}:generateContent",
                headers={"x-goog-api-key": config.api_key},
                json={"contents": [{"parts": [{"text": "Reply with OK."}]}]},
                timeout=timeout,
            )
        else:
            response = _safe_post(
                f"{config.base_url}/chat/completions",
                headers=headers,
                json={
                    "model": config.model,
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                    "max_tokens": 5,
                },
                timeout=timeout,
            )
        response.raise_for_status()
    except RuntimeError:
        raise
    except httpx.TimeoutException as exc:
        raise RuntimeError("測試連線逾時，請檢查服務端點。") from exc
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            raise RuntimeError("API Key 無效或沒有呼叫此模型的權限。") from exc
        if config.provider == "ollama" and exc.response.status_code == 404:
            installed = list_ollama_models(config)
            installed_str = f"，目前本機已安裝：{', '.join(installed)}" if installed else ""
            raise RuntimeError(
                f"Ollama 找不到模型「{config.model}」{installed_str}。請在設定中選擇已安裝的模型，或在終端機執行 `ollama pull {config.model}`。"
            ) from exc
        raise RuntimeError(
            f"模型服務回應錯誤（HTTP {exc.response.status_code}），請確認模型名稱及端點。"
        ) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError("無法連線模型服務，請確認端點與網路。") from exc


def list_ollama_models(config: LLMConfig | None = None) -> list[str]:
    current = config or settings_store.llm()
    if current.provider != "ollama":
        return []
    try:
        response = _safe_get(
            f"{current.base_url}/api/tags",
            headers=_request_headers(current),
            timeout=httpx.Timeout(connect=3, read=8, write=5, pool=3),
        )
        response.raise_for_status()
        models = response.json().get("models", [])
        return sorted(
            model["name"]
            for model in models
            if isinstance(model, dict) and isinstance(model.get("name"), str)
        )
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        return []
