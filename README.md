# LeetCode Crawler

Download the newest accepted solution for every solved LeetCode problem, save
it under `submissions/`, and optionally commit and push the changed files.

## Requirements

- Python 3.10 or newer
- Git (only needed for automatic commits and pushes)
- Chrome, or Patchright's Chromium browser

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m patchright install chromium
```

The last command provides a fallback browser. By default the crawler first uses
the installed Chrome channel through Patchright and keeps its authenticated
session in the ignored `.browser-profile/` directory.

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
python main.py
```

LeetCode may show a CAPTCHA or Turnstile challenge. Complete it in the opened
browser; the crawler waits up to five minutes by default. Patchright removes
Playwright's browser fingerprints, but does not solve or click the challenge.

Useful options:

```powershell
# Verify authentication and all three GraphQL queries without writing anything
python main.py --check --no-push

# Download files without pushing them
python main.py --no-push

# Enter credentials and complete verification manually in the opened browser
python main.py --manual-login --no-push

# CI mode (credentials must be in config.ini or environment variables)
python main.py --headless --non-interactive
```

The checkpoint advances only after every requested solution is downloaded and
the configured Git operation succeeds. A failed run is therefore safe to retry.
