import asyncio
import configparser
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import main


def test_parse_timestamp_accepts_epoch_and_iso():
    assert main.parse_timestamp("1700000000") == 1700000000
    assert main.parse_timestamp("2024-01-01T00:00:00Z") == 1704067200


def test_numeric_checkpoint_takes_precedence():
    config = main.load_config(Path("does-not-exist.ini"))
    config.set(main.SECTION_RECORD, main.RECORD_LAST_UPDATE, "20200101_000000")
    config.set(
        main.SECTION_RECORD,
        main.RECORD_LAST_SUBMISSION_TIMESTAMP,
        "1700000000",
    )
    assert main.get_last_update(config) == 1700000000


def test_epoch_legacy_checkpoint_is_supported_on_windows():
    config = main.load_config(Path("does-not-exist.ini"))
    config.set(main.SECTION_RECORD, main.RECORD_LAST_UPDATE, "19700101_000000")
    assert main.get_last_update(config) == 0


def test_only_new_questions_are_selected_and_watermark_is_stable():
    questions = [
        main.SolvedQuestion("1", "Old", "old", 100),
        main.SolvedQuestion("2", "Newer", "newer", 300),
        main.SolvedQuestion("3", "New", "new", 200),
    ]
    changed, watermark = main.questions_since(questions, checkpoint=100)
    assert [question.title_slug for question in changed] == ["new", "newer"]
    assert watermark == 300

def test_missing_timestamp_is_retried_instead_of_skipped():
    question = main.SolvedQuestion("1", "Unknown", "unknown", None)
    changed, watermark = main.questions_since([question], checkpoint=500)
    assert changed == [question]
    assert watermark == 500


def test_retry_includes_an_unchanged_file_for_git_recovery(tmp_path, mocker):
    timestamp = int(datetime(2024, 1, 2, 3, 4, 5).timestamp())
    question = main.SolvedQuestion("42", "Example", "example", timestamp)
    submission = main.Submission(123, "python3", timestamp)
    path = tmp_path / main.submission_filename(
        question.frontend_id, timestamp, submission.language
    )
    path.write_bytes(b"print('answer')\n")
    settings = main.Settings(
        config_path=Path("config.ini"),
        submissions_path=tmp_path,
        browser_profile_path=Path(".browser-profile"),
        username="user",
        password="password",
        headless=True,
        push=False,
        browser_channel="chrome",
        login_timeout_seconds=10,
        request_delay_seconds=0,
        manual_login=False,
    )
    mocker.patch.object(
        main,
        "fetch_solved_questions",
        new=mocker.AsyncMock(return_value=[question]),
    )
    mocker.patch.object(
        main,
        "fetch_latest_accepted_submission",
        new=mocker.AsyncMock(return_value=submission),
    )
    mocker.patch.object(
        main,
        "fetch_submission_code",
        new=mocker.AsyncMock(return_value=("print('answer')\n", timestamp)),
    )

    files, updated_count, watermark = asyncio.run(
        main.crawl(mocker.MagicMock(), settings, checkpoint=0)
    )

    assert files == [path]
    assert updated_count == 0
    assert watermark == timestamp


def test_downloads_use_bounded_concurrency(tmp_path, mocker):
    questions = [
        main.SolvedQuestion(str(index), f"Question {index}", str(index), index)
        for index in range(1, 7)
    ]
    active = 0
    peak = 0

    async def fetch_submission(_client, title_slug):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        return main.Submission(int(title_slug), "python3", 1_700_000_000)

    async def fetch_code(_client, submission_id):
        nonlocal active
        await asyncio.sleep(0.01)
        active -= 1
        return f"print({submission_id})\n", 1_700_000_000 + submission_id

    settings = main.Settings(
        config_path=Path("config.ini"),
        submissions_path=tmp_path,
        browser_profile_path=Path(".browser-profile"),
        username="",
        password="",
        headless=True,
        push=False,
        browser_channel="chrome",
        login_timeout_seconds=10,
        request_delay_seconds=0,
        manual_login=True,
        concurrent_downloads=3,
    )
    mocker.patch.object(
        main, "fetch_latest_accepted_submission", new=fetch_submission
    )
    mocker.patch.object(main, "fetch_submission_code", new=fetch_code)

    files, updated_count, watermark = asyncio.run(
        main.download_questions(mocker.MagicMock(), settings, questions)
    )

    assert len(files) == len(questions)
    assert all(path.is_file() for path in files)
    assert updated_count == len(questions)
    assert watermark == 1_700_000_006
    assert peak == 3


