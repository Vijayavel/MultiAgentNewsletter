"""Each of the five agents must call its own provider with its own key."""

from datetime import date

from conftest import WRITERS, BASE_URL_TO_PROVIDER, PROVIDER_KEYS, review_reply, writer_reply

from newsletter.config import PROVIDERS, load_settings
from newsletter.reviewer import review
from newsletter.writers import run_writers


def test_writers_use_their_own_provider_key_endpoint_and_model(fake_llm, articles):
    for p in ("openai", "google", "deepseek", "openrouter"):
        fake_llm.replies[p] = writer_reply()
    run_writers(articles, load_settings(), date(2026, 10, 13))

    assert sorted(fake_llm.providers_called()) == ["deepseek", "google", "openai", "openrouter"]
    for provider, api_key, base_url, model, kwargs in fake_llm.calls:
        assert api_key == PROVIDER_KEYS[PROVIDERS[provider].key_env]
        assert BASE_URL_TO_PROVIDER[base_url] == provider
        assert model == PROVIDERS[provider].default_model
        assert kwargs["response_format"] == {"type": "json_object"}


def test_reviewer_uses_cerebras(fake_llm, articles):
    fake_llm.replies["openai"] = writer_reply()
    fake_llm.replies["cerebras"] = review_reply()
    subs = run_writers(articles, load_settings(), date(2026, 10, 13), writers=WRITERS[:1])
    review(subs, articles, load_settings(), date(2026, 10, 13))

    provider, api_key, *_ = fake_llm.calls[-1]
    assert provider == "cerebras" and api_key == "key-cerebras"


def test_openai_uses_max_completion_tokens_others_max_tokens(fake_llm, articles):
    for p in ("openai", "google", "deepseek", "openrouter"):
        fake_llm.replies[p] = writer_reply()
    run_writers(articles, load_settings(), date(2026, 10, 13))
    for provider, *_, kwargs in fake_llm.calls:
        expected = "max_completion_tokens" if provider == "openai" else "max_tokens"
        assert expected in kwargs


def test_model_and_base_url_are_configurable(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://proxy.example.com/v1")
    assert PROVIDERS["deepseek"].model == "deepseek-v4-pro"
    assert PROVIDERS["openrouter"].base_url == "https://proxy.example.com/v1"


def test_missing_key_fails_that_writer_only(fake_llm, articles, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY")
    for p in ("openai", "deepseek", "openrouter"):
        fake_llm.replies[p] = writer_reply()
    subs = {s.agent: s for s in run_writers(articles, load_settings(), date(2026, 10, 13))}
    assert "GOOGLE_API_KEY is not set" in subs["google"].error
    assert all(subs[a].ok for a in ("openai", "deepseek", "openrouter"))
