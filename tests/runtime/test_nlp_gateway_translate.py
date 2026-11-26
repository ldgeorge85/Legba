# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""translation on the model gateway's chat model.

The gateway has no translate route; :meth:`GatewayNlpClient.translate` asks the
``fast`` chat alias instead, behind the legacy ``/translate`` contract
(``{"translated", "source_lang", "target_lang", "ms"}``). These tests pin the
request (one completion, strict system prompt, temperature 1.0, no
``max_tokens``), the answer cleaning, the garbage guards, the chunking, the
retry path, the ``supports_translate`` switch, the descriptor, and the
translation backfill sweep stamping ``title_en`` / ``text_en`` through the REAL
handler and wiring over ``httpx.MockTransport``. No network, no container.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest
import yaml

from legba.data.schemas.stack import NLPService, NLPServiceConfig
from legba.data.stack.nlp_service import (
    GatewayNlpClient,
    NlpServiceUnavailable,
    NlpServiceUnsupported,
)
from legba.data.stack.nlp_service.gateway import DEFAULT_TRANSLATE_MODEL
from legba.data.stack.nlp_service.gateway_translate import (
    DEFAULT_CHUNK_CHARS,
    clean_translation,
    split_for_translation,
    system_prompt,
)
from legba.runtime.nlp_client_factory import build_nlp_client_from_stack_component
from legba.runtime.registry_client import RegistryHTTPClient
from tests._gateway_env import GATEWAY_ENV

_REPO = Path(__file__).resolve().parents[2]
_YAML = _REPO / "descriptors" / "stack_component_nlp_gateway_bearer.yaml"
_COMPONENT_ID = "nlp.gateway.bearer"
_BASE = "https://gw.test/v1"
_TOKEN = "tok-test"
_RU = "Путин заявил сегодня, что переговоры продолжатся."
_EN = "Putin said today that the negotiations will continue."


def _completion(content: Any, *, served: str = "core-chat-model") -> httpx.Response:
    return httpx.Response(
        200,
        headers={"x-served-model": served},
        json={
            "id": "c1", "model": "fast",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 12, "total_tokens": 52},
        },
    )


class _Gateway:
    """A scripted gateway: one queued response per call, every request kept."""

    def __init__(self, *responses: httpx.Response | Callable[[httpx.Request], httpx.Response]):
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nxt = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return nxt(request) if callable(nxt) else nxt

    def body(self, i: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[i].content)


def _client(gw: _Gateway, **kw: Any) -> tuple[GatewayNlpClient, list[float]]:
    slept: list[float] = []

    async def _sleep(s: float) -> None:
        slept.append(s)

    kw.setdefault("translate_model", "fast")
    inner = httpx.AsyncClient(transport=httpx.MockTransport(gw), base_url=_BASE)
    return GatewayNlpClient(
        endpoint=_BASE, api_key=_TOKEN, client=inner, sleep=_sleep, **kw,
    ), slept


# ---------------------------------------------------------------------------
# The request and the legacy answer shape
# ---------------------------------------------------------------------------


async def test_a_plain_translation_is_one_completion_in_the_legacy_shape():
    gw = _Gateway(_completion(_EN))
    client, _ = _client(gw)
    out = await client.translate(_RU, source_lang="ru", target_lang="en")

    assert set(out) == {"translated", "source_lang", "target_lang", "ms"}
    assert out["translated"] == _EN
    assert (out["source_lang"], out["target_lang"]) == ("ru", "en")
    assert isinstance(out["ms"], float)

    assert len(gw.requests) == 1
    req = gw.requests[0]
    assert req.method == "POST" and req.url.path == "/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {_TOKEN}"
    body = gw.body()
    assert body["model"] == "fast"
    assert body["temperature"] == 1.0
    assert "max_tokens" not in body and "max_completion_tokens" not in body
    system, user = body["messages"]
    assert system == {"role": "system", "content": system_prompt("ru", "en")}
    assert system["content"] == (
        "Translate the following Russian text into English. Output ONLY the "
        "translation, no preamble, no notes, preserve names, numbers, dates and "
        "paragraph breaks; if the text is already English return it unchanged."
    )
    assert user == {"role": "user", "content": _RU}


async def test_the_positional_legacy_call_shape_works():
    gw = _Gateway(_completion(_EN))
    client, _ = _client(gw)
    out = await client.translate(_RU, "ru")
    assert out["translated"] == _EN and out["target_lang"] == "en"


def test_the_prompt_names_an_unknown_or_missing_source_plainly():
    assert system_prompt(None).startswith("Translate the following text into English.")
    assert system_prompt("und").startswith("Translate the following text into English.")
    assert system_prompt("sw").startswith(
        "Translate the following text (language code 'sw') into English.")
    assert system_prompt("uk-UA").startswith("Translate the following Ukrainian text")