def test_password_is_read_from_config(monkeypatch):
    config = main.load_config(Path("does-not-exist.ini"))
    config.set(main.SECTION_USER, main.USER_USERNAME, "configured-user")
    config.set(main.SECTION_USER, main.USER_PASSWORD, "configured-password")
    args = SimpleNamespace(
        config=Path("config.ini"),
        submissions=Path("submissions"),
        browser_profile=Path(".browser-profile"),
        non_interactive=True,
        headless=None,
        push=None,
        browser_channel=None,
        manual_login=False,
    )
    monkeypatch.delenv("LEETCODE_USERNAME", raising=False)
    monkeypatch.delenv("LEETCODE_PASSWORD", raising=False)

    settings = main.resolve_settings(args, config)

    assert settings.username == "configured-user"
    assert settings.password == "configured-password"


def test_cli_concurrency_overrides_config():
    config = main.load_config(Path("does-not-exist.ini"))
    config.set(main.SECTION_BROWSER, "ConcurrentDownloads", "4")
    args = SimpleNamespace(
        config=Path("config.ini"),
        submissions=Path("submissions"),
        browser_profile=Path(".browser-profile"),
        non_interactive=True,
        headless=None,
        push=False,
        browser_channel=None,
        manual_login=True,
        concurrency=2,
    )

    settings = main.resolve_settings(args, config)

    assert settings.concurrent_downloads == 2


def test_manual_login_does_not_read_configured_credentials(monkeypatch):
    config = main.load_config(Path("does-not-exist.ini"))
    config.set(main.SECTION_USER, main.USER_USERNAME, "configured-user")
    config.set(main.SECTION_USER, main.USER_PASSWORD, "configured-password")
    args = SimpleNamespace(
        config=Path("config.ini"),
        submissions=Path("submissions"),
        browser_profile=Path(".browser-profile"),
        non_interactive=True,
        headless=None,
        push=False,
        browser_channel=None,
        manual_login=True,
    )
    monkeypatch.delenv("LEETCODE_USERNAME", raising=False)
    monkeypatch.delenv("LEETCODE_PASSWORD", raising=False)

    settings = main.resolve_settings(args, config)

    assert settings.username == ""
    assert settings.password == ""
    assert settings.manual_login


def test_session_cookie_is_sufficient_even_before_redirect():
    cookies = [
        {"name": "csrftoken", "value": "csrf"},
        {"name": "LEETCODE_SESSION", "value": "session"},
    ]
    assert main.has_leetcode_session(cookies)


def test_only_session_cookie_is_extracted_for_http_requests():
    cookies = [
        {"name": "csrftoken", "value": "csrf"},
        {"name": "cf_clearance", "value": "clearance"},
        {"name": "LEETCODE_SESSION", "value": "session"},
    ]
    assert main.extract_leetcode_session(cookies) == "session"


def test_missing_session_cookie_is_rejected():
    try:
        main.extract_leetcode_session(
            [{"name": "csrftoken", "value": "csrf"}]
        )
    except main.CrawlerError as exc:
        assert "session cookie" in str(exc)
    else:
        raise AssertionError("missing session cookie was accepted")


def test_cloudflare_messages_are_not_treated_as_credential_errors():
    assert main.credential_error(
        ["Verification successful", "Checking your browser"]
    ) is None
    assert main.credential_error(["Incorrect username or password"]) == (
        "Incorrect username or password"
    )


def test_login_waits_for_cloudflare_to_enable_sign_in(mocker):
    page = mocker.MagicMock()
    page.url = main.LOGIN_URL

    username = mocker.MagicMock()
    password = mocker.MagicMock()
    button = mocker.MagicMock()
    button.is_visible.return_value = True
    button.is_enabled.side_effect = [False, False, True]
    alerts = mocker.MagicMock()
    alerts.all_text_contents.return_value = []
    turnstile = mocker.MagicMock()
    turnstile.count.return_value = 0

    locators = {
        "#id_login": username,
        "#id_password": password,
        "#signin_btn": button,
        "[role='alert']": alerts,
        "input[name='cf-turnstile-response']": turnstile,
    }
    page.locator.side_effect = locators.__getitem__
    page.context.cookies.side_effect = [
        [],
        [],
        [],
        [],
        [{"name": "LEETCODE_SESSION", "value": "session"}],
    ]

    settings = main.Settings(
        config_path=Path("config.ini"),
        submissions_path=Path("submissions"),
        browser_profile_path=Path(".browser-profile"),
        username="user",
        password="password",
        headless=False,
        push=False,
        browser_channel="chrome",
        login_timeout_seconds=10,
        request_delay_seconds=0,
        manual_login=False,
    )

    main.login(page, settings)

    assert button.is_enabled.call_count == 3
    button.click.assert_called_once_with(timeout=5_000)
    page.goto.assert_any_call(main.LEETCODE_URL, wait_until="domcontentloaded")


