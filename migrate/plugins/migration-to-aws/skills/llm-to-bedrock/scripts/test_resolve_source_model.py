"""Content lock for the model-line-sacred resolution rules.

The hard rule: auto-resolution may only pin the SAME model line to a date or
pure version suffix. Cross-line swaps and moving aliases escalate to the user.
"""
import json
import pathlib
import subprocess  # nosec B404 — test-only, fixed args
import sys
import tempfile

from resolve_source_model import resolve, safe_variant


def test_exact_match_wins():
    out = resolve(["gpt-5.4", "gpt-5.4-2026-03-05"], "gpt-5.4")
    assert out == {"status": "exact", "resolved_id": "gpt-5.4"}


def test_date_suffix_is_same_line():
    out = resolve(["gpt-5.4-2026-03-05"], "gpt-5.4")
    assert out["status"] == "prefix"
    assert out["resolved_id"] == "gpt-5.4-2026-03-05"


def test_version_suffix_is_same_line():
    out = resolve(["claude-3-5-sonnet-2"], "claude-3-5-sonnet")
    assert out["status"] == "prefix"


def test_shortest_safe_variant_picked():
    out = resolve(["gpt-5.4-2026-03-05-1234", "gpt-5.4-2026-03-05"], "gpt-5.4")
    assert out["resolved_id"] == "gpt-5.4-2026-03-05"


def test_alphabetic_suffixes_never_auto_resolve():
    # mini/pro/latest are different model lines or moving aliases — the exact
    # failure mode the hard rule exists to prevent.
    catalog = ["gpt-5.4-mini", "gpt-5.4-pro", "gpt-5.4-latest"]
    out = resolve(catalog, "gpt-5.4")
    assert out["status"] == "not_found"
    assert out.get("ambiguous_prefix") is True
    assert set(out["candidates"]) == set(catalog)


def test_cross_line_never_matches():
    # gpt-5 and gpt-54 must not satisfy a gpt-5.4 plan id.
    out = resolve(["gpt-5", "gpt-54", "gpt-5.5"], "gpt-5.4")
    assert out["status"] == "not_found"
    assert "ambiguous_prefix" not in out


def test_no_match_ranks_by_longest_common_prefix():
    out = resolve(["claude-3-5-haiku", "gemini-1.5-pro", "gpt-4o"], "claude-3-5-sonnet")
    assert out["status"] == "not_found"
    assert out["candidates"][0] == "claude-3-5-haiku"


def test_safe_variant_boundary():
    assert safe_variant("gpt-5.4-2026-03-05", "gpt-5.4")
    assert safe_variant("gpt-5.4-3.1", "gpt-5.4")
    assert not safe_variant("gpt-5.4-turbo", "gpt-5.4")
    assert not safe_variant("gpt-5.4-2026-03-05-preview", "gpt-5.4")


def test_cli_no_key_exits_2_without_network():
    # An env file with no recognized provider key must produce {"status":
    # "no_key"} and exit 2 before any network call is attempted.
    script = pathlib.Path(__file__).parent / "resolve_source_model.py"
    with tempfile.TemporaryDirectory() as tmp:
        env_file = pathlib.Path(tmp) / ".source-provider-env"
        env_file.write_text("SOMETHING_ELSE=x\n", encoding="utf-8")
        proc = subprocess.run(  # nosec B603 — test-only, fixed args
            [sys.executable, str(script), str(env_file)],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "PLAN_MODEL_ID": "gpt-4o"},
        )
    assert proc.returncode == 2
    assert json.loads(proc.stdout) == {"status": "no_key"}


def test_provider_selected_from_file_not_ambient_env(tmp_path, monkeypatch):
    # Review finding: an ambient OPENAI_API_KEY must not select OpenAI when
    # the migration's env file carries a GEMINI_API_KEY — the model ID would
    # go to the wrong provider's API.
    import resolve_source_model as rsm
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ambient")
    p = tmp_path / ".source-provider-env"
    p.write_text("GEMINI_API_KEY=AIza-file\n", encoding="utf-8")
    pairs = rsm.load_env_file(str(p))
    assert rsm.pick_provider(pairs) == "GEMINI_API_KEY"


def test_anthropic_pagination_follows_has_more(monkeypatch):
    import resolve_source_model as rsm
    calls = []

    def fake_get(url, headers):
        calls.append(url)
        if "after_id" not in url:
            return {"data": [{"id": "claude-3-5-haiku-20241022"}],
                    "has_more": True, "last_id": "claude-3-5-haiku-20241022"}
        return {"data": [{"id": "claude-3-5-sonnet-20241022"}], "has_more": False}

    monkeypatch.setattr(rsm, "_get_json", fake_get)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    ids = rsm.list_anthropic()
    assert len(calls) == 2 and "after_id=claude-3-5-haiku-20241022" in calls[1]
    # A page-2 model must resolve, not report not_found.
    assert rsm.resolve(ids, "claude-3-5-sonnet")["status"] == "prefix"


def test_gemini_pagination_follows_next_page_token(monkeypatch):
    import resolve_source_model as rsm
    calls = []

    def fake_get(url, headers):
        calls.append(url)
        if "pageToken" not in url:
            return {"models": [{"name": "models/gemini-1.5-flash"}],
                    "nextPageToken": "tok2"}
        return {"models": [{"name": "models/gemini-1.5-pro"}]}

    monkeypatch.setattr(rsm, "_get_json", fake_get)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza")
    ids = rsm.list_gemini()
    assert len(calls) == 2 and "pageToken=tok2" in calls[1]
    assert "gemini-1.5-pro" in ids
