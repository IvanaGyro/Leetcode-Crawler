# LeetCode Crawler

Download the newest accepted solution for every solved LeetCode problem, save
it under `submissions/`, and optionally commit and push the changed files.

## Requirements

- [Pixi](https://pixi.prefix.dev/)
- Git (only needed for automatic commits and pushes)
- Chrome, or Patchright's Chromium browser

## Configuration

Copy `config-sample.ini` to `config.ini`, then put your LeetCode username or
email in `[User] Username` and your password in `[User] Password`. For
non-interactive use, `LEETCODE_USERNAME` and `LEETCODE_PASSWORD` environment
variables can be used instead and take precedence over `config.ini`.

Create or clone the output repository if you want automatic Git commits:

```powershell
git init submissions
# or: git clone <your-remote-repository> submissions
```

## Usage

```powershell
pixi run crawler
```

LeetCode may show a CAPTCHA or Turnstile challenge. Complete it in the opened
browser; the crawler waits up to five minutes by default. Patchright removes
Playwright's browser fingerprints, but does not solve or click the challenge.

Useful options:

```powershell
# Verify authentication and all three GraphQL queries without writing anything
pixi run crawler --check --no-push

# Download files without pushing them
pixi run crawler --no-push

# Enter credentials and complete verification manually in the opened browser
pixi run crawler --manual-login --no-push

# CI mode (credentials must be in config.ini or environment variables)
pixi run crawler --headless --non-interactive
```

The checkpoint advances only after every requested solution is downloaded and
the configured Git operation succeeds. A failed run is therefore safe to retry.

## Testing

```powershell
pixi run test
```
