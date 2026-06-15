import json

from iworkflow.cli import register_claude, register_codex


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