def _graphql_response(mocker, status, payload, headers=None):
    response = mocker.MagicMock()
    response.status_code = status
    response.ok = 200 <= status < 400
    response.headers = headers or {}
    response.json.return_value = payload
    return response


def test_graphql_request_sends_only_the_session_cookie(mocker):
    session = mocker.MagicMock()
    session.post = mocker.AsyncMock(
        return_value=_graphql_response(
            mocker, 200, {"data": {"answer": 42}}
        )
    )
    client = main.LeetCodeClient(session, "session-value", 0)

    data = asyncio.run(
        client.graphql_request("operation", "query", {"id": 1})
    )

    assert data == {"answer": 42}
    request = session.post.await_args
    assert request.kwargs["cookies"] == {
        main.LEETCODE_SESSION_COOKIE: "session-value"
    }
    assert request.kwargs["discard_cookies"]


def test_graphql_request_retries_rate_limits(mocker):
    session = mocker.MagicMock()
    session.post = mocker.AsyncMock(
        side_effect=[
            _graphql_response(
                mocker, 429, {}, {"retry-after": "0"}
            ),
            _graphql_response(mocker, 200, {"data": {"answer": 42}}),
        ]
    )
    client = main.LeetCodeClient(session, "session-value", 0)
    sleep = mocker.patch.object(
        main.asyncio, "sleep", new=mocker.AsyncMock()
    )

    data = asyncio.run(client.graphql_request("operation", "query", {}))

    assert data == {"answer": 42}
    assert session.post.await_count == 2
    sleep.assert_awaited_once_with(0.5)


def test_numeric_problem_ids_are_zero_padded():
    timestamp = int(datetime(2024, 1, 2, 3, 4, 5).timestamp())
    assert main.submission_filename("42", timestamp, "python3") == (
        "0042_20240102_030405.py"
    )


def test_non_numeric_ids_and_unknown_languages_are_safe():
    timestamp = int(datetime(2024, 1, 2, 3, 4, 5).timestamp())
    assert main.submission_filename("LCR 001", timestamp, "Foo++") == (
        "LCR-001_20240102_030405.foo"
    )


def test_write_submission_is_atomic_and_idempotent(tmp_path):
    path = tmp_path / "nested" / "answer.py"
    assert main.write_submission(path, "print('ok')\n")
    assert not main.write_submission(path, "print('ok')\n")
    assert path.read_text(encoding="utf-8") == "print('ok')\n"
    assert not path.with_name("answer.py.tmp").exists()


def test_checkpoint_preserves_configured_password(tmp_path):
    path = tmp_path / "config.ini"
    config = main.load_config(path)
    config.set(main.SECTION_USER, main.USER_USERNAME, "example")
    config.set(main.SECTION_USER, main.USER_PASSWORD, "secret")
    main.save_checkpoint(config, path, 1700000000)

    written = configparser.ConfigParser()
    written.optionxform = str
    written.read(path, encoding="utf-8")
    assert written.get(main.SECTION_USER, main.USER_PASSWORD) == "secret"
    assert written.get(
        main.SECTION_RECORD, main.RECORD_LAST_SUBMISSION_TIMESTAMP
    ) == "1700000000"


def test_commit_contains_only_updated_submission_files(tmp_path):
    repo = tmp_path

    def git(*arguments):
        return subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "--quiet")
    git("config", "user.name", "Crawler Test")
    git("config", "user.email", "crawler-test@example.invalid")
    (repo / "baseline.txt").write_text("baseline", encoding="utf-8")
    git("add", "baseline.txt")
    git("commit", "--quiet", "-m", "baseline")

    (repo / "unrelated.txt").write_text("unrelated", encoding="utf-8")
    git("add", "unrelated.txt")
    answer = repo / "0001_20240101_000000.py"
    answer.write_text("answer", encoding="utf-8")

    assert main.git_commit(repo, [answer])
    committed = git("show", "--pretty=", "--name-only", "HEAD").stdout.splitlines()
    staged = git("diff", "--cached", "--name-only").stdout.splitlines()
    assert committed == [answer.name]
    assert staged == ["unrelated.txt"]
