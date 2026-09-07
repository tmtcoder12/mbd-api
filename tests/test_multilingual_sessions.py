import pytest

from mbd_api.core import build_query_cache_key, normalize_session_language


@pytest.mark.parametrize("language", ["cmn", "eng", "fra", "hin", "jpn", "kor", "spa"])
def test_supported_session_languages(language: str) -> None:
    assert normalize_session_language(language.upper()) == language


@pytest.mark.parametrize("language", ["", "en", "deu", "english", "123"])
def test_unsupported_session_languages_are_rejected(language: str) -> None:
    with pytest.raises(ValueError, match="language"):
        normalize_session_language(language)


def test_current_exact_cache_isolated_by_language_prompt() -> None:
    english = build_query_cache_key(
        "qcache:v1",
        "11111111-1111-4111-8111-555555555555",
        "What time do you close?",
        "Respond in eng.",
        8,
        0.0,
    )
    french = build_query_cache_key(
        "qcache:v1",
        "11111111-1111-4111-8111-555555555555",
        "What time do you close?",
        "Respond in fra.",
        8,
        0.0,
    )
    assert english != french
