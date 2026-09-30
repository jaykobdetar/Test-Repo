# Project history

The former Auto Interpretability Lab / Probe-MCP implementation is preserved in
Git at commit `381d4f0b832ca72afa051dec8bf00111e213e642`, immediately before this
simplification. Its documentation and evidence describe that historical project.
They are not current setup instructions, active services or spending approval.

The active project intentionally removes Qwen experiments and model operations,
GPU provisioning and stopping, sandbox/arbitrary execution, scientific
certification, service deployment and image publishing. These are product scope
changes, not claims that equivalent capabilities exist in Agent Workspace.

To inspect an old file without restoring it:

```sh
git show 381d4f0:README.md
git show 381d4f0:STATUS.md
```

If a separate historical checkout is needed, the following creates a detached
worktree without changing the active branch:

```sh
git worktree add --detach ../Test-Repo-history 381d4f0
```

These commands are reference instructions; historical deployment scripts should
not be treated as part of the current workflow. See [current status](../../STATUS.md)
and the [simplification record](../simplification.md).
