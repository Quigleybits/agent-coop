"""Offline contracts for prompt-cache metadata."""

from __future__ import annotations

import hashlib

from agent_coop import coop_autonomous
from agent_coop import coop_prompt_cache


def test_bootstrap_prefix_is_byte_stable_and_versioned():
    first = coop_autonomous.content_free_turn(60)
    second = coop_autonomous.content_free_turn(7_200)

    assert first == second == coop_prompt_cache.PROMPT_PREFIX
    assert coop_prompt_cache.PROMPT_PREFIX_VERSION == "coop-bootstrap-v4"
    assert coop_prompt_cache.prompt_trace_details(
        first,
        provider="codex",
    ) == {
        "prompt_prefix_version": "coop-bootstrap-v4",
        "prompt_prefix_sha256": hashlib.sha256(
            first.encode("utf-8")
        ).hexdigest(),
        "prompt_prefix_bytes": len(first.encode("utf-8")),
        "prompt_cache_mode": "provider_managed",
    }


def test_bootstrap_prefix_is_self_contained_for_foreign_workspaces():
    prefix = coop_prompt_cache.PROMPT_PREFIX

    assert "SKILL.md" not in prefix
    assert "COOP_GUIDE.md" not in prefix
    for required in (
        "next_action.command",
        "required_inputs",
        "reason_code",
        "legal_next_actions",
        "stop boundaries",
        "Never ask `human`",
        "next_action.kind",
    ):
        assert required in prefix


def test_token_efficient_prompt_has_distinct_stable_exit_contract():
    prompt = coop_autonomous.content_free_turn(60, token_efficient=True)

    assert prompt == coop_prompt_cache.TOKEN_EFFICIENT_PROMPT_PREFIX
    assert prompt.startswith(coop_prompt_cache.PROMPT_PREFIX)
    assert "After your final canonical board write succeeds" in prompt
    assert "do not run status, help, inbox, or history" in prompt
    assert "do not produce a narrative summary" in prompt
    assert prompt != coop_autonomous.content_free_turn(60)

    details = coop_prompt_cache.prompt_trace_details(
        prompt,
        provider="codex",
    )
    assert details["prompt_prefix_version"] \
        == coop_prompt_cache.TOKEN_EFFICIENT_PROMPT_PREFIX_VERSION
    assert details["prompt_prefix_sha256"] \
        == coop_prompt_cache.TOKEN_EFFICIENT_PROMPT_PREFIX_SHA256


def test_only_known_prefix_is_fingerprinted_and_claude_hint_is_explicit():
    assert coop_prompt_cache.prompt_trace_details(
        coop_prompt_cache.PROMPT_PREFIX,
        provider="claude",
        provider_hint=True,
    )["prompt_cache_mode"] == "provider_hint"
    assert coop_prompt_cache.prompt_trace_details(
        "task text must not be fingerprinted",
        provider="claude",
        provider_hint=True,
    ) == {"prompt_cache_mode": "unobserved"}


def test_codex_usage_normalizes_complete_weighted_components():
    assert coop_prompt_cache.normalize_codex_usage(
        {
            "inputTokens": 1_200,
            "cachedInputTokens": 900,
            "cacheWriteInputTokens": 64,
            "outputTokens": 33,
            "reasoningOutputTokens": 7,
            "totalTokens": 1_233,
        },
        model_id="gpt-5.4",
        model_calls=2,
    ) == {
        "model_id": "gpt-5.4",
        "input_tokens": 1_200,
        "uncached_input_tokens": 236,
        "cached_input_tokens": 900,
        "cache_write_input_tokens": 64,
        "output_tokens": 33,
        "reasoning_output_tokens": 7,
        "total_tokens": 1_233,
        "model_calls": 2,
        "usage_observation": "complete",
    }


