# Evals
 
Manual evaluations of how an agent uses pydeps-mcp: does it pick the right
tool, interpret the output correctly, and recover from errors?
 
**Setup:** Claude Code, model Opus 5.5 Medium, run on 2026-11-13.
 
## Test projects
 
| Project | Contents | Covers |
|---|---|---|
| `fixtures/requirements-project` | `requirements.txt` with stale pins, a `-r` line, a range, an unconstrained dep, a private package, a fake package and invalid lines | `blocked`, `major_bump`, `allowed`, `unconstrained`, `errors`, `skipped`, `unparsed` |
| `fixtures/pyproject-project` | Same dependencies in `[project].dependencies` | `pyproject.toml` parsing |
| `fixtures/empty-project` | No dependency files | "no dependencies" error |
| `fixtures/posthog` | 208 dependencies in `pyproject.toml` copied from [`posthog`](https://github.com/PostHog/posthog/blob/b89cff503190a89c00924c11349a7a5879ad6b55/pyproject.toml) |    Measures PyPI fetching only (`bench.py`), not the agent's turn: ~6.8× faster with 10 concurrent requests |
 
## Results
 
| # | Prompt | Expected | Tools called | Pass? | Notes |
|---|---|---|---|---|---|
| 1 | "What dependencies does `<absolute path>/fixtures/requirements-project` have?" | `list_dependencies` only | `list_dependencies` | Yes | See *[What changed](#what-changed-because-of-these-evals)*: `-r` line was dropped without notice. |
| 2 | "What dependencies does `<absolute path>/fixtures/pyproject-project` have?" | `list_dependencies` only | `list_dependencies` | Yes | The fixture has no optional dependencies or dependency groups, which the tool doesn’t read. |
| 3 | "Audit the dependencies of `<absolute path>/fixtures/requirements-project`" | One `audit_project` call | `audit_project` | Yes | Correct statuses for all five checked packages, with sensible risk notes (django three majors behind and out of LTS support; requests a same-major bump). Reported both packages missing from PyPI, both unparsed lines and the skipped `-r` include. Also flagged a tool issue: numpy is capped at `<2` with 2.5.4 latest, but `major_bump` was false. See *[What changed](#what-changed-because-of-these-evals)*. |
| 4 | "Is it safe to upgrade Django in this project?" | Notices `major_bump`, points to changelog | None | Yes | Answered from the audit_project result earlier in the same session and pointed out the major version bump. |
| 5 | "Check my private-lib dependency" | Explains it's not on PyPI, suggests a private index | None | Yes | Answered from the audit_project result earlier in the same session. Claude: "I couldn't check it. `my-private-lib` (pinned `==1.0` in `fixtures/requirements-project/requirements.txt`) isn't on PyPI, and `check_latest` only looks at PyPI." and "If it's a real package, you'd have to check it against the private index it's published on." |
| 6 | "Audit `<absolute path>/fixtures/empty-project`" | Reads the error, asks whether this is the project root | `audit_project` | Yes | The audit tool returned an error saying no dependencies are declared and asking to check that this is the project root. |
| 7 | Prompt 2 with a relative path | Recovers using the error message | `list_dependencies` | N/A | Claude resolved the relative path itself before calling, so the error message wasn’t tested. |
 
## What changed because of these evals

### `-r` include lines were dropped silently

- **Observed:** `requirements.txt` starts with `-r requirements-la.txt`. `list_dependencies` skipped it without any notice — Claude also notes it does not exist.
- **Change:** lines starting with `-` are now reported in a new `skipped` field instead of silently skipped.
- **Re-run:** Claude now mentions the line as skipped.
- **Decision:** following includes is out of scope for now; surfacing them is enough for the agent to warn the user.

### `major_bump` missed upper-bound caps

- **Observed:** numpy is declared `<2` with 2.5.4 latest. Claude correctly said moving to 2.x is a breaking change, but noticed `major_bump` was false, so the entry ranked lower than it should.
- **Change:** `major_bump` now compares the latest major against the highest major the whole constraint allows (`==`, `<`, `<=`, `~=`), instead of only handling a single `==` pin.
- **Re-run:** Pass: numpy `<2` now flagged `major_bump: true` and sorted second, after django; summary unchanged (3 blocked, 1 allowed, 1 unconstrained, 2 errors).
 
## Performance
 
| Concurrency | Dependencies | Audit time |
|---|---|---|
| 1 | 208 | 13.93s |
| 10 | 208 | 2.05s |