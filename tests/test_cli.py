import json

import pytest

from iworkflow.cli import (
    RegisterError, register_claude, register_codex, unregister_claude, unregister_codex,
)


def test_register_claude_creates_and_merges(tmp_path):
    path, wrote = register_claude(str(tmp_path))
    assert wrote is True
    assert path == tmp_path / ".mcp.json"
    data = json.loads(path.read_text())
    assert data["mcpServers"]["iworkflow"]["command"] == "iworkflow-mcp"

    # idempotent: second call doesn't duplicate / re-write
    _, wrote_again = register_claude(str(tmp_path))
    assert wrote_again is False


def test_register_claude_preserves_existing_servers(tmp_path):
    existing = {"mcpServers": {"other": {"command": "other-mcp"}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(existing))
    register_claude(str(tmp_path))
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert data["mcpServers"]["other"]["command"] == "other-mcp"   # untouched
    assert "iworkflow" in data["mcpServers"]


def test_register_codex_appends_block_once(tmp_path):
    path, wrote = register_codex(str(tmp_path))
    assert wrote is True
    assert path == tmp_path / ".codex" / "config.toml"
    text = path.read_text()
    assert "[mcp_servers.iworkflow]" in text
    assert 'command = "iworkflow-mcp"' in text

    _, wrote_again = register_codex(str(tmp_path))
    assert wrote_again is False
    assert text.count("[mcp_servers.iworkflow]") == 1


def test_register_adds_iworkflow_to_gitignore(tmp_path):
    from iworkflow.cli import ensure_gitignore

    path, wrote = ensure_gitignore(str(tmp_path))
    assert wrote is True
    assert path == tmp_path / ".gitignore"
    assert ".iworkflow/" in path.read_text()

    _, again = ensure_gitignore(str(tmp_path))   # idempotent
    assert again is False
    assert path.read_text().count(".iworkflow/") == 1


def test_ensure_gitignore_preserves_existing(tmp_path):
    from iworkflow.cli import ensure_gitignore

    (tmp_path / ".gitignore").write_text("node_modules/\n.venv/\n")
    ensure_gitignore(str(tmp_path))
    text = (tmp_path / ".gitignore").read_text()
    assert "node_modules/" in text and ".venv/" in text and ".iworkflow/" in text


# --- OSS-robustness of register/unregister (the .codex/config.toml incident) ----
def _codex(tmp_path):
    return tmp_path / ".codex" / "config.toml"


def test_register_codex_uses_owned_markers(tmp_path):
    register_codex(str(tmp_path))
    text = _codex(tmp_path).read_text()
    assert "# >>> iworkflow >>>" in text and "# <<< iworkflow <<<" in text


def test_register_codex_result_is_valid_toml(tmp_path):
    import tomllib

    existing = '[mcp_servers.other]\ncommand = "other"\n'
    _codex(tmp_path).parent.mkdir(parents=True)
    _codex(tmp_path).write_text(existing)
    register_codex(str(tmp_path))
    data = tomllib.loads(_codex(tmp_path).read_text())          # parses → no duplicate-key
    assert data["mcp_servers"]["iworkflow"]["command"] == "iworkflow-mcp"
    assert data["mcp_servers"]["other"]["command"] == "other"   # untouched


def test_register_codex_leaves_foreign_entry_alone(tmp_path):
    # another tool already registered iworkflow OUTSIDE our markers (the intelia case)
    foreign = '[mcp_servers.iworkflow]\ncommand = "via-some-proxy"\n'
    _codex(tmp_path).parent.mkdir(parents=True)
    _codex(tmp_path).write_text(foreign)
    _, wrote = register_codex(str(tmp_path))
    text = _codex(tmp_path).read_text()
    assert wrote is False                                       # didn't add a 2nd block
    assert text.count("[mcp_servers.iworkflow]") == 1
    assert "# >>> iworkflow >>>" not in text                    # left the foreign one as-is


def test_register_codex_self_heals_duplicate(tmp_path):
    # a broken file: our managed block AND a foreign block → duplicate key (won't parse)
    broken = ('# >>> iworkflow >>>\n[mcp_servers.iworkflow]\ncommand = "iworkflow-mcp"\n'
              '# <<< iworkflow <<<\n\n[mcp_servers.iworkflow]\ncommand = "foreign"\n')
    _codex(tmp_path).parent.mkdir(parents=True)
    _codex(tmp_path).write_text(broken)
    _, wrote = register_codex(str(tmp_path))
    import tomllib
    text = _codex(tmp_path).read_text()
    assert wrote is True
    tomllib.loads(text)                                         # heals → parses again
    assert text.count("[mcp_servers.iworkflow]") == 1


def test_register_codex_refuses_broken_foreign_toml(tmp_path):
    _codex(tmp_path).parent.mkdir(parents=True)
    _codex(tmp_path).write_text("this is = = not valid toml\n")
    with pytest.raises(RegisterError):
        register_codex(str(tmp_path))


def test_unregister_codex_removes_our_block_only(tmp_path):
    _codex(tmp_path).parent.mkdir(parents=True)
    _codex(tmp_path).write_text('[mcp_servers.other]\ncommand = "x"\n')
    register_codex(str(tmp_path))
    _, changed = unregister_codex(str(tmp_path))
    text = _codex(tmp_path).read_text()
    assert changed is True
    assert "iworkflow" not in text
    assert "# >>> iworkflow >>>" not in text
    assert "mcp_servers.other" in text                          # foreign server preserved

    _, again = unregister_codex(str(tmp_path))
    assert again is False                                       # idempotent


def test_unregister_claude_round_trip(tmp_path):
    register_claude(str(tmp_path))
    _, changed = unregister_claude(str(tmp_path))
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert changed is True
    assert "iworkflow" not in data.get("mcpServers", {})
    _, again = unregister_claude(str(tmp_path))
    assert again is False


def test_register_claude_refuses_invalid_json(tmp_path):
    (tmp_path / ".mcp.json").write_text("{not json")
    with pytest.raises(RegisterError):
        register_claude(str(tmp_path))
