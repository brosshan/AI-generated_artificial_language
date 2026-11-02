import httpx
import pytest
from pydantic import ValidationError

from app.settings import (
    PROVIDER_DEFAULTS,
    ProviderConfig,
    SettingsStore,
    SettingsUpdate,
    generate_script,
)


def build_update(provider="ollama", selected_key=""):
    providers = {
        name: ProviderConfig(**{**values, "api_key": ""})
        for name, values in PROVIDER_DEFAULTS.items()
    }
    if selected_key:
        providers[provider] = providers[provider].model_copy(
            update={"api_key": selected_key}
        )
    return SettingsUpdate(
        provider=provider,
        providers=providers,
        tts_provider="chattts",
        voice="chattts-random",
    )


def test_settings_api_never_returns_api_key():
    store = SettingsStore()
    store.save(build_update("openai", "secret-for-test"))
    public = store.public()
    assert public["provider"] == "openai"
    assert public["providers"]["openai"]["api_key_set"] is True
    assert "api_key" not in public["providers"]["openai"]


def test_saving_blank_key_keeps_existing_key():
    store = SettingsStore()
    store.save(build_update("gemini", "secret-for-test"))
    store.save(build_update("gemini"))
    assert store.llm().api_key == "secret-for-test"

def test_api_key_can_be_cleared():
    store = SettingsStore()
    store.save(build_update("openai", "secret-for-test"))
    update = build_update("openai")
    update.providers["openai"] = update.providers["openai"].model_copy(
        update={"clear_api_key": True}
    )
    store.save(update)
    assert store.llm().api_key == ""
    assert store.public()["providers"]["openai"]["api_key_set"] is False


def test_provider_url_requires_https_for_remote_hosts():
    with pytest.raises(ValidationError, match="必須使用 HTTPS"):
        ProviderConfig(
            model="vision-model",
            base_url="http://api.openai.com/v1",
        )
    assert ProviderConfig(
        model="local-model",
        base_url="http://127.0.0.1:11434",
    ).base_url == "http://127.0.0.1:11434"


def test_openai_compatible_request_includes_vision_image(monkeypatch):
    observed = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "這是一份測試講稿。"}}]}

    def fake_post(url, **kwargs):
        observed["url"] = url
        observed.update(kwargs)
        return Response()

    monkeypatch.setattr(httpx, "post", fake_post)
    config = SettingsStore().llm_for(
        "openai",
        ProviderConfig(
            model="vision-model",
            base_url="https://api.openai.com/v1",
            api_key="test-key",
        ),
    )
    result = generate_script(config, b"slide-image", "Describe the slide.")

    assert result == "這是一份測試講稿。"
    assert observed["url"] == "https://api.openai.com/v1/chat/completions"
    assert observed["headers"]["Authorization"] == "Bearer test-key"
    image = observed["json"]["messages"][0]["content"][1]["image_url"]["url"]
    assert image.startswith("data:image/png;base64,")


@pytest.mark.parametrize(
    ("provider", "base_url", "response_body", "expected_url"),
    [
        (
            "gemini",
            "https://generativelanguage.googleapis.com",
            {"candidates": [{"content": {"parts": [{"text": "Gemini 講稿"}]}}]},
            "https://generativelanguage.googleapis.com/v1beta/models/vision-model:generateContent",
        ),
        (
            "nvidia",
            "https://integrate.api.nvidia.com/v1",
            {"choices": [{"message": {"content": "NVIDIA 講稿"}}]},
            "https://integrate.api.nvidia.com/v1/chat/completions",
        ),
    ],
)
def test_cloud_provider_vision_request(monkeypatch, provider, base_url, response_body, expected_url):
    observed = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return response_body

    def fake_post(url, **kwargs):
        observed["url"] = url
        observed.update(kwargs)
        return Response()

    monkeypatch.setattr(httpx, "post", fake_post)
    config = SettingsStore().llm_for(
        provider,
        ProviderConfig(model="vision-model", base_url=base_url, api_key="test-key"),
    )
    result = generate_script(config, b"slide-image", "Describe the slide.")

    assert result.endswith("講稿")
    assert observed["url"] == expected_url
    assert observed["headers"].get("x-goog-api-key", "Bearer test-key") == (
        "test-key" if provider == "gemini" else "Bearer test-key"
    )
