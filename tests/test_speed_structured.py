"""Structured answers: schema one-shot + runner postcommit contracts."""
import json
from types import SimpleNamespace

from agent_coop import coop_prompt_cache
from agent_coop import coop_start


def _claude_envelope(question_id=9, answer="pong"):
    return json.dumps({
        "type": "result",
        "result": json.dumps({"question_id": question_id, "answer": answer}),
        "structured_output": {"question_id": question_id, "answer": answer},
        "num_turns": 1,
        "modelUsage": {"claude-opus-5": {}},
        "usage": {
            "input_tokens": 80,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 400,
            "output_tokens": 10,
        },
    })


def _grok_envelope(question_id=9, answer="pong"):
    return json.dumps({
        "num_turns": 1,
        "model": "grok-code-fast-1",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 8,
            "total_tokens": 108,
            "cache_write_input_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 25},
        },
        "structuredOutput": {"question_id": question_id, "answer": answer},
    })


# ---- parse_structured_answer ------------------------------------------------

def test_parse_accepts_both_provider_envelopes():
    assert coop_start.parse_structured_answer(
        _claude_envelope(), expected_question_id=9) == "pong"
    assert coop_start.parse_structured_answer(
        _grok_envelope(), expected_question_id=9) == "pong"


def test_parse_rejects_wrong_question_empty_answer_and_garbage():
    assert coop_start.parse_structured_answer(
        _claude_envelope(question_id=8), expected_question_id=9) is None
    assert coop_start.parse_structured_answer(
        _claude_envelope(answer="   "), expected_question_id=9) is None
    assert coop_start.parse_structured_answer(
        json.dumps({"structured_output": {"question_id": True,
                                          "answer": "x"}}),
        expected_question_id=1) is None
    assert coop_start.parse_structured_answer(
        "not json", expected_question_id=9) is None
    assert coop_start.parse_structured_answer(
        json.dumps(["list"]), expected_question_id=9) is None
    assert coop_start.parse_structured_answer(
        None, expected_question_id=9) is None


# ---- structured_answer_argv -------------------------------------------------

