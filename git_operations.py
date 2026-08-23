"""Git worktree operations for downloaded submissions.

This module deliberately uses :mod:`pygit2` for every repository operation so
the crawler does not depend on a Git executable being present on the host.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path
from typing import Sequence

import pygit2

from errors import CrawlerError


PYGIT2_USERNAME_ENV = "PYGIT2_USERNAME"
PYGIT2_PASSWORD_ENV = "PYGIT2_PASSWORD"
PYGIT2_SSH_KEY_PATH_ENV = "PYGIT2_SSH_KEY_PATH"
PYGIT2_SSH_PUBLIC_KEY_PATH_ENV = "PYGIT2_SSH_PUBLIC_KEY_PATH"
PYGIT2_SSH_PASSPHRASE_ENV = "PYGIT2_SSH_PASSPHRASE"


def open_submission_repository(
    path: Path, *, push_requested: bool
) -> pygit2.Repository | None:
    """Open ``path`` only when it is itself a non-bare Git worktree."""
    repository: pygit2.Repository | None = None
    if path.is_dir():
        try:
            repository_path = pygit2.discover_repository(str(path))
            if repository_path is not None:
                candidate = pygit2.Repository(repository_path)
                if candidate.workdir is not None and (
                    Path(candidate.workdir).resolve() == path.resolve()
                ):
                    repository = candidate
        except (OSError, pygit2.GitError):
            pass

    if repository is None:
        push_detail = " The requested push is unavailable." if push_requested else ""
        print(
            f"Warning: submission folder {path} is not a Git repository; "
            f"Git commit was skipped.{push_detail}",
            file=sys.stderr,
        )
    return repository


def _relative_submission_files(
    repository: pygit2.Repository, updated_files: Sequence[Path]
) -> list[str]:
    if repository.workdir is None:
        raise CrawlerError("Git repository has no working directory")

    workdir = Path(repository.workdir).resolve()
    relative_files: set[str] = set()
    for path in updated_files:
        try:
            relative_files.add(
                path.resolve().relative_to(workdir).as_posix()
            )
        except ValueError as exc:
            raise CrawlerError(
                f"Updated submission {path} is outside the Git repository"
            ) from exc
    return sorted(relative_files)


def _hooks_path(repository: pygit2.Repository) -> Path:
    """Return the effective hooks directory, including ``core.hooksPath``."""
    try:
        configured_path = str(repository.config["core.hooksPath"]).strip()
    except KeyError:
        return Path(repository.path) / "hooks"
    except (ValueError, pygit2.GitError) as exc:
        raise CrawlerError(
            f"Git hooks configuration is invalid in the submission repository: {exc}"
        ) from exc

    if not configured_path:
        return Path(repository.path) / "hooks"

    hooks_path = Path(configured_path).expanduser()
    if not hooks_path.is_absolute():
        base_path = (
            Path(repository.workdir)
            if repository.workdir is not None
            else Path(repository.path)
        )
        hooks_path = base_path / hooks_path
    return hooks_path


def _configured_hook(
    repository: pygit2.Repository, hook_names: Sequence[str]
) -> str | None:
    hooks_path = _hooks_path(repository)
    for hook_name in hook_names:
        if (hooks_path / hook_name).is_file():
            return hook_name
    return None


def _repository_operation_is_active(repository: pygit2.Repository) -> bool:
    """Whether Git has an in-progress operation that owns the next commit."""
    try:
        state = repository.state
        if callable(state):
            state = state()
        return state != pygit2.enums.RepositoryState.NONE
    except pygit2.GitError as exc:
        raise CrawlerError(
            f"Git could not inspect the submission repository state: {exc}"
        ) from exc


def _commit_signing_is_enabled(repository: pygit2.Repository) -> bool:
    try:
        return repository.config.get_bool("commit.gpgSign")
    except KeyError:
        return False
    except (ValueError, pygit2.GitError) as exc:
        raise CrawlerError(
            "Git commit signing configuration is invalid in the submission "
            f"repository: {exc}"
        ) from exc


def git_commit(
    repository: pygit2.Repository, updated_files: Sequence[Path]
) -> bool:
    """Commit only the supplied submission files, preserving other staging."""
    if not updated_files:
        return False
    if _repository_operation_is_active(repository):
        raise CrawlerError(
            "Git commit cannot create a partial commit while the repository has "
            "an active merge, rebase, or other operation; finish or abort it first."
        )

    relative_files = _relative_submission_files(repository, updated_files)
    try:
        ignored_files = [
            path for path in relative_files if repository.path_is_ignored(path)
        ]
    except pygit2.GitError as exc:
        raise CrawlerError(f"Git could not inspect ignored files: {exc}") from exc
    if ignored_files:
        raise CrawlerError(
            "Git could not stage the updated submissions because they are "
            "ignored by repository rules: " + ", ".join(ignored_files)
        )

    hook_name = _configured_hook(repository, ("pre-commit", "commit-msg"))
    if hook_name is not None:
        raise CrawlerError(
            f"Git commit cannot run the configured {hook_name} hook with "
            "pygit2; commit manually or remove the hook."
        )
    if _commit_signing_is_enabled(repository):
        raise CrawlerError(
            "Git commit signing is enabled, but pygit2 cannot create a "
            "signed commit; commit manually or disable commit.gpgSign."
        )

    try:
        worktree_index = repository.index
        for path in relative_files:
            worktree_index.add(path)
        worktree_index.write()

        commit_index = pygit2.Index()
        parents: list[pygit2.Oid] = []
        head_commit: pygit2.Commit | None = None
        if not repository.head_is_unborn:
            head_commit = repository[repository.head.target]
            commit_index.read_tree(head_commit.tree)
            parents.append(head_commit.id)
        for path in relative_files:
            commit_index.add(worktree_index[path])
        tree_id = commit_index.write_tree(repository)
    except (KeyError, pygit2.GitError) as exc:
        raise CrawlerError(f"Git could not stage the updated submissions: {exc}") from exc

    if head_commit is not None and tree_id == head_commit.tree.id:
        return False

    count = len(relative_files)
    title = f"Update {count} answer" if count == 1 else f"Update {count} answers"
    body = "Updated files:\n" + "\n".join(relative_files)
    try:
        signature = repository.default_signature
    except (KeyError, pygit2.GitError) as exc:
        raise CrawlerError(
            "Git commit failed: configure user.name and user.email in the "
            "submission repository"
        ) from exc
    try:
        repository.create_commit(
            "HEAD", signature, signature, f"{title}\n\n{body}", tree_id, parents
        )
    except pygit2.GitError as exc:
        raise CrawlerError(f"Git commit failed: {exc}") from exc
    return True


def _branch_upstream(branch: pygit2.Branch) -> pygit2.Branch | None:
    try:
        return branch.upstream
    except (KeyError, ValueError, pygit2.GitError):
        return None


def _configured_remote(
    repository: pygit2.Repository, config_key: str
) -> pygit2.Remote | None:
    try:
        remote_name = str(repository.config[config_key]).strip()
    except KeyError:
        return None
    if not remote_name:
        return None
    try:
        return repository.remotes[remote_name]
    except KeyError as exc:
        raise CrawlerError(
            f"Git push failed: configured remote {remote_name!r} does not exist"
        ) from exc


def _push_remote(
    repository: pygit2.Repository, branch: pygit2.Branch
) -> tuple[pygit2.Remote, pygit2.Branch | None] | None:
    upstream = _branch_upstream(branch)

    for config_key in (
        f"branch.{branch.branch_name}.pushRemote",
        "remote.pushDefault",
        f"branch.{branch.branch_name}.remote",
    ):
        remote = _configured_remote(repository, config_key)
        if remote is not None:
            return remote, upstream

    if upstream is not None:
        try:
            return repository.remotes[upstream.remote_name], upstream
        except (KeyError, ValueError):
            pass

    try:
        return repository.remotes["origin"], upstream
    except KeyError:
        remotes = list(repository.remotes)
        return (remotes[0], upstream) if len(remotes) == 1 else None


def _push_urls(repository: pygit2.Repository, remote: pygit2.Remote) -> list[str]:
    try:
        push_urls = list(
            repository.config.get_multivar(f"remote.{remote.name}.pushurl")
        )
    except KeyError:
        push_urls = []
    return push_urls or [remote.url]


def _upstream_destination(
    remote: pygit2.Remote, upstream: pygit2.Branch
) -> str:
    full_prefix = f"refs/remotes/{remote.name}/"
    if upstream.name.startswith(full_prefix):
        return upstream.name.removeprefix(full_prefix)

    short_prefix = f"{remote.name}/"
    if upstream.branch_name.startswith(short_prefix):
        return upstream.branch_name.removeprefix(short_prefix)
    return upstream.branch_name


def _push_default(repository: pygit2.Repository) -> str:
    try:
        value = str(repository.config["push.default"]).strip().lower()
    except KeyError:
        return "simple"
    return value or "simple"


def _push_auto_setup_remote(repository: pygit2.Repository) -> bool:
    try:
        return repository.config.get_bool("push.autoSetupRemote")
    except KeyError:
        return False
    except (ValueError, pygit2.GitError) as exc:
        raise CrawlerError(
            f"Git push auto-setup configuration is invalid: {exc}"
        ) from exc


def _implicit_push_refspecs(
    repository: pygit2.Repository,
    branch: pygit2.Branch,
    remote: pygit2.Remote,
    upstream: pygit2.Branch | None,
) -> tuple[list[str], str, bool]:
    push_default = _push_default(repository)
    target_branch = branch.branch_name
    upstream_matches_remote = (
        upstream is not None and upstream.remote_name == remote.name
    )

    if push_default == "nothing":
        raise CrawlerError(
            "Git push was requested, but push.default is 'nothing'; push skipped."
        )
    if push_default == "matching":
        raise CrawlerError(
            "Git push with push.default='matching' is unsupported by the "
            "pygit2 crawler; configure remote.<name>.push refspecs instead."
        )
    if push_default not in {"simple", "current", "upstream", "tracking"}:
        raise CrawlerError(f"Git push has unsupported push.default={push_default!r}")

    if push_default in {"upstream", "tracking"}:
        if not upstream_matches_remote or upstream is None:
            raise CrawlerError(
                "Git push with push.default='upstream' requires an upstream "
                "on the selected push remote."
            )
        target_branch = _upstream_destination(remote, upstream)
    elif upstream_matches_remote and upstream is not None:
        upstream_branch = _upstream_destination(remote, upstream)
        if push_default == "simple":
            if upstream_branch != branch.branch_name:
                raise CrawlerError(
                    "Git push refused because the checked-out branch "
                    f"{branch.branch_name!r} tracks {remote.name}/{upstream_branch} "
                    "with a different name. Configure an explicit "
                    "remote.<name>.push refspec or push.default='upstream' to "
                    "allow this mapping."
                )
            target_branch = upstream_branch

    return (
        [f"{branch.name}:refs/heads/{target_branch}"],
        target_branch,
        upstream is None
        and (push_default != "current" or _push_auto_setup_remote(repository)),
    )


def _push_refspecs(
    repository: pygit2.Repository,
    branch: pygit2.Branch,
    remote: pygit2.Remote,
    upstream: pygit2.Branch | None,
) -> tuple[list[str], str | None, bool]:
    configured_refspecs = list(remote.push_refspecs)
    if configured_refspecs:
        return configured_refspecs, None, False
    return _implicit_push_refspecs(repository, branch, remote, upstream)


def _remote_is_mirror(repository: pygit2.Repository, remote: pygit2.Remote) -> bool:
    try:
        return repository.config.get_bool(f"remote.{remote.name}.mirror")
    except KeyError:
        return False
    except (ValueError, pygit2.GitError) as exc:
        raise CrawlerError(
            f"Git mirror configuration for remote {remote.name!r} is invalid: {exc}"
        ) from exc


def _push_transport(
    repository: pygit2.Repository, remote: pygit2.Remote, url: str | None = None
) -> pygit2.Remote:
    """Create a transport that targets exactly one requested push URL."""
    target_url = url or remote.push_url or remote.url
    return repository.remotes.create_anonymous(target_url)


class _PushCallbacks(pygit2.RemoteCallbacks):
    def __init__(self) -> None:
        super().__init__()
        self.rejection: str | None = None
        self.missing_http_credentials = False

    def _ssh_keypair(self, username: str) -> pygit2.Keypair | None:
        private_key_path = os.environ.get(PYGIT2_SSH_KEY_PATH_ENV)
        if not private_key_path:
            return None
        public_key_path = os.environ.get(
            PYGIT2_SSH_PUBLIC_KEY_PATH_ENV, f"{private_key_path}.pub"
        )
        if not Path(public_key_path).is_file():
            return None
        try:
            return pygit2.Keypair(
                username,
                public_key_path,
                private_key_path,
                os.environ.get(PYGIT2_SSH_PASSPHRASE_ENV, ""),
            )
        except (OSError, ValueError, pygit2.GitError):
            return None

    def credentials(
        self, url: str, username_from_url: str | None, allowed_types: int
    ) -> pygit2.Keypair | pygit2.KeypairFromAgent | pygit2.UserPass | pygit2.Username | None:
        username = (
            username_from_url
            or os.environ.get(PYGIT2_USERNAME_ENV)
            or getpass.getuser()
        )
        if allowed_types & pygit2.enums.CredentialType.USERPASS_PLAINTEXT:
            password = os.environ.get(PYGIT2_PASSWORD_ENV)
            if password:
                return pygit2.UserPass(username, password)
            self.missing_http_credentials = True
            return None
        if allowed_types & pygit2.enums.CredentialType.SSH_KEY:
            keypair = self._ssh_keypair(username)
            if keypair is not None:
                return keypair
            return pygit2.KeypairFromAgent(username)
        if allowed_types & pygit2.enums.CredentialType.USERNAME:
            return pygit2.Username(username)
        return None

    def push_update_reference(self, refname: str, message: str | None) -> None:
        if message:
            self.rejection = f"{refname}: {message}"


def git_push(repository: pygit2.Repository, repo_path: Path) -> bool:
    """Push the checked-out branch when this repository has a push target."""
    if repository.head_is_unborn:
        print(
            f"Warning: Git push was requested, but submission folder {repo_path} "
            "has no commits; push skipped.",
            file=sys.stderr,
        )
        return False
    if repository.head_is_detached:
        print(
            f"Warning: Git push was requested, but submission folder {repo_path} "
            "has a detached HEAD; push skipped.",
            file=sys.stderr,
        )
        return False

    branch = repository.branches.get(repository.head.shorthand)
    if branch is None:
        print(
            f"Warning: Git push was requested, but submission folder {repo_path} "
            "has no checked-out branch; push skipped.",
            file=sys.stderr,
        )
        return False

    push_target = _push_remote(repository, branch)
    if push_target is None:
        print(
            f"Warning: Git push was requested, but submission folder {repo_path} "
            "has no configured remote; push skipped.",
            file=sys.stderr,
        )
        return False
    remote, upstream = push_target
    if _remote_is_mirror(repository, remote):
        raise CrawlerError(
            f"Git push cannot honor remote {remote.name!r} mirror setting with "
            "pygit2; configure a non-mirror remote for submissions."
        )

    hook_name = _configured_hook(repository, ("pre-push",))
    if hook_name is not None:
        raise CrawlerError(
            f"Git push cannot run the configured {hook_name} hook with pygit2; "
            "push manually or remove the hook."
        )

    refspecs, target_branch, set_upstream = _push_refspecs(
        repository, branch, remote, upstream
    )
    callbacks = _PushCallbacks()

    try:
        for push_url in _push_urls(repository, remote):
            transport = _push_transport(repository, remote, push_url)
            transport.push(refspecs, callbacks=callbacks)
            if callbacks.rejection is not None:
                raise CrawlerError(f"Git push failed: {callbacks.rejection}")
    except pygit2.GitError as exc:
        if "unsupported url protocol" in str(exc).lower():
            print(
                "Warning: Git push was requested, but pygit2 does not support "
                f"the configured remote URL for submission folder {repo_path}; "
                "push skipped.",
                file=sys.stderr,
            )
            return False
        if callbacks.missing_http_credentials:
            raise CrawlerError(
                "Git push needs HTTPS credentials. Set "
                f"{PYGIT2_USERNAME_ENV} and {PYGIT2_PASSWORD_ENV}, or use an "
                "SSH remote."
            ) from exc
        raise CrawlerError(f"Git push failed: {exc}") from exc

    if set_upstream and target_branch is not None:
        remote_reference = f"refs/remotes/{remote.name}/{target_branch}"
        repository.references.create(
            remote_reference, repository.head.target, force=True
        )
        remote_branch = repository.branches.get(f"{remote.name}/{target_branch}")
        if remote_branch is not None:
            branch.upstream = remote_branch
    return True