async def test_an_unknown_source_is_echoed_und_and_never_detected():
    gw = _Gateway(_completion(_EN))
    client, _ = _client(gw)
    out = await client.translate(_RU, source_lang=None)
    assert out["source_lang"] == "und"
    assert "detect" not in gw.body()["messages"][0]["content"].lower()


# ---------------------------------------------------------------------------
# Already-English
# ---------------------------------------------------------------------------


async def test_already_english_comes_back_unchanged():
    gw = _Gateway(_completion(_EN))
    client, _ = _client(gw)
    out = await client.translate(_EN, source_lang=None)
    assert out["translated"] == _EN
    assert len(gw.requests) == 1


async def test_a_source_equal_to_the_target_is_returned_without_a_call():
    gw = _Gateway(_completion("never asked"))
    client, _ = _client(gw)
    out = await client.translate(_EN, source_lang="en-GB")
    assert out["translated"] == _EN and out["source_lang"] == "en-GB"
    assert gw.requests == []


# ---------------------------------------------------------------------------
# Cleaning and the garbage guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("answer", [
    f"```\n{_EN}\n```",
    f"```text\n{_EN}\n```",
    f'"{_EN}"',
    f"“{_EN}”",
    f"«{_EN}»",
    f"Translation: {_EN}",
    f"<think></think>\n{_EN}",
    f"  {_EN}\n",
    [{"type": "text", "text": _EN}],
])
async def test_fenced_quoted_or_labelled_answers_are_cleaned(answer):
    gw = _Gateway(_completion(answer))
    client, _ = _client(gw)
    out = await client.translate(_RU, source_lang="ru")
    assert out["translated"] == _EN


def test_a_quoted_source_keeps_its_quotes():
    src = "«Мы не отступим», — заявил министр"
    assert clean_translation('"We will not retreat," the minister said', src) == (
        '"We will not retreat," the minister said')
    assert clean_translation('"We will not retreat"', "«Мы не отступим»") == (
        '"We will not retreat"')


@pytest.mark.parametrize("answer", ["", "   ", "```\n```", '""', None])
async def test_an_empty_answer_is_unavailable(answer):
    gw = _Gateway(_completion(answer))
    client, _ = _client(gw)
    with pytest.raises(NlpServiceUnavailable, match="empty") as exc:
        await client.translate(_RU, source_lang="ru")
    assert _RU not in str(exc.value)


async def test_a_completion_without_choices_is_unavailable():
    gw = _Gateway(httpx.Response(200, json={"choices": []}))
    client, _ = _client(gw)
    with pytest.raises(NlpServiceUnavailable, match="empty or unreadable"):
        await client.translate(_RU, source_lang="ru")


async def test_an_oversized_answer_is_unavailable_never_stamped():
    rambling = _EN + " Note: " + ("this is additional commentary. " * 20)
    gw = _Gateway(_completion(rambling))
    client, _ = _client(gw)
    with pytest.raises(NlpServiceUnavailable, match="over 3x") as exc:
        await client.translate(_RU, source_lang="ru")
    assert _RU not in str(exc.value) and "commentary" not in str(exc.value)


async def test_a_short_cjk_title_is_not_refused_by_the_size_bound():
    # 8 CJK characters (24 bytes) → a 30-character English title: 3.75x by
    # characters, 1.25x by bytes — an honest answer, kept.
    gw = _Gateway(_completion("Putin delivered a speech today"))
    client, _ = _client(gw)
    out = await client.translate("普京今天发表讲话", source_lang="zh")
    assert out["translated"] == "Putin delivered a speech today"


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def test_split_keeps_the_separators_and_reassembles_exactly():
    paras = [f"Paragraph {i} " + ("слово " * 30).strip() for i in range(6)]
    text = "\n\n".join(paras[:3]) + "\n\n\n" + "\n\n".join(paras[3:])
    chunks, joins = split_for_translation(text, 400)
    assert len(chunks) > 1 and len(joins) == len(chunks) - 1
    assert all(len(c) <= 400 for c in chunks)
    rebuilt = chunks[0] + "".join(j + c for j, c in zip(joins, chunks[1:]))
    assert rebuilt == text


def test_split_falls_back_to_lines_sentences_then_a_hard_cut():
    lines = "\n".join("Строка номер %d." % i for i in range(50))
    chunks, joins = split_for_translation(lines, 100)
    assert all(len(c) <= 100 for c in chunks) and set(joins) == {"\n"}

    sentences = " ".join("Предложение номер %d." % i for i in range(40))
    chunks, joins = split_for_translation(sentences, 120)
    assert all(len(c) <= 120 for c in chunks) and set(joins) == {" "}

    blob = "x" * 250
    chunks, joins = split_for_translation(blob, 100)
    assert chunks == ["x" * 100, "x" * 100, "x" * 50] and joins == ["", ""]
    assert split_for_translation("short", DEFAULT_CHUNK_CHARS) == (["short"], [])