def test_codex_usage_keeps_valid_fields_but_marks_partial():
    usage = coop_prompt_cache.normalize_codex_usage({
        "inputTokens": 100,
        "cachedInputTokens": True,
        "outputTokens": 5,
    })

    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 5
    assert usage["cache_write_input_tokens"] == 0
    assert usage["usage_observation"] == "partial"
    assert "uncached_input_tokens" not in usage
    assert coop_prompt_cache.normalize_codex_usage({
        "inputTokens": 10,
        "cachedInputTokens": 9,
        "cacheWriteInputTokens": 2,
        "outputTokens": 1,
    })["usage_observation"] == "partial"
    assert coop_prompt_cache.normalize_codex_usage({}) == {}
    assert coop_prompt_cache.normalize_codex_usage("not a mapping") == {}


def test_cli_usage_normalizes_claude_result_envelope():
    assert coop_prompt_cache.normalize_cli_usage("claude", {
        "num_turns": 3,
        "modelUsage": {"claude-opus-5": {}},
        "usage": {
            "input_tokens": 80,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 400,
            "output_tokens": 10,
        },
    }) == {
        "model_id": "claude-opus-5",
        "input_tokens": 500,
        "uncached_input_tokens": 80,
        "cached_input_tokens": 400,
        "cache_write_input_tokens": 20,
        "output_tokens": 10,
        "total_tokens": 510,
        "model_calls": 3,
        "usage_observation": "complete",
    }


def test_cli_usage_normalizes_grok_openai_style_envelope():
    assert coop_prompt_cache.normalize_cli_usage("grok", {
        "model": "grok-code-fast-1",
        "num_turns": 1,
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 8,
            "total_tokens": 108,
            "cache_write_input_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 25},
        },
    }) == {
        "model_id": "grok-code-fast-1",
        "input_tokens": 100,
        "uncached_input_tokens": 75,
        "cached_input_tokens": 25,
        "cache_write_input_tokens": 0,
        "output_tokens": 8,
        "total_tokens": 108,
        "model_calls": 1,
        "usage_observation": "complete",
    }


def test_cli_usage_never_invents_missing_cache_semantics():
    usage = coop_prompt_cache.normalize_cli_usage("grok", {
        "model": "grok-code-fast-1",
        "usage": {"prompt_tokens": 100, "completion_tokens": 8},
    })

    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 8
    assert usage["usage_observation"] == "partial"
    assert "cached_input_tokens" not in usage
    assert "cache_write_input_tokens" not in usage
    assert coop_prompt_cache.normalize_cli_usage("codex", {}) == {}
    assert coop_prompt_cache.normalize_cli_usage("claude", []) == {}


def test_codex_usage_delta_requires_monotonic_observed_totals():
    assert coop_prompt_cache.codex_usage_delta(
        {
            "model_id": "gpt-5.4",
            "input_tokens": 1200,
            "uncached_input_tokens": 236,
            "cached_input_tokens": 900,
            "cache_write_input_tokens": 64,
            "output_tokens": 33,
            "reasoning_output_tokens": 7,
            "total_tokens": 1233,
            "model_calls": 2,
        },
        {
            "model_id": "gpt-5.4",
            "input_tokens": 200,
            "uncached_input_tokens": 84,
            "cached_input_tokens": 100,
            "cache_write_input_tokens": 16,
            "output_tokens": 10,
            "reasoning_output_tokens": 3,
            "total_tokens": 210,
            "model_calls": 1,
        },
    ) == {
        "model_id": "gpt-5.4",
        "input_tokens": 1000,
        "uncached_input_tokens": 152,
        "cached_input_tokens": 800,
        "cache_write_input_tokens": 48,
        "output_tokens": 23,
        "reasoning_output_tokens": 4,
        "total_tokens": 1023,
        "model_calls": 1,
        "usage_observation": "complete",
    }
    assert coop_prompt_cache.codex_usage_delta(
        {"input_tokens": 100, "cached_input_tokens": 50},
        {"input_tokens": 200, "cached_input_tokens": 50},
    ) == {}
