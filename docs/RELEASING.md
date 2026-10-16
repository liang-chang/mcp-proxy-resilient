# Release maintenance

This repository is independently maintained at https://github.com/liang-chang/mcp-proxy-resilient.
Keep the upstream Git history, original author attribution, and MIT license.
The initial distribution channels are GitHub source, wheel/sdist assets, and a Windows x64
portable ZIP. PyPI, Docker Hub, GHCR, and automatic dependency-PR merging are not enabled.

## Version and artifacts

The only program version source is src/mcp_proxy/__init__.py; setuptools reads it dynamically.
The initial independent candidate keeps version 0.13.0+resilient.2. Its tag must be exactly
v0.13.0+resilient.2, including the plus sign. This preserves the upstream-base version.
For a future version, update the source, changelog, and example installation filenames together.
uv.lock contains no fixed editable-project version; dependency/metadata changes still require
uv lock and a successful uv sync --locked.

Assets for the initial candidate:

- mcp_proxy_resilient-0.13.0+resilient.2-py3-none-any.whl
- mcp_proxy_resilient-0.13.0+resilient.2.tar.gz
- mcp-proxy-resilient-0.13.0-r2-windows-x64.zip
- The ZIP's .sha256 sidecar and SHA256SUMS.txt covering every release asset.

The Windows build script reinstalls the editable project before freezing. Without that step,
the executable could identify as r2 while retaining r1 dist-info metadata.
The ZIP must contain Python DLLs, _internal, dependency notices, documentation, and generic
examples. Never include local servers.json, credentials, cache, logs, or terminal recordings.
Internal agent plans and worktrees are excluded from the publication working set by .gitignore.

## Local preparation

Use an isolated feature worktree and preserve unrelated changes. Before the actual release,
review and commit the intended source/docs/workflow files, integrate them into main, and
verify the exact commit to be tagged. Generated dist/build files stay out of Git.
Use git status --short --untracked-files=all; avoid blindly staging the workspace root.

From the project root:

~~~powershell
uv sync --locked --python 3.13
uv run --locked pytest -q
uv run --locked mypy src
uv run --locked ruff check src tests scripts/portable_entry.py
uv run --locked ruff format --check src tests scripts/portable_entry.py
uv build --out-dir dist/python
./scripts/build-windows.ps1
~~~

Extract the ZIP to a new directory containing spaces. Set MCP_PROXY_PORTABLE_EXE to its exe,
then rerun uv run --locked pytest -q. Without that variable, the 11 Windows artifact tests
are skipped; source tests alone are not artifact acceptance. The tests restrict the proxy's
PATH to System32 and remove Python environment variables.

Inspect wheel/sdist metadata and install the wheel into a separate environment to confirm
the installed mcp-proxy-resilient entry point works independently of the source checkout.
If code or build inputs change, rebuild and rerun artifact tests before releasing.

CI tests Windows/Ubuntu with Python 3.10 and 3.13. Report Linux/macOS validation truthfully:
a configured job, a successful cross-build, and a successful runtime test are different evidence.
macOS is experimental; no macOS/Linux standalone binary is part of this first release.

## Repository setup and publication

The repository URL is a planned publication target until it has actually been created.
An existing upstream clone should retain the original remote as upstream and use the new
repository as origin. Inspect git remote -v before changing either:

~~~shell
git remote rename origin upstream
git remote add origin https://github.com/liang-chang/mcp-proxy-resilient.git
~~~

Run these setup commands only when origin still points to sparfenyuk/mcp-proxy and upstream
does not already exist. Repository creation, commits, pushes, tags, and release publication
are explicit maintainer steps, separate from preparing local changes.

After the reviewed commit is on main and publication is authorized:

~~~powershell
git push -u origin main
# Wait for CI on that exact main commit before tagging.
$version = uv run --locked python -c 'from mcp_proxy import __version__; print(__version__)'
git tag -a "v$version" -m "mcp-proxy-resilient $version"
git push origin "refs/tags/v$version"
~~~

.github/workflows/release.yml reacts to v* tags and first calls the reusable CI workflow
from the same commit. It validates the tag against the dynamic package version, builds
and tests the relocated Windows ZIP, builds wheel/sdist, stages checksums, then publishes
a GitHub Release using the repository-scoped GITHUB_TOKEN. Only the publish job has
contents: write. No PyPI or container-registry credentials are needed.

Manual workflow_dispatch on a branch runs validation/build and uploads workflow artifacts;
only a tag ref can reach the publish job. Artifact and Actions failure messages must be
checked against the intended commit SHA. If a Release already exists, inspect it before
retrying; the workflow does not silently overwrite an existing release.

## After publication

- Confirm the tag points at the reviewed commit and all required jobs passed.
- Download the published ZIP and checksums, verify them, and run --version from the download.
- Confirm the wheel metadata, package version, tag, ZIP name, and release notes agree.
- Change the changelog entry from unreleased when the release is actually published.
- GitHub About suggestion: A recoverable MCP proxy for stdio and HTTP, with shared discovery and reconnection.

Workflow references: [reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows),
[workflow artifacts](https://docs.github.com/en/actions/tutorials/store-and-share-data),
[gh release create](https://cli.github.com/manual/gh_release_create).