async def test_long_text_is_chunked_and_joined_in_order():
    paras = [f"Абзац {i}. " + ("текст " * 40).strip() for i in range(5)]
    text = "\n\n".join(paras)

    def answer(request: httpx.Request) -> httpx.Response:
        chunk = json.loads(request.content)["messages"][1]["content"]
        # Translate each paragraph to a marker that names it, preserving the
        # paragraph breaks inside the chunk.
        out = "\n\n".join(
            f"EN[{p.split('.')[0].split()[-1]}]" for p in chunk.split("\n\n")
        )
        return _completion(out)

    gw = _Gateway(answer)
    client, _ = _client(gw, translate_chunk_chars=600)
    out = await client.translate(text, source_lang="ru")
    assert len(gw.requests) > 1, "a text over the chunk ceiling is several calls"
    assert all(
        len(json.loads(r.content)["messages"][1]["content"]) <= 600 for r in gw.requests
    )
    assert out["translated"] == "\n\n".join(f"EN[{i}]" for i in range(5))


async def test_one_bad_chunk_fails_the_whole_translation():
    text = "\n\n".join(("текст " * 80).strip() for _ in range(3))
    gw = _Gateway(_completion("Fine first part."), _completion(""))
    client, _ = _client(gw, translate_chunk_chars=600)
    with pytest.raises(NlpServiceUnavailable):
        await client.translate(text, source_lang="ru")


# ---------------------------------------------------------------------------
# Retry / Retry-After / auth: the client's existing behaviour
# ---------------------------------------------------------------------------


async def test_429_with_retry_after_is_honoured_once_then_answers():
    gw = _Gateway(
        httpx.Response(429, headers={"Retry-After": "3"}, json={"error": {
            "message": "rate limited", "type": "rate_limit"}}),
        _completion(_EN),
    )
    client, slept = _client(gw)
    out = await client.translate(_RU, source_lang="ru")
    assert out["translated"] == _EN
    assert len(gw.requests) == 2 and slept == [3.0]


async def test_a_chat_error_body_never_carries_the_text_into_the_error():
    gw = _Gateway(httpx.Response(400, json={"error": {
        "message": "bad request", "type": "invalid_request_error"}}))
    client, slept = _client(gw)
    with pytest.raises(NlpServiceUnavailable, match="bad request") as exc:
        await client.translate(_RU, source_lang="ru")
    assert _RU not in str(exc.value) and slept == []


async def test_the_request_text_is_never_logged(caplog):
    gw = _Gateway(_completion(_EN))
    client, _ = _client(gw)
    with caplog.at_level("DEBUG", logger="legba.data.stack.nlp_service.gateway"):
        await client.translate(_RU, source_lang="ru")
    assert caplog.records, "the counts-only debug line is emitted"
    assert all(_RU not in r.getMessage() and _EN not in r.getMessage()
               for r in caplog.records)
    assert "served_model=core-chat-model" in caplog.text
    assert "completion_tokens=12" in caplog.text


# ---------------------------------------------------------------------------
# supports_translate, schema, descriptor, factory
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model, expected", [
    ("fast", True), ("deep", True), (None, False), ("", False), ("  ", False),
])
async def test_supports_translate_follows_the_configured_model(model, expected):
    gw = _Gateway(_completion(_EN))
    client, _ = _client(gw, translate_model=model)
    assert client.supports_translate is expected
    if not expected:
        with pytest.raises(NlpServiceUnsupported):
            await client.translate(_RU, source_lang="ru")
        assert gw.requests == []


def test_the_descriptor_validates_with_translate_model():
    model = NLPService.model_validate(yaml.safe_load(_YAML.read_text()))
    assert model.config.translate_model is not None
    # The chat model is deployment data: the descriptor names the variable.
    assert model.config.translate_model.raw == "${env:LEGBA_GATEWAY_TRANSLATE_MODEL}"
    assert "no translation" not in model.name


def test_translate_model_is_refused_on_the_legacy_provider():
    t = lambda s: {"factory_kind": "text", "raw": s}  # noqa: E731
    cfg = {
        "endpoint": t("https://nlp.example.internal"),
        "translate_model": t("fast"),
    }
    with pytest.raises(ValueError, match="translate_model is for provider 'bearer_gateway'"):
        NLPServiceConfig.model_validate(cfg)
    cfg.pop("translate_model")
    assert NLPServiceConfig.model_validate(cfg).translate_model is None


def _registry(body: dict[str, Any]) -> RegistryHTTPClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": _COMPONENT_ID, "version": "0" * 16, "body": body})

    inner = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://registry.test")
    return RegistryHTTPClient(base_url="http://registry.test", token=None, client=inner)


@pytest.mark.parametrize("mutate, expected", [
    (lambda c: None, GATEWAY_ENV["LEGBA_GATEWAY_TRANSLATE_MODEL"]),
    (lambda c: c.pop("translate_model"), DEFAULT_TRANSLATE_MODEL),
    (lambda c: c.__setitem__("translate_model", {"factory_kind": "text", "raw": "deep"}), "deep"),
    (lambda c: c.__setitem__("translate_model", {"factory_kind": "text", "raw": ""}), None),
])
async def test_the_factory_applies_the_translate_model(mutate, expected):
    body = yaml.safe_load(_YAML.read_text())
    mutate(body["config"])

    async def secrets(secret_id: str) -> bytes:
        return _TOKEN.encode()

    client = await build_nlp_client_from_stack_component(
        _COMPONENT_ID, registry_client=_registry(body), secrets_resolve=secrets,
    )
    assert isinstance(client, GatewayNlpClient)
    assert client.translate_model == expected
    assert client.supports_translate is (expected is not None)


# ---------------------------------------------------------------------------
# The translation backfill sweep, end to end over the mock transport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Deps:
    """The two StandardDeps fields the sweep and its wiring read."""

    pg_pool: Any
    extras: dict[str, Any] = field(default_factory=dict)


class _Conn:
    def __init__(self, pool: "_Pool") -> None:
        self._pool = pool

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self._pool.selects.append(args)
        return list(self._pool.rows)

    async def execute(self, sql: str, *args: Any) -> str:
        self._pool.writes.append((sql, args))
        return "UPDATE 1"


class _Acquire:
    def __init__(self, pool: "_Pool") -> None:
        self._pool = pool

    async def __aenter__(self) -> _Conn:
        return _Conn(self._pool)

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Pool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.selects: list[tuple[Any, ...]] = []
        self.writes: list[tuple[str, tuple[Any, ...]]] = []

    def acquire(self) -> _Acquire:
        return _Acquire(self)


async def test_the_translation_sweep_stamps_title_en_and_text_en_through_the_gateway():
    from legba.data.analysts.deterministic_handlers import reenrich_translation
    from legba.runtime.analyst_deps_builder import _wire_reenrich_translation

    title_ru = "Путин заявил о переговорах"
    summary_ru = "Переговоры продолжатся на следующей неделе."
    translations = {
        title_ru: "Putin spoke about negotiations",
        f"{title_ru}\n{summary_ru}": (
            "Putin spoke about negotiations\nThe negotiations will continue next week."),
    }

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["messages"][0]["content"] == system_prompt("ru", "en")
        return _completion(translations[body["messages"][1]["content"]])

    gw = _Gateway(answer)
    client, _ = _client(gw)
    pool = _Pool([{
        "id": "sig-1", "language": "ru",
        "payload": {"title": title_ru, "summary": summary_ru, "language": "ru"},
    }])
    deps = _Deps(pg_pool=pool)
    descriptor = SimpleNamespace(identity=SimpleNamespace(id="analyst.reenrich_translation"))

    wired = await _wire_reenrich_translation(descriptor, deps, nlp_client=client)
    assert wired is not deps, "a gateway client with a translate_model is wired"
    assert wired.extras[reenrich_translation.NLP_DEPS_EXTRA_KEY] is client

    result = await reenrich_translation.handle([], {"translate_languages": ["ru"]}, wired)
    data = result.finding.data
    assert (data["examined"], data["titles_stamped"], data["texts_stamped"],
            data["failures"], data["skipped_no_nlp"]) == (1, 1, 1, 0, 0)
    assert [r.url.path for r in gw.requests] == ["/v1/chat/completions"] * 2
    (sql, args), = pool.writes
    assert "text_en" in sql
    assert args == ("sig-1", json.dumps("Putin spoke about negotiations"),
                    json.dumps(translations[f"{title_ru}\n{summary_ru}"]))


async def test_the_sweep_leaves_a_row_unstamped_on_a_refused_answer():
    from legba.data.analysts.deterministic_handlers import reenrich_translation

    gw = _Gateway(_completion(""))
    client, _ = _client(gw)
    pool = _Pool([{"id": "sig-2", "language": "ru",
                   "payload": {"title": "Путин", "language": "ru"}}])
    deps = _Deps(pg_pool=pool, extras={reenrich_translation.NLP_DEPS_EXTRA_KEY: client})
    result = await reenrich_translation.handle([], {"translate_languages": ["ru"]}, deps)
    assert result.finding.data["failures"] == 1
    assert result.finding.data["titles_stamped"] == 0
    assert pool.writes == [], "garbage is never stamped; the row stays a candidate"


@pytest.fixture(autouse=True)
def _gateway_env(monkeypatch):
    """The gateway descriptors name their endpoint and models as ${env:NAME}."""
    from tests._gateway_env import set_gateway_env

    set_gateway_env(monkeypatch)
