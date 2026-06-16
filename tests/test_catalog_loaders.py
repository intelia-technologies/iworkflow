import json

from iworkflow.catalog_loaders import load_project_catalog
from iworkflow.toolsets import ToolKind


def test_load_project_catalog_registers_mcp_servers_and_claude_skill(tmp_path):
    command_cfg = {"command": "python", "args": ["-m", "server"]}
    url_cfg = {"url": "https://example.invalid/mcp"}
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"local": command_cfg, "remote": url_cfg}})
    )

    skill_dir = tmp_path / ".claude" / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    body = "Use foo carefully.\n"
    (skill_dir / "SKILL.md").write_text(
        "---\nname: foo\ndescription: does foo\n---\n" + body
    )

    catalog = load_project_catalog(str(tmp_path))

    specs = {spec.name: spec for spec in catalog.all()}
    assert specs["local"].kind == ToolKind.MCP
    assert specs["local"].description == "local"
    assert specs["local"].mcp == command_cfg
    assert specs["remote"].kind == ToolKind.MCP
    assert specs["remote"].description == "remote"
    assert specs["remote"].mcp == url_cfg
    assert specs["foo"].kind == ToolKind.SKILL
    assert specs["foo"].description == "does foo"
    assert specs["foo"].inject_prompt == body


def test_load_project_catalog_empty_project_returns_empty_catalog(tmp_path):
    assert load_project_catalog(str(tmp_path)).all() == []


def test_load_project_catalog_reads_codex_config_flat_skills_and_commands(tmp_path):
    json_cfg = {"command": "json-server"}
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"shared": json_cfg}})
    )

    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(
        """
[mcp_servers.shared]
command = "toml-server"

[mcp_servers.toml]
url = "https://example.invalid/mcp"
"""
    )

    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    flat_body = "Use bar.\n"
    (skills_dir / "bar.md").write_text(
        "---\ndescription: bar desc\n---\n" + flat_body
    )

    commands_dir = tmp_path / ".claude" / "commands"
    commands_dir.mkdir()
    command_text = "\nFix things\n\nRun the fix workflow.\n"
    (commands_dir / "fix.md").write_text(command_text)

    catalog = load_project_catalog(str(tmp_path))

    specs = {spec.name: spec for spec in catalog.all()}
    assert specs["shared"].mcp == json_cfg
    assert specs["toml"].kind == ToolKind.MCP
    assert specs["toml"].mcp == {"url": "https://example.invalid/mcp"}
    assert specs["bar"].kind == ToolKind.SKILL
    assert specs["bar"].description == "bar desc"
    assert specs["bar"].inject_prompt == flat_body
    assert specs["fix"].kind == ToolKind.COMMAND
    assert specs["fix"].description == "Fix things"
    assert specs["fix"].inject_prompt == command_text


def test_load_project_catalog_reads_namespaced_commands_and_codex_skills(tmp_path):
    commands_dir = tmp_path / ".claude" / "commands" / "workflows"
    commands_dir.mkdir(parents=True)
    command_text = (
        "---\n"
        "description: Plan an iworkflow change\n"
        "argument-hint: [topic]\n"
        "---\n"
        "\n"
        "# Plan\n"
        "\n"
        "Create the implementation plan.\n"
    )
    (commands_dir / "plan.md").write_text(command_text)

    codex_skill_dir = tmp_path / ".codex" / "skills" / "workflows-plan"
    codex_skill_dir.mkdir(parents=True)
    skill_body = "Follow the local workflows plan command.\n"
    (codex_skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: workflows-plan\n"
        "description: Use when planning iworkflow work in Codex\n"
        "---\n"
        + skill_body
    )

    catalog = load_project_catalog(str(tmp_path))

    specs = {spec.name: spec for spec in catalog.all()}
    assert specs["workflows:plan"].kind == ToolKind.COMMAND
    assert specs["workflows:plan"].description == "Plan an iworkflow change"
    assert specs["workflows:plan"].inject_prompt == command_text
    assert specs["workflows-plan"].kind == ToolKind.SKILL
    assert specs["workflows-plan"].description == "Use when planning iworkflow work in Codex"
    assert specs["workflows-plan"].inject_prompt == skill_body
