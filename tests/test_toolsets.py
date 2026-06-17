import asyncio
import json
import os

from iworkflow.providers import ClaudeProvider, CodexProvider, Provider
from iworkflow.scheduler import Runner
from iworkflow.toolsets import ToolCatalog, ToolKind, ToolSet, ToolSpec


def test_toolset_views_from_mixed_specs():
    mcp_cfg = {"command": "postgres-mcp", "args": ["--readonly"]}
    toolset = ToolSet(
        (
            ToolSpec(
                "postgres",
                ToolKind.MCP,
                "Query the database",
                tags=("database",),
                mcp=mcp_cfg,
                allow_tools=("mcp__postgres__query",),
            ),
            ToolSpec(
                "review-skill",
                ToolKind.SKILL,
                "Review code",
                inject_prompt="Use the review checklist.",
            ),
            ToolSpec(
                "fix-command",
                ToolKind.COMMAND,
                "Fix CI",
                inject_prompt="Follow the CI repair recipe.",
            ),
            ToolSpec(
                "Bash",
                ToolKind.NATIVE,
                "Run shell commands",
                allow_tools=("Read", "mcp__postgres__query"),
            ),
        )
    )

    assert toolset.mcp_servers() == {"postgres": mcp_cfg}
    assert toolset.allowed() == ["mcp__postgres__query", "Read", "Bash"]
    assert toolset.prompt_additions() == (
        "Use the review checklist.\n\nFollow the CI repair recipe."
    )
    assert toolset.is_empty() is False
    assert ToolSet().is_empty() is True


def test_catalog_resolve_by_name_and_tag_ignores_unknowns_in_catalog_order():
    catalog = ToolCatalog()
    postgres = ToolSpec("postgres", ToolKind.MCP, "Query database", tags=("db",))
    review = ToolSpec("review", ToolKind.SKILL, "Review code", tags=("code",))
    bash = ToolSpec("Bash", ToolKind.NATIVE, "Run shell", tags=("shell",))
    catalog.register(postgres)
    catalog.register(review)
    catalog.register(bash)

    resolved = catalog.resolve(["shell", "postgres", "missing", "db"])

    assert resolved.specs == (postgres, bash)
    assert catalog.all() == [postgres, review, bash]


def test_catalog_search_returns_top_k_positive_scores():
    catalog = ToolCatalog()
    postgres = ToolSpec(
        "postgres", ToolKind.MCP, "Query relational database", tags=("sql",)
    )
    bash = ToolSpec("Bash", ToolKind.NATIVE, "Run shell commands", tags=("terminal",))
    review = ToolSpec("review", ToolKind.SKILL, "Inspect code quality")
    catalog.register(postgres)
    catalog.register(bash)
    catalog.register(review)

    result = catalog.search("database sql shell", k=2)

    assert result.specs == (postgres, bash)
    assert catalog.search("nomatch", k=3).is_empty()


class RecordingProvider(Provider):
    def __init__(self):
        super().__init__("codex")
        self.toolset = "not-called"

    async def run(
        self,
        prompt,
        *,
        schema,
        sandbox="read-only",
        cwd=None,
        toolset=None,
        model=None,
    ):
        self.toolset = toolset
        return {"verdict": "DONE", "summary": prompt}


def test_runner_resolves_tools_with_catalog_and_forwards_toolset(tmp_path):
    catalog = ToolCatalog()
    postgres = ToolSpec("postgres", ToolKind.MCP, "Query database", tags=("db",))
    bash = ToolSpec("Bash", ToolKind.NATIVE, "Run shell", tags=("shell",))
    catalog.register(postgres)
    catalog.register(bash)
    provider = RecordingProvider()
    runner = Runner(
        "toolset-forwarding",
        {"codex": provider},
        {"codex": 1},
        journal_dir=str(tmp_path),
        catalog=catalog,
    )

    result = asyncio.run(
        runner.agent("work", label="job", prefer=["codex"], tools=["shell", "db"])
    )

    assert result.ok
    assert isinstance(provider.toolset, ToolSet)
    assert provider.toolset.specs == (postgres, bash)


def test_runner_auto_selects_tools_by_keyword_search(tmp_path):
    catalog = ToolCatalog()
    db = ToolSpec("postgres", ToolKind.MCP, "Query the database with SQL", tags=("db",))
    web = ToolSpec("fetch", ToolKind.MCP, "Fetch a web page over http", tags=("web",))
    catalog.register(db)
    catalog.register(web)
    provider = RecordingProvider()
    runner = Runner(
        "auto-tools",
        {"codex": provider},
        {"codex": 1},
        journal_dir=str(tmp_path),
        catalog=catalog,
    )

    result = asyncio.run(
        runner.agent(
            "help me query the database",
            label="job",
            prefer=["codex"],
            auto_tools=1,
        )
    )

    assert result.ok
    # keyword search picked the db tool by relevance, not the web tool
    assert provider.toolset.specs == (db,)


