# LeetCode Crawler

Download the newest accepted solution for every solved LeetCode problem, save
it under `submissions/`, and optionally commit and push the changed files.

## Requirements

- [Pixi](https://pixi.prefix.dev/)
- Git for automatic commits and pushes. Install Git and ensure `git` is on
  `PATH` before using those features.
- Chrome, or Patchright's Chromium browser

## Configuration

Copy `config-sample.ini` to `config.ini`, then put your LeetCode username or
email in `[User] Username` and your password in `[User] Password`. For
non-interactive use, `LEETCODE_USERNAME` and `LEETCODE_PASSWORD` environment
variables can be used instead and take precedence over `config.ini`.

For automatic Git commits, initialize or clone a repository that contains
`submissions/`. The folder may be the repository itself or a subfolder of it.

```powershell
# Make submissions/ its own repository
git init submissions

# Or use a repository that contains submissions/
git init my-solutions
```

Configure `user.name` and `user.email` in the repository that receives the
commits. The crawler delegates push selection to Git, so configure remotes and
push settings normally. If a branch has no upstream, set one yourself, for
example with `git push --set-upstream <remote> <branch>`, or enable
`push.autoSetupRemote`. The crawler will not guess a first-push remote.

## Usage

```powershell
pixi run crawl
```

LeetCode may show a CAPTCHA or Turnstile challenge. Complete it in the opened
browser; the crawler waits up to five minutes by default. Patchright removes
Playwright's browser fingerprints, but does not solve or click the challenge.
After login, the browser closes and only the `LEETCODE_SESSION` cookie is handed
to a browser-impersonating `curl-cffi` client. Solution downloads run concurrently
while request starts respect the configured delay.

Useful options:

```powershell
# Verify authentication and all three GraphQL queries without writing anything
pixi run crawl --check --no-push

# Validate up to 50 of the most recent accepted submissions (useful for CI)
pixi run crawl --check 50 --no-push

# Download and commit files locally, without pushing them
pixi run crawl --no-push

# Push a previous no-push run after inspecting it
pixi run push

# Enter credentials and complete verification manually in the opened browser
pixi run crawl --manual-login --no-push

# Override the default eight concurrent solution downloads
pixi run crawl --concurrency 4 --no-push

# CI mode (credentials must be in config.ini or environment variables)
pixi run crawl --headless --non-interactive
```

Running `pixi run crawl --no-push` followed by `pixi run push` has the same
commit-and-push outcome as `pixi run crawl`, while allowing the local commit to
be inspected before it is pushed.

The checkpoint advances only after every requested solution is downloaded. When
pushing is enabled, it advances only after `git push` succeeds. If the
submission folder is not inside a Git worktree, Git is unavailable, no push
target is configured, the branch has no upstream, or the push fails, the crawler
explains the problem and keeps the checkpoint unchanged so the run can be
retried safely. `ConcurrentDownloads` in `[Browser]` accepts values from 1
through 32; `RequestDelaySeconds` is applied globally rather than once per
worker.

## Testing

```powershell
pixi run test
```

The `Crawler validity` GitHub Actions workflow also runs daily and for pull
requests targeting `main`. Pull requests are tested from GitHub's merge ref, so
the checked revision includes the current target-branch commit. The live check
downloads up to 50 recent submissions without writing solutions, committing, or
pushing. Configure `LEETCODE_USERNAME` and `LEETCODE_PASSWORD` as repository
Actions secrets for this job.
