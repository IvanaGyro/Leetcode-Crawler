from __future__ import annotations

import argparse
import asyncio
import configparser
import getpass
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from curl_cffi.requests.exceptions import RequestException
import pygit2


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.ini"
DEFAULT_SUBMISSIONS_PATH = BASE_DIR / "submissions"
DEFAULT_BROWSER_PROFILE_PATH = BASE_DIR / ".browser-profile"

LEETCODE_URL = "https://leetcode.com"
LOGIN_URL = f"{LEETCODE_URL}/accounts/login/"
GRAPHQL_URL = f"{LEETCODE_URL}/graphql/"

DATETIME_FORMAT = "%Y%m%d_%H%M%S"

SECTION_RECORD = "Record"
SECTION_USER = "User"
SECTION_GIT = "Git"
SECTION_BROWSER = "Browser"

RECORD_LAST_UPDATE = "LastUpdate"
RECORD_LAST_SUBMISSION_TIMESTAMP = "LastSubmissionTimestamp"
USER_USERNAME = "Username"
USER_PASSWORD = "Password"

ACCEPTED_STATUS = 10
GRAPHQL_PAGE_SIZE = 100
GRAPHQL_RETRIES = 5
LEETCODE_SESSION_COOKIE = "LEETCODE_SESSION"
DEFAULT_CONCURRENT_DOWNLOADS = 8
MAX_CONCURRENT_DOWNLOADS = 32

LOGIN_ERROR_PHRASES = (
    "incorrect username",
    "incorrect password",
    "invalid username",
    "invalid password",
    "username or password",
    "too many login",
    "account is locked",
    "account has been locked",
)

PYGIT2_USERNAME_ENV = "PYGIT2_USERNAME"
PYGIT2_PASSWORD_ENV = "PYGIT2_PASSWORD"
PYGIT2_SSH_KEY_PATH_ENV = "PYGIT2_SSH_KEY_PATH"
PYGIT2_SSH_PUBLIC_KEY_PATH_ENV = "PYGIT2_SSH_PUBLIC_KEY_PATH"
PYGIT2_SSH_PASSPHRASE_ENV = "PYGIT2_SSH_PASSPHRASE"


USER_PROGRESS_QUERY = """
query userProgressQuestionList($filters: UserProgressQuestionListInput) {
  userProgressQuestionList(filters: $filters) {
    questions {
      frontendId
      title
      titleSlug
      lastSubmittedAt
      questionStatus
      lastResult
    }
  }
}
"""

SUBMISSION_LIST_QUERY = """
query submissionList(
  $offset: Int!
  $limit: Int!
  $lastKey: String
  $questionSlug: String!
  $status: Int
) {
  questionSubmissionList(
    offset: $offset
    limit: $limit
    lastKey: $lastKey
    questionSlug: $questionSlug
    status: $status
  ) {
    lastKey
    hasNext
    submissions {
      id
      title
      titleSlug
      statusDisplay
      lang
      timestamp
      url
      frontendId
    }
  }
}
"""

SUBMISSION_DETAILS_QUERY = """
query submissionDetails($submissionId: Int!) {
  submissionDetails(submissionId: $submissionId) {
    code
    timestamp
    statusCode
    lang {
      name
      verboseName
    }
    question {
      questionId
      titleSlug
    }
  }
}
"""


LANGUAGE_EXTENSIONS = {
    "bash": "sh",
    "c": "c",
    "csharp": "cs",
    "cpp": "cpp",
    "dart": "dart",
    "elixir": "ex",
    "erlang": "erl",
    "golang": "go",
    "java": "java",
    "javascript": "js",
    "kotlin": "kt",
    "mysql": "sql",
    "mssql": "sql",
    "oraclesql": "sql",
    "php": "php",
    "python": "py",
    "python3": "py",
    "racket": "rkt",
    "ruby": "rb",
    "rust": "rs",
    "scala": "scala",
    "swift": "swift",
    "typescript": "ts",
}


class CrawlerError(RuntimeError):
    """A user-actionable crawler failure."""


@dataclass(frozen=True)
class SolvedQuestion:
    frontend_id: str
    title: str
    title_slug: str
    last_submitted_at: int | None


@dataclass(frozen=True)
class Submission:
    submission_id: int
    language: str
    timestamp: int