def test_runner_forwards_none_toolset_without_tools_or_catalog(tmp_path):
    catalog = ToolCatalog()
    catalog.register(ToolSpec("Bash", ToolKind.NATIVE, "Run shell", tags=("shell",)))

    no_tools_provider = RecordingProvider()
    with_catalog = Runner(
        "no-tools",
        {"codex": no_tools_provider},
        {"codex": 1},
        journal_dir=str(tmp_path / "with-catalog"),
        catalog=catalog,
    )
    asyncio.run(with_catalog.agent("work", label="job", prefer=["codex"]))
    assert no_tools_provider.toolset is None

    no_catalog_provider = RecordingProvider()
    without_catalog = Runner(
        "no-catalog",
        {"codex": no_catalog_provider},
        {"codex": 1},
        journal_dir=str(tmp_path / "without-catalog"),
    )
    asyncio.run(
        without_catalog.agent("work", label="job", prefer=["codex"], tools=["shell"])
    )
    assert no_catalog_provider.toolset is None


class CapturingCodexProvider(CodexProvider):
    def __init__(self):
        super().__init__("codex")
        self.argv = None
        self.stdin = None

    async def _exec(self, argv, stdin, cwd=None):
        self.argv = argv
        self.stdin = stdin
        # CodexProvider now reads the answer from the -o file (stdout is the --json
        # event stream); mirror that so the captured result round-trips.
        with open(argv[argv.index("-o") + 1], "w") as fh:
            fh.write("codex ok")
        return 0, "", ""


def test_codex_provider_injects_prompt_and_mcp_flags():
    mcp_cfg = {"command": "postgres-mcp", "args": ["--readonly", "x y"]}
    toolset = ToolSet(
        (
            ToolSpec("postgres", ToolKind.MCP, "Query database", mcp=mcp_cfg),
            ToolSpec(
                "review",
                ToolKind.SKILL,
                "Review code",
                inject_prompt="Use the review checklist.",
            ),
            ToolSpec("Bash", ToolKind.NATIVE, "Run shell"),
        )
    )
    provider = CapturingCodexProvider()

    result = asyncio.run(
        provider.run(
            "base prompt",
            schema=None,
            sandbox="read-only",
            toolset=toolset,
        )
    )

    assert result == "codex ok"
    assert provider.stdin == "Use the review checklist.\n\nbase prompt"
    assert provider.argv.count("--ignore-user-config") == 1
    assert "-c" in provider.argv
    assert 'mcp_servers.postgres.command="postgres-mcp"' in provider.argv
    assert 'mcp_servers.postgres.args=["--readonly","x y"]' in provider.argv


class CapturingClaudeProvider(ClaudeProvider):
    def __init__(self):
        super().__init__("claude")
        self.argv = None
        self.stdin = None
        self.mcp_config = None
        self.mcp_config_path = None
        self.mcp_config_existed_during_exec = False

    async def _exec(self, argv, stdin, cwd=None):
        self.argv = argv
        self.stdin = stdin
        self.mcp_config_path = argv[argv.index("--mcp-config") + 1]
        self.mcp_config_existed_during_exec = os.path.exists(self.mcp_config_path)
        with open(self.mcp_config_path) as f:
            self.mcp_config = json.load(f)
        return 0, json.dumps({"result": "claude ok"}), ""


def test_claude_provider_injects_prompt_mcp_config_and_allowed_tools():
    mcp_cfg = {"command": "postgres-mcp", "args": ["--readonly"]}
    toolset = ToolSet(
        (
            ToolSpec(
                "postgres",
                ToolKind.MCP,
                "Query database",
                mcp=mcp_cfg,
                allow_tools=("mcp__postgres__query",),
            ),
            ToolSpec(
                "review",
                ToolKind.SKILL,
                "Review code",
                inject_prompt="Use the review checklist.",
            ),
            ToolSpec("Bash", ToolKind.NATIVE, "Run shell"),
        )
    )
    provider = CapturingClaudeProvider()

    result = asyncio.run(
        provider.run(
            "base prompt",
            schema=None,
            sandbox="read-only",
            toolset=toolset,
        )
    )

    assert result == "claude ok"
    assert provider.stdin == "Use the review checklist.\n\nbase prompt"
    assert provider.mcp_config == {"mcpServers": {"postgres": mcp_cfg}}
    assert provider.mcp_config_existed_during_exec is True
    assert not os.path.exists(provider.mcp_config_path)
    allowed_index = provider.argv.index("--allowedTools")
    assert provider.argv[allowed_index + 1:allowed_index + 3] == [
        "mcp__postgres__query",
        "Bash",
    ]