def test_argv_shapes_per_provider():
    claude = coop_start.structured_answer_argv(
        "claude", resolve=lambda name: f"/bin/{name}")
    assert claude[0] == "/bin/claude"
    # The prompt is delivered off argv (stdin); -p takes no positional.
    assert claude[1:3] == ["-p", "--output-format"]
    assert "--json-schema" in claude and "--output-format" in claude
    assert claude[claude.index("--tools") + 1] == ""
    assert claude[claude.index("--setting-sources") + 1] == ""
    assert claude[claude.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--strict-mcp-config" in claude
    assert "--safe-mode" in claude
    assert "--disable-slash-commands" in claude
    assert "--no-session-persistence" in claude
    assert "--no-chrome" in claude
    assert "--dangerously-skip-permissions" not in claude
    grok = coop_start.structured_answer_argv(
        "grok", resolve=lambda name: f"/bin/{name}")
    assert grok[0] == "/bin/grok"
    assert "--always-approve" in grok and "--json-schema" in grok
    assert "-p" in grok  # replaced by --prompt-file at delivery time
    assert "--tools" not in grok
    assert coop_start.structured_answer_argv(
        "codex", resolve=lambda name: name) is None
    assert coop_start.structured_answer_argv(
        "claude", resolve=lambda name: None) is None


def test_schema_is_strict_json():
    schema = json.loads(coop_start.STRUCTURED_ANSWER_SCHEMA)
    assert schema["required"] == ["question_id", "answer"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["answer"]["minLength"] == 1


def test_structured_answer_env_keeps_runtime_and_auth_not_workspace_state():
    source = {
        "PATH": r"C:\tools",
        "PATHEXT": ".EXE;.CMD",
        "USERPROFILE": r"C:\Users\tester",
        "TEMP": r"C:\Temp",
        "HTTPS_PROXY": "http://proxy",
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": "eu-west-2",
        "XAI_API_KEY": "xai-secret",
        "GOOGLE_APPLICATION_CREDENTIALS": "must-not-pass-without-vertex",
        "COOP_DB": r"C:\work\board.db",
        "COOP_SESSION_ID": "session-secret",
        "PYTHONPATH": r"C:\work",
        "UNRELATED_SECRET": "must-not-pass",
    }

    isolated = coop_start.structured_answer_env(source, provider="claude")

    assert isolated == {
        "PATH": r"C:\tools",
        "PATHEXT": ".EXE;.CMD",
        "USERPROFILE": r"C:\Users\tester",
        "TEMP": r"C:\Temp",
        "HTTPS_PROXY": "http://proxy",
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-secret",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_REGION": "eu-west-2",
    }
    assert coop_start.structured_answer_env(source, provider="grok") == {
        "PATH": r"C:\tools",
        "PATHEXT": ".EXE;.CMD",
        "USERPROFILE": r"C:\Users\tester",
        "TEMP": r"C:\Temp",
        "HTTPS_PROXY": "http://proxy",
        "XAI_API_KEY": "xai-secret",
    }
    # Without the Bedrock opt-in the AWS credentials stay in the shell.
    without = coop_start.structured_answer_env(
        {**source, "CLAUDE_CODE_USE_BEDROCK": "0"},
        provider="claude",
    )
    assert "AWS_REGION" not in without


# ---- invoke_structured_answer -----------------------------------------------

def test_invoke_isolates_claude_cwd_env_and_passes_timeout(tmp_path):
    calls = {}
    observed_usage = []

    def runner(argv, *, cwd, capture_output, timeout, env, stdin):
        calls["argv"] = argv
        calls["cwd"] = cwd
        calls["timeout"] = timeout
        calls["env"] = env
        calls["stdin"] = stdin.read().decode("utf-8")
        assert tmp_path.resolve() != type(tmp_path)(cwd).resolve()
        assert type(tmp_path)(cwd).is_dir()
        return SimpleNamespace(
            returncode=0, stdout=_claude_envelope().encode("utf-8"))

    answer = coop_start.invoke_structured_answer(
        provider="claude", prompt="P", expected_question_id=9,
        cwd=str(tmp_path), timeout_s=45,
        runner=runner, resolve=lambda name: name,
        usage_callback=observed_usage.append,
        base_env={
            "PATH": "provider-path",
            "USERPROFILE": "profile",
            "ANTHROPIC_API_KEY": "auth",
            "COOP_DB": "must-not-pass",
            "PYTHONPATH": "must-not-pass",
        })
    assert answer == "pong"
    assert calls["cwd"] != str(tmp_path)
    assert not type(tmp_path)(calls["cwd"]).exists()
    assert calls["timeout"] == 45.0
    assert calls["argv"][0] == "claude"
    assert "P" not in calls["argv"]
    assert calls["stdin"] == "P"
    assert calls["env"] == {
        "PATH": "provider-path",
        "USERPROFILE": "profile",
        "ANTHROPIC_API_KEY": "auth",
    }
    assert observed_usage == [{
        "model_id": "claude-opus-5",
        "input_tokens": 500,
        "uncached_input_tokens": 80,
        "cached_input_tokens": 400,
        "cache_write_input_tokens": 20,
        "output_tokens": 10,
        "total_tokens": 510,
        "model_calls": 1,
        "usage_observation": "complete",
    }]


def test_grok_structured_invocation_is_isolated_like_claude(tmp_path):
    calls = {}
    observed_usage = []

    def runner(argv, **kwargs):
        calls["argv"] = argv
        calls.update(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout=_grok_envelope().encode("utf-8"),
        )

    answer = coop_start.invoke_structured_answer(
        provider="grok",
        prompt="P",
        expected_question_id=9,
        cwd=str(tmp_path),
        timeout_s=30,
        runner=runner,
        resolve=lambda name: name,
        usage_callback=observed_usage.append,
        base_env={"PATH": "provider-path", "COOP_DB": "must-not-pass"},
    )

    assert answer == "pong"
    # No tool restriction exists for grok's one-shot, so it never runs in
    # the workspace and never sees the workspace environment.
    assert calls["cwd"] != str(tmp_path)
    assert calls["env"] == {"PATH": "provider-path"}
    assert "--tools" not in calls["argv"]
    assert observed_usage[0]["model_id"] == "grok-code-fast-1"
    assert observed_usage[0]["uncached_input_tokens"] == 75
    assert observed_usage[0]["usage_observation"] == "complete"


def test_invoke_fails_soft_on_exit_exception_and_missing_cli():
    observed_usage = []
    assert coop_start.invoke_structured_answer(
        provider="claude", prompt="P", expected_question_id=9, cwd=".",
        runner=lambda *a, **k: SimpleNamespace(returncode=2, stdout=b""),
        resolve=lambda name: name,
        usage_callback=observed_usage.append) is None

    def raises(*_a, **_k):
        raise OSError("boom")

    assert coop_start.invoke_structured_answer(
        provider="claude", prompt="P", expected_question_id=9, cwd=".",
        runner=raises, resolve=lambda name: name,
        usage_callback=observed_usage.append) is None
    assert coop_start.invoke_structured_answer(
        provider="grok", prompt="P", expected_question_id=9, cwd=".",
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"{}"),
        resolve=lambda name: None,
        usage_callback=observed_usage.append) is None
    assert observed_usage == [
        {"usage_observation": "unobserved"},
        {"usage_observation": "unobserved"},
        {"usage_observation": "unobserved"},
    ]


# ---- structured_answer_prompt -----------------------------------------------

def test_prompt_carries_question_identity_and_item():
    prompt = coop_prompt_cache.structured_answer_prompt(
        {"question_id": 9, "exact_question": "ping from codex?",
         "asked_by_agent": "codex", "assigned_to_agent": "grok"},
        {"id": 35, "title": "mesh run"})
    assert "ping from codex?" in prompt
    assert "question_id 9" in prompt
    assert "codex" in prompt and "grok" in prompt
    assert "work item 35" in prompt
    bare = coop_prompt_cache.structured_answer_prompt(
        {"question_id": 2, "exact_question": "q", "asked_by_agent": "a",
         "assigned_to_agent": "b"})
    assert "work item" not in bare