@dataclass(frozen=True)
class Settings:
    config_path: Path
    submissions_path: Path
    browser_profile_path: Path
    username: str
    password: str
    headless: bool
    push: bool
    browser_channel: str | None
    login_timeout_seconds: int
    request_delay_seconds: float
    manual_login: bool
    concurrent_downloads: int = DEFAULT_CONCURRENT_DOWNLOADS


def load_config(path: Path) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.optionxform = str
    if path.is_file():
        config.read(path, encoding="utf-8")

    for section in (SECTION_RECORD, SECTION_USER, SECTION_GIT, SECTION_BROWSER):
        if not config.has_section(section):
            config.add_section(section)
    return config


def parse_timestamp(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"Invalid timestamp: {value!r}")
    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return int(float(text))

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def get_last_update(config: configparser.ConfigParser) -> int:
    raw_timestamp = config.get(
        SECTION_RECORD, RECORD_LAST_SUBMISSION_TIMESTAMP, fallback=""
    ).strip()
    if raw_timestamp:
        try:
            return parse_timestamp(raw_timestamp)
        except ValueError as exc:
            raise CrawlerError(
                f"Invalid [{SECTION_RECORD}] {RECORD_LAST_SUBMISSION_TIMESTAMP}: "
                f"{raw_timestamp!r}"
            ) from exc

    legacy_value = config.get(
        SECTION_RECORD, RECORD_LAST_UPDATE, fallback=""
    ).strip()
    if not legacy_value:
        return 0
    try:
        legacy_datetime = datetime.strptime(legacy_value, DATETIME_FORMAT)
    except ValueError as exc:
        raise CrawlerError(
            f"Invalid [{SECTION_RECORD}] {RECORD_LAST_UPDATE}: {legacy_value!r}"
        ) from exc
    # Preserve the original crawler's local-time interpretation for compatibility.
    local_epoch = datetime.fromtimestamp(0)
    if legacy_datetime <= local_epoch:
        return 0
    try:
        return int(time.mktime(legacy_datetime.timetuple()))
    except (OverflowError, OSError):
        # Some Windows C runtimes reject otherwise valid dates near the epoch.
        return int((legacy_datetime - local_epoch).total_seconds())


