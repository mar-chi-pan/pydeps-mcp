# pydeps-mcp

An MCP server that lets AI agents audit a Python project's dependencies:
what's declared, what's outdated, what's blocked by a version constraint,
and which upgrades are major version jumps worth reading the changelog for
(if available).

It's read-only: it never edits your files, so an agent can call it freely.

## Tools

| Tool | Use it for | Returns |
|---|---|---|
| `audit_project(project_path)` | A full health check in one call | Entries sorted by urgency, a per-status summary, plus `errors`, `skipped` and `unparsed` |
| `list_dependencies(project_path)` | Just seeing what's declared | Name, specifier, extras and marker per dependency. Includes the file name that declares the dependencies, any skipped lines and any unparsed dependencies. |
| `check_latest(package)` | One package's latest release | Latest version, release date, supported Python versions and `changelog_url` (if any). |

Agents: see [llms.txt](llms.txt) for a compact description of the tools and errors.

`audit_project` gives each dependency a status:

- **blocked**: the declared constraint excludes the latest version, so upgrading means changing it.
- **allowed**: the constraint already permits the latest version.
- **unconstrained**: no version constraint is declared.

`major_bump` is true when the latest version is a major release above the highest version the constraint allows (e.g. `==3.2` or `<2`). Check `changelog_url` before upgrading.

Dependencies are read from `[project].dependencies` in `pyproject.toml`,
falling back to `requirements.txt`.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/mar-chi-pan/pydeps-mcp.git
cd pydeps-mcp
uv sync
```

Try it in the MCP Inspector:

```bash
uv run mcp dev server.py
```

## Connect to Claude

**Claude Code**

```bash
claude mcp add --transport stdio pydeps -- \
  uv run --directory /absolute/path/to/pydeps-mcp python server.py
```

Run `/mcp` inside Claude Code to check it's connected.

**Claude desktop app**: add this to `claude_desktop_config.json`, then fully restart the app:

```json
{
  "mcpServers": {
    "pydeps": {
      "command": "/absolute/path/to/uv",
      "args": ["run", "--directory", "/absolute/path/to/pydeps-mcp", "python", "server.py"]
    }
  }
}
```

Then ask something like: *"Audit the dependencies of /path/to/my-project. What should I upgrade first?"*

## Designing for agents

Some decisions that only make sense because the user is a model:

- **Docstrings are written for the model.** They say *when* to call each tool
  (e.g. "prefer `audit_project` to calling `check_latest` for every package"),
  not just what it does. See [EVALS.md](EVALS.md) for how agents used them in
  practice.
- **Structured, compact output.** Typed results (`TypedDict` + a `StrEnum` for
  status) give the agent an output schema with the allowed values up front.
  PyPI's large JSON response is reduced to the few fields an agent needs.
- **Errors that say what to do next.** "Not found on PyPI. Check the spelling,
  or it may come from a private index" rather than a bare 404.
- **Partial results over failure.** One private package or malformed line
  doesn't fail the audit; it lands in `errors`, `skipped` or `unparsed` and the rest
  is returned.
- **Sorted by urgency, with a summary.** The agent can read the summary and the
  first few entries and stop, instead of scanning everything.
- **A next step, not just a verdict.** `changelog_url` points the agent at where
  to check for breaking changes.

## Implementation notes

- PyPI lookups are async (`httpx.AsyncClient`) with bounded concurrency
  (10 in flight). On PostHog’s 208-dependency project this cut audit time from
  13.93s to 2.05s.
- The HTTP client's lifetime is managed by the server's lifespan, so it's
  opened once and closed cleanly on shutdown.
- Packages listed more than once (e.g. with different markers) are fetched once.

## Evals

I tested the server with Claude Code against fixture projects; see
[EVALS.md](EVALS.md). Two findings changed the tool:

- `-r` include lines were dropped silently, so the agent couldn't tell its
  picture was incomplete. They're now reported in `skipped`.
- Claude noticed `major_bump` ignored caps like `numpy<2`. It now compares
  against the highest major the whole constraint allows.

Benchmark: `uv run python bench.py fixtures/posthog`

## Limitations

- Reads declared constraints, not installed versions or lock files.
- Only queries public PyPI; private indexes show up in `errors`.

## License

MIT