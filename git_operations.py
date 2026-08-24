"""Git worktree operations for downloaded submissions.

The crawler delegates repository policy, including push-target selection, to
the user's installed Git executable.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SUBMISSIONS_PATH = BASE_DIR / "submissions"


class GitOperationError(RuntimeError):
    """A user-actionable failure while using the system Git executable."""


class GitUnavailableError(GitOperationError):
    """Raised when Git cannot be run from the current environment."""


def _warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def _git_is_available() -> bool:
    return shutil.which("git") is not None


def _git_unavailable_message(*, push_requested: bool) -> str:
    operation = "automatic commits and pushes" if push_requested else "automatic commits"
    checkpoint = (
        " The requested push was not attempted, so the checkpoint will not advance."
        if push_requested
        else ""
    )
    return (
        f"Git was not found on PATH, so {operation} are unavailable. "
        f"Install Git and retry; no Git commands were run.{checkpoint}"
    )


def _run_git(
    repo_path: Path,
    arguments: Sequence[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run Git in a worktree and turn command failures into Git errors."""
    if not _git_is_available():
        raise GitUnavailableError(
            "Git was not found on PATH. Install Git and retry."
        )

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise GitUnavailableError(
            "Git could not be started. Install Git and ensure it is available on PATH."
        ) from exc

    if check and result.returncode != 0:
        raise GitOperationError(f"Git command failed: {_command_detail(result)}")
    return result


def _command_detail(result: subprocess.CompletedProcess[str]) -> str:
    detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
    return " ".join(detail.split())


def open_submission_repository(
    submissions_path: Path, *, push_requested: bool
) -> Path | None:
    """Find the Git worktree containing ``submissions_path``.

    ``submissions_path`` may be the worktree itself or any folder inside one.
    """
    if not _git_is_available():
        _warn(_git_unavailable_message(push_requested=push_requested))
        return None

    if not submissions_path.is_dir():
        result = None
    else:
        try:
            result = _run_git(
                submissions_path, ["rev-parse", "--show-toplevel"], check=False
            )
        except GitUnavailableError:
            _warn(_git_unavailable_message(push_requested=push_requested))
            return None

    if result is None or result.returncode != 0:
        push_detail = (
            " The requested push was not attempted, so the checkpoint will not advance."
            if push_requested
            else ""
        )
        _warn(
            f"Submission folder {submissions_path} is not inside a Git worktree. "
            "Initialize or clone a repository that contains this folder; automatic "
            f"commit was skipped.{push_detail}"
        )
        return None

    return Path(result.stdout.strip()).resolve()


def _relative_submission_files(
    repository_path: Path, updated_files: Sequence[Path]
) -> list[str]:
    root = repository_path.resolve()
    relative_files: set[str] = set()
    for path in updated_files:
        try:
            relative_files.add(path.resolve().relative_to(root).as_posix())
        except ValueError as exc:
            raise GitOperationError(
                f"Updated submission {path} is outside the Git worktree {root}"
            ) from exc
    return sorted(relative_files)


def git_commit(repository_path: Path, updated_files: Sequence[Path]) -> bool:
    """Commit only the supplied submissions, preserving other staged changes."""
    if not updated_files:
        return False

    relative_files = _relative_submission_files(repository_path, updated_files)
    _run_git(repository_path, ["add", "--", *relative_files])
    diff = _run_git(
        repository_path,
        ["diff", "--cached", "--quiet", "--", *relative_files],
        check=False,
    )
    if diff.returncode == 0:
        return False
    if diff.returncode != 1:
        raise GitOperationError(
            "Git could not inspect whether the updated submissions are staged"
        )

    count = len(relative_files)
    title = f"Update {count} answer" if count == 1 else f"Update {count} answers"
    body = "Updated files:\n" + "\n".join(relative_files)
    _run_git(
        repository_path,
        ["commit", "--only", "-m", title, "-m", body, "--", *relative_files],
    )
    return True


def _has_no_push_target(detail: str) -> bool:
    normalized = detail.lower()
    return "no configured push destination" in normalized


def _has_no_upstream(detail: str) -> bool:
    normalized = detail.lower()
    return "has no upstream branch" in normalized or "no upstream branch" in normalized


def git_push(repository_path: Path, submissions_path: Path) -> bool:
    """Run bare ``git push`` and leave all push policy to Git configuration."""
    try:
        result = _run_git(repository_path, ["push"], check=False)
    except GitUnavailableError:
        _warn(_git_unavailable_message(push_requested=True))
        return False

    if result.returncode == 0:
        return True

    detail = _command_detail(result)
    if _has_no_push_target(detail):
        _warn(
            f"Git push was requested for submission folder {submissions_path}, but "
            "Git has no configured push target. Configure a remote and its push "
            "target, then retry. The checkpoint was not advanced."
        )
    elif _has_no_upstream(detail):
        _warn(
            f"Git push was requested for submission folder {submissions_path}, but "
            "the current branch has no upstream. Set it yourself with `git push "
            "--set-upstream <remote> <branch>`, or enable `push.autoSetupRemote`; "
            "the crawler will not choose a remote. The checkpoint was not advanced."
        )
    else:
        _warn(
            f"Git push failed for submission folder {submissions_path}: {detail}. "
            "Correct the Git error and retry; the checkpoint was not advanced."
        )
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Push the Git worktree that contains downloaded submissions."
    )
    parser.add_argument(
        "--submissions",
        type=Path,
        default=DEFAULT_SUBMISSIONS_PATH,
        help="directory containing downloaded solutions",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    submissions_path = args.submissions.resolve()
    repository_path = open_submission_repository(
        submissions_path, push_requested=True
    )
    if repository_path is None:
        return 1
    return 0 if git_push(repository_path, submissions_path) else 1


if __name__ == "__main__":
    raise SystemExit(main())