def save_checkpoint(
    config: configparser.ConfigParser, path: Path, timestamp: int
) -> None:
    config.set(
        SECTION_RECORD, RECORD_LAST_SUBMISSION_TIMESTAMP, str(int(timestamp))
    )
    config.set(
        SECTION_RECORD,
        RECORD_LAST_UPDATE,
        datetime.fromtimestamp(timestamp).strftime(DATETIME_FORMAT),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as stream:
            config.write(stream)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _config_bool(
    config: configparser.ConfigParser, section: str, option: str, default: bool
) -> bool:
    try:
        return config.getboolean(section, option, fallback=default)
    except ValueError as exc:
        raise CrawlerError(f"[{section}] {option} must be true or false") from exc


def resolve_settings(
    args: argparse.Namespace, config: configparser.ConfigParser
) -> Settings:
    if args.manual_login:
        username = ""
        password = ""
    else:
        username = (
            os.environ.get("LEETCODE_USERNAME")
            or config.get(SECTION_USER, USER_USERNAME, fallback="")
        ).strip()
        password = os.environ.get("LEETCODE_PASSWORD") or config.get(
            SECTION_USER, USER_PASSWORD, fallback=""
        )

        if not username:
            if args.non_interactive:
                raise CrawlerError(
                    "LEETCODE_USERNAME or [User] Username is required in "
                    "non-interactive mode"
                )
            username = input("LeetCode username or email: ").strip()
        if not password:
            if args.non_interactive:
                raise CrawlerError(
                    "LEETCODE_PASSWORD is required in non-interactive mode"
                )
            password = getpass.getpass("LeetCode password: ")
        if not username or not password:
            raise CrawlerError(
                "Both a LeetCode username/email and password are required"
            )

    configured_headless = _config_bool(
        config, SECTION_BROWSER, "Headless", default=False
    )
    configured_push = _config_bool(config, SECTION_GIT, "Push", default=True)
    headless = configured_headless if args.headless is None else args.headless
    push = configured_push if args.push is None else args.push

    channel = args.browser_channel
    if channel is None:
        channel = config.get(
            SECTION_BROWSER, "Channel", fallback="chrome"
        ).strip()
    channel = channel or None

    try:
        login_timeout = config.getint(
            SECTION_BROWSER, "LoginTimeoutSeconds", fallback=300
        )
        request_delay = config.getfloat(
            SECTION_BROWSER, "RequestDelaySeconds", fallback=0.25
        )
        configured_concurrency = config.getint(
            SECTION_BROWSER,
            "ConcurrentDownloads",
            fallback=DEFAULT_CONCURRENT_DOWNLOADS,
        )
    except ValueError as exc:
        raise CrawlerError(
            "Browser timeout, request delay, and concurrency settings must be numeric"
        ) from exc
    concurrency_argument = getattr(args, "concurrency", None)
    concurrency = (
        configured_concurrency
        if concurrency_argument is None
        else concurrency_argument
    )
    if login_timeout < 10:
        raise CrawlerError("LoginTimeoutSeconds must be at least 10")
    if request_delay < 0:
        raise CrawlerError("RequestDelaySeconds cannot be negative")
    if not 1 <= concurrency <= MAX_CONCURRENT_DOWNLOADS:
        raise CrawlerError(
            f"ConcurrentDownloads must be between 1 and {MAX_CONCURRENT_DOWNLOADS}"
        )

    return Settings(
        config_path=args.config.resolve(),
        submissions_path=args.submissions.resolve(),
        browser_profile_path=args.browser_profile.resolve(),
        username=username,
        password=password,
        headless=headless,
        push=push,
        browser_channel=channel,
        login_timeout_seconds=login_timeout,
        request_delay_seconds=request_delay,
        concurrent_downloads=concurrency,
        manual_login=args.manual_login,
    )


def question_from_graphql(raw: Mapping[str, Any]) -> SolvedQuestion:
    slug = str(raw.get("titleSlug") or "").strip()
    if not slug:
        raise CrawlerError("LeetCode returned a solved question without a title slug")

    raw_timestamp = raw.get("lastSubmittedAt")
    timestamp = None if raw_timestamp in (None, "") else parse_timestamp(raw_timestamp)
    return SolvedQuestion(
        frontend_id=str(raw.get("frontendId") or "unknown").strip(),
        title=str(raw.get("title") or slug).strip(),
        title_slug=slug,
        last_submitted_at=timestamp,
    )


def questions_since(
    questions: Iterable[SolvedQuestion], checkpoint: int
) -> tuple[list[SolvedQuestion], int]:
    all_questions = list(questions)
    timestamped = [
        question.last_submitted_at
        for question in all_questions
        if question.last_submitted_at is not None
    ]
    watermark = max(timestamped, default=checkpoint)
    changed = [
        question
        for question in all_questions
        if question.last_submitted_at is None
        or question.last_submitted_at > checkpoint
    ]
    changed.sort(key=lambda question: question.last_submitted_at or 0)
    return changed, watermark


def extension_for_language(language: str) -> str:
    normalized = language.strip().lower()
    if normalized in LANGUAGE_EXTENSIONS:
        return LANGUAGE_EXTENSIONS[normalized]
    fallback = re.sub(r"[^a-z0-9]+", "", normalized)
    return fallback or "txt"


def safe_problem_id(frontend_id: str) -> str:
    value = frontend_id.strip()
    if value.isdigit():
        return f"{int(value):04d}"
    safe_value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-_")
    return safe_value or "unknown"


def submission_filename(
    frontend_id: str, timestamp: int, language: str
) -> str:
    time_string = datetime.fromtimestamp(timestamp).strftime(DATETIME_FORMAT)
    return (
        f"{safe_problem_id(frontend_id)}_{time_string}."
        f"{extension_for_language(language)}"
    )


def write_submission(path: Path, code: str) -> bool:
    encoded_code = code.encode("utf-8")
    if path.is_file() and path.read_bytes() == encoded_code:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp")
    try:
        with temporary_path.open("wb") as stream:
            stream.write(encoded_code)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return True


def _graphql_error_message(payload: Mapping[str, Any]) -> str:
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return "LeetCode returned an invalid GraphQL response"
    messages = [
        str(error.get("message"))
        for error in errors
        if isinstance(error, Mapping) and error.get("message")
    ]
    return "; ".join(messages) or "LeetCode returned a GraphQL error"


class LeetCodeClient:
    def __init__(
        self,
        session: Any,
        session_cookie: str,
        request_delay_seconds: float,
    ) -> None:
        self._session = session
        self._cookies = {LEETCODE_SESSION_COOKIE: session_cookie}
        self._request_delay_seconds = request_delay_seconds
        self._pace_lock = asyncio.Lock()
        self._next_request_at = 0.0

    async def _pace_request(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            async with self._pace_lock:
                now = loop.time()
                wait_seconds = self._next_request_at - now
                if wait_seconds <= 0:
                    self._next_request_at = now + self._request_delay_seconds
                    return
            await asyncio.sleep(wait_seconds)

    async def _defer_requests(self, delay_seconds: float) -> None:
        loop = asyncio.get_running_loop()
        async with self._pace_lock:
            self._next_request_at = max(
                self._next_request_at,
                loop.time() + delay_seconds,
            )

    async def graphql_request(
        self,
        operation_name: str,
        query: str,
        variables: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = {
            "operationName": operation_name,
            "query": query,
            "variables": dict(variables),
        }

        for attempt in range(GRAPHQL_RETRIES):
            await self._pace_request()
            try:
                response = await self._session.post(
                    GRAPHQL_URL,
                    json=payload,
                    cookies=self._cookies,
                    discard_cookies=True,
                )
            except RequestException as exc:
                if attempt == GRAPHQL_RETRIES - 1:
                    raise CrawlerError(
                        f"LeetCode request {operation_name!r} failed after "
                        f"{GRAPHQL_RETRIES} attempts: {exc}"
                    ) from exc
                await asyncio.sleep(min(2**attempt, 30))
                continue

            status = response.status_code
            if status == 429 or 500 <= status < 600:
                if attempt == GRAPHQL_RETRIES - 1:
                    raise CrawlerError(
                        f"LeetCode API returned HTTP {status} after "
                        f"{GRAPHQL_RETRIES} attempts"
                    )
                retry_after = response.headers.get("retry-after", "")
                try:
                    delay = min(float(retry_after), 60.0)
                except ValueError:
                    delay = min(2**attempt, 30)
                delay = max(delay, 0.5)
                if status == 429 or retry_after:
                    await self._defer_requests(delay)
                else:
                    await asyncio.sleep(delay)
                continue
            if status in (401, 403):
                raise CrawlerError(
                    "LeetCode rejected the authenticated API request. "
                    "Sign in again and complete any browser verification."
                )
            if not response.ok:
                raise CrawlerError(f"LeetCode API returned HTTP {status}")

            try:
                result = response.json()
            except (json.JSONDecodeError, ValueError) as exc:
                raise CrawlerError(
                    "LeetCode returned a non-JSON API response; browser verification "
                    "may still be pending"
                ) from exc
            if not isinstance(result, Mapping):
                raise CrawlerError("LeetCode returned an invalid GraphQL response")
            if result.get("errors"):
                raise CrawlerError(_graphql_error_message(result))
            data = result.get("data")
            if not isinstance(data, Mapping):
                raise CrawlerError("LeetCode GraphQL response did not contain data")
            return data

        raise AssertionError("unreachable")


async def fetch_solved_questions(client: LeetCodeClient) -> list[SolvedQuestion]:
    questions: list[SolvedQuestion] = []
    skip = 0
    while True:
        data = await client.graphql_request(
            "userProgressQuestionList",
            USER_PROGRESS_QUERY,
            {
                "filters": {
                    "questionStatus": "SOLVED",
                    "skip": skip,
                    "limit": GRAPHQL_PAGE_SIZE,
                }
            },
        )
        container = data.get("userProgressQuestionList")
        if not isinstance(container, Mapping):
            raise CrawlerError(
                "LeetCode did not return the signed-in user's solved questions"
            )
        raw_questions = container.get("questions")
        if not isinstance(raw_questions, list):
            raise CrawlerError("LeetCode returned an invalid solved-question list")
        page_questions = [
            question_from_graphql(raw)
            for raw in raw_questions
            if isinstance(raw, Mapping)
        ]
        questions.extend(page_questions)
        if len(raw_questions) < GRAPHQL_PAGE_SIZE:
            break
        skip += len(raw_questions)
    return questions


async def fetch_latest_accepted_submission(
    client: LeetCodeClient, title_slug: str
) -> Submission:
    data = await client.graphql_request(
        "submissionList",
        SUBMISSION_LIST_QUERY,
        {
            "questionSlug": title_slug,
            "offset": 0,
            "limit": 1,
            "lastKey": None,
            "status": ACCEPTED_STATUS,
        },
    )
    container = data.get("questionSubmissionList")
    if not isinstance(container, Mapping):
        raise CrawlerError(
            f"LeetCode did not return submissions for {title_slug!r}"
        )
    submissions = container.get("submissions")
    if not isinstance(submissions, list) or not submissions:
        raise CrawlerError(
            f"No accepted submission was returned for {title_slug!r}"
        )
    raw = submissions[0]
    if not isinstance(raw, Mapping):
        raise CrawlerError(f"Invalid submission returned for {title_slug!r}")
    try:
        return Submission(
            submission_id=int(raw["id"]),
            language=str(raw["lang"]),
            timestamp=parse_timestamp(raw["timestamp"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CrawlerError(
            f"Incomplete submission metadata returned for {title_slug!r}"
        ) from exc


async def fetch_submission_code(
    client: LeetCodeClient, submission_id: int
) -> tuple[str, int | None]:
    data = await client.graphql_request(
        "submissionDetails",
        SUBMISSION_DETAILS_QUERY,
        {"submissionId": submission_id},
    )
    details = data.get("submissionDetails")
    if not isinstance(details, Mapping):
        raise CrawlerError(
            f"LeetCode did not return code for submission {submission_id}"
        )
    code = details.get("code")
    if not isinstance(code, str):
        raise CrawlerError(
            f"LeetCode returned invalid code for submission {submission_id}"
        )
    raw_timestamp = details.get("timestamp")
    timestamp = (
        None if raw_timestamp in (None, "") else parse_timestamp(raw_timestamp)
    )
    return code, timestamp


def has_leetcode_session(cookies: Iterable[Mapping[str, Any]]) -> bool:
    return any(
        cookie.get("name") == LEETCODE_SESSION_COOKIE and cookie.get("value")
        for cookie in cookies
    )


def extract_leetcode_session(cookies: Iterable[Mapping[str, Any]]) -> str:
    session_cookie = next(
        (
            str(cookie.get("value"))
            for cookie in cookies
            if cookie.get("name") == LEETCODE_SESSION_COOKIE
            and cookie.get("value")
        ),
        "",
    )
    if not session_cookie:
        raise CrawlerError(
            "LeetCode login completed without an authenticated session cookie"
        )
    return session_cookie


def credential_error(messages: Iterable[str]) -> str | None:
    for message in messages:
        normalized = message.strip().lower()
        if any(phrase in normalized for phrase in LOGIN_ERROR_PHRASES):
            return message.strip()
    return None


def _visible_login_messages(page: Any) -> list[str]:
    return [
        text.strip()
        for text in page.locator("[role='alert']").all_text_contents()
        if text.strip()
    ]


def _turnstile_response(page: Any) -> str:
    response = page.locator("input[name='cf-turnstile-response']")
    if response.count() == 0:
        return ""
    try:
        return response.first.input_value().strip()
    except Exception:
        # The hidden input can be replaced while Cloudflare navigates.
        return ""


def login(page: Any, settings: Settings) -> None:
    if has_leetcode_session(page.context.cookies([LEETCODE_URL])):
        page.goto(LEETCODE_URL, wait_until="domcontentloaded")
        return

    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    if settings.manual_login:
        print(
            "Sign in manually in the browser and complete Cloudflare verification."
        )
        deadline = time.monotonic() + settings.login_timeout_seconds
        while time.monotonic() < deadline:
            cookies = page.context.cookies([LEETCODE_URL])
            if has_leetcode_session(cookies):
                page.goto(LEETCODE_URL, wait_until="domcontentloaded")
                return
            page.wait_for_timeout(500)
        raise CrawlerError(
            "Manual LeetCode login timed out before a session was created."
        )

    username_field = page.locator("#id_login")
    password_field = page.locator("#id_password")
    sign_in_button = page.locator("#signin_btn")

    print("Waiting for LeetCode. Complete any Cloudflare verification shown.")
    try:
        username_field.wait_for(
            state="visible", timeout=settings.login_timeout_seconds * 1_000
        )
    except Exception as exc:
        raise CrawlerError(
            "The LeetCode login form did not load after Cloudflare verification."
        ) from exc

    deadline = time.monotonic() + settings.login_timeout_seconds
    username_field.fill(settings.username)
    password_field.fill(settings.password)

    print(
        "Complete Cloudflare verification; sign-in will continue when the "
        "button is enabled."
    )
    last_messages: list[str] = []
    initial_click_completed = False
    while time.monotonic() < deadline:
        cookies = page.context.cookies([LEETCODE_URL])
        if has_leetcode_session(cookies):
            page.goto(LEETCODE_URL, wait_until="domcontentloaded")
            return

        last_messages = _visible_login_messages(page)
        explicit_error = credential_error(last_messages)
        if explicit_error:
            raise CrawlerError(f"LeetCode login failed: {explicit_error}")

        try:
            button_is_ready = (
                sign_in_button.is_visible()
                and sign_in_button.is_enabled(timeout=1_000)
            )
        except Exception:
            button_is_ready = False
        if button_is_ready:
            try:
                sign_in_button.click(timeout=5_000)
                initial_click_completed = True
                break
            except Exception:
                # The user may have clicked at the same moment and started navigation.
                pass
        page.wait_for_timeout(500)

    if not initial_click_completed:
        cookies = page.context.cookies([LEETCODE_URL])
        if has_leetcode_session(cookies):
            page.goto(LEETCODE_URL, wait_until="domcontentloaded")
            return
        page_path = page.url.split("?", 1)[0]
        message_hint = (
            f" Last page message: {last_messages[0]}" if last_messages else ""
        )
        raise CrawlerError(
            "LeetCode kept the Sign In button disabled or did not complete "
            f"sign-in before the timeout (page={page_path}).{message_hint}"
        )

    print("Signing in. The crawler will resume after verification completes.")
    deadline = time.monotonic() + settings.login_timeout_seconds
    initial_turnstile_response = _turnstile_response(page)
    submission_count = 1
    last_submission_at = time.monotonic()
    form_was_hidden = False
    verification_ready_at: float | None = None

    while time.monotonic() < deadline:
        cookies = page.context.cookies([LEETCODE_URL])
        if has_leetcode_session(cookies):
            page.goto(LEETCODE_URL, wait_until="domcontentloaded")
            return

        try:
            form_is_visible = username_field.is_visible()
        except Exception:
            form_is_visible = False
        if not form_is_visible:
            form_was_hidden = True
            page.wait_for_timeout(500)
            continue

        last_messages = _visible_login_messages(page)
        explicit_error = credential_error(last_messages)
        if explicit_error:
            raise CrawlerError(f"LeetCode login failed: {explicit_error}")

        now = time.monotonic()
        turnstile_response = _turnstile_response(page)
        if (
            turnstile_response
            and turnstile_response != initial_turnstile_response
            and verification_ready_at is None
        ):
            verification_ready_at = now

        try:
            fields_were_cleared = (
                not username_field.input_value() or not password_field.input_value()
            )
        except Exception:
            fields_were_cleared = False

        verification_is_ready = (
            verification_ready_at is not None and now - verification_ready_at >= 1
        )
        should_resubmit = (
            submission_count < 2
            and now - last_submission_at >= 1.5
            and (form_was_hidden or fields_were_cleared or verification_is_ready)
        )
        if should_resubmit:
            username_field.fill(settings.username)
            password_field.fill(settings.password)
            if sign_in_button.is_enabled():
                print("Cloudflare verification completed; resubmitting sign-in once.")
                try:
                    sign_in_button.click(timeout=5_000)
                except Exception:
                    pass
                else:
                    submission_count += 1
                    last_submission_at = time.monotonic()
                    form_was_hidden = False

        page.wait_for_timeout(500)

    mode_hint = (
        "Rerun without --headless so you can complete browser verification."
        if settings.headless
        else "Complete any verification shown in the browser and try again."
    )
    page_path = page.url.split("?", 1)[0]
    message_hint = f" Last page message: {last_messages[0]}" if last_messages else ""
    raise CrawlerError(
        "LeetCode login timed out "
        f"(page={page_path}, sign-in submissions={submission_count}). "
        f"{mode_hint}{message_hint}"
    )


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


def _configured_hook(
    repository: pygit2.Repository, hook_names: Sequence[str]
) -> str | None:
    hooks_path = Path(repository.path) / "hooks"
    for hook_name in hook_names:
        if (hooks_path / hook_name).is_file():
            return hook_name
    return None


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
        upstream is None,
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


def _push_transport(
    repository: pygit2.Repository, remote: pygit2.Remote, url: str | None = None
) -> pygit2.Remote:
    """Return a transport targeting ``url`` without rewriting Git URLs."""
    target_url = url or remote.push_url or remote.url
    if target_url == remote.url:
        return remote
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


async def download_questions(
    client: LeetCodeClient,
    settings: Settings,
    questions: Sequence[SolvedQuestion],
) -> tuple[list[Path], int, int]:
    semaphore = asyncio.Semaphore(settings.concurrent_downloads)
    progress_lock = asyncio.Lock()
    completed_count = 0

    async def download(
        question: SolvedQuestion,
    ) -> tuple[Path, bool, int]:
        nonlocal completed_count
        async with semaphore:
            submission = await fetch_latest_accepted_submission(
                client, question.title_slug
            )
            code, detail_timestamp = await fetch_submission_code(
                client, submission.submission_id
            )
            timestamp = detail_timestamp or submission.timestamp
            filename = submission_filename(
                question.frontend_id, timestamp, submission.language
            )
            path = settings.submissions_path / filename
            updated = write_submission(path, code)

        async with progress_lock:
            completed_count += 1
            print(
                f"[{completed_count}/{len(questions)}] Downloaded "
                f"{question.frontend_id}. {question.title}"
            )
        return path, updated, timestamp

    results = await asyncio.gather(*(download(question) for question in questions))
    submission_files = [path for path, _, _ in results]
    updated_count = sum(updated for _, updated, _ in results)
    watermark = max((timestamp for _, _, timestamp in results), default=0)
    return submission_files, updated_count, watermark


async def crawl(
    client: LeetCodeClient, settings: Settings, checkpoint: int
) -> tuple[list[Path], int, int]:
    solved_questions = await fetch_solved_questions(client)
    changed_questions, watermark = questions_since(solved_questions, checkpoint)
    print(
        f"Found {len(solved_questions)} solved problems; "
        f"{len(changed_questions)} changed since the last successful run."
    )

    submission_files, updated_count, download_watermark = await download_questions(
        client, settings, changed_questions
    )
    return submission_files, updated_count, max(watermark, download_watermark)


async def run_check(client: LeetCodeClient) -> None:
    solved_questions = await fetch_solved_questions(client)
    if not solved_questions:
        print("Authenticated API check succeeded; this account has no solved problems.")
        return
    latest_question = max(
        solved_questions, key=lambda question: question.last_submitted_at or 0
    )
    submission = await fetch_latest_accepted_submission(
        client, latest_question.title_slug
    )
    code, _ = await fetch_submission_code(client, submission.submission_id)
    if not code:
        raise CrawlerError("LeetCode returned an empty solution during the API check")
    print(
        "Authenticated API check succeeded: "
        f"{len(solved_questions)} solved problems found and solution download verified."
    )


async def run_authenticated(
    session_cookie: str,
    settings: Settings,
    checkpoint: int,
    check_only: bool,
) -> tuple[list[Path], int, int] | None:
    from curl_cffi.requests import AsyncSession

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": LEETCODE_URL,
        "Referer": f"{LEETCODE_URL}/",
    }
    async with AsyncSession(
        headers=headers,
        impersonate="chrome",
        max_clients=settings.concurrent_downloads,
        timeout=30,
    ) as session:
        client = LeetCodeClient(
            session,
            session_cookie,
            settings.request_delay_seconds,
        )
        if check_only:
            await run_check(client)
            return None
        return await crawl(client, settings, checkpoint)


def launch_browser_context(playwright: Any, settings: Settings) -> Any:
    options: dict[str, Any] = {
        "user_data_dir": str(settings.browser_profile_path),
        "headless": settings.headless,
        "no_viewport": True,
        "args": ["--start-maximized"],
    }
    if settings.browser_channel:
        options["channel"] = settings.browser_channel
    try:
        return playwright.chromium.launch_persistent_context(**options)
    except Exception as first_error:
        if not settings.browser_channel:
            raise CrawlerError(
                f"Could not launch Chromium: {first_error}"
            ) from first_error
        print(
            f"Browser channel {settings.browser_channel!r} is unavailable; "
            "trying Patchright Chromium."
        )
        options.pop("channel", None)
        try:
            return playwright.chromium.launch_persistent_context(**options)
        except Exception as second_error:
            raise CrawlerError(
                f"Could not launch Patchright Chromium: {second_error}"
            ) from second_error


def execute(args: argparse.Namespace) -> None:
    config = load_config(args.config.resolve())
    settings = resolve_settings(args, config)
    checkpoint = get_last_update(config)

    from patchright.sync_api import Error as BrowserError
    from patchright.sync_api import sync_playwright

    # Patchright removes Playwright's CDP and command-line fingerprint leaks.
    # The persistent Chrome profile is used only to create or refresh the human
    # session. Bulk requests receive only its LEETCODE_SESSION cookie.
    with sync_playwright() as playwright:
        context = launch_browser_context(playwright, settings)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(30_000)
            try:
                login(page, settings)
                session_cookie = extract_leetcode_session(
                    context.cookies([LEETCODE_URL])
                )
            except BrowserError as exc:
                detail = str(exc).strip() or type(exc).__name__
                if "target page, context or browser has been closed" in detail.lower():
                    detail = "The Chrome window was closed before the crawler finished."
                else:
                    detail = f"Browser operation failed: {detail}"
                raise CrawlerError(detail) from exc
        finally:
            context.close()

    result = asyncio.run(
        run_authenticated(session_cookie, settings, checkpoint, args.check)
    )
    if result is None:
        return

    submission_files, updated_count, watermark = result
    repository = open_submission_repository(
        settings.submissions_path, push_requested=settings.push
    )
    if repository is None:
        committed = False
        push_succeeded = not settings.push
    else:
        committed = git_commit(repository, submission_files)
        push_succeeded = not settings.push or git_push(
            repository, settings.submissions_path
        )

    if watermark > checkpoint:
        if push_succeeded:
            save_checkpoint(config, settings.config_path, watermark)
        else:
            print(
                "Git push was skipped, so the checkpoint was not advanced.",
                file=sys.stderr,
            )

    noun = "submission" if updated_count == 1 else "submissions"
    print(f"{updated_count} {noun} updated.")
    if committed:
        print("Created a Git commit for the updated submissions.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download the latest accepted LeetCode solution per solved problem."
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH, help="INI config path"
    )
    parser.add_argument(
        "--submissions",
        type=Path,
        default=DEFAULT_SUBMISSIONS_PATH,
        help="directory where solution files are stored",
    )
    parser.add_argument(
        "--browser-profile",
        type=Path,
        default=DEFAULT_BROWSER_PROFILE_PATH,
        help="persistent Chrome profile used only by this crawler",
    )
    browser_mode = parser.add_mutually_exclusive_group()
    browser_mode.add_argument(
        "--headless", action="store_true", dest="headless", default=None
    )
    browser_mode.add_argument(
        "--headed", action="store_false", dest="headless"
    )
    push_mode = parser.add_mutually_exclusive_group()
    push_mode.add_argument("--push", action="store_true", dest="push", default=None)
    push_mode.add_argument("--no-push", action="store_false", dest="push")
    parser.add_argument(
        "--browser-channel",
        default=None,
        help=(
            "Playwright browser channel (for example chrome); use an empty "
            "value for Chromium"
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help=(
            "maximum concurrent solution downloads "
            f"(1-{MAX_CONCURRENT_DOWNLOADS})"
        ),
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="fail instead of prompting for missing credentials",
    )
    parser.add_argument(
        "--manual-login",
        action="store_true",
        help=(
            "open LeetCode and wait for you to sign in manually; configured "
            "credentials are not read or filled"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "verify login and API access without writing files, Git commits, "
            "or checkpoints"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        execute(args)
    except (CrawlerError, KeyboardInterrupt) as exc:
        message = "Interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)
        print(f"Error: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
