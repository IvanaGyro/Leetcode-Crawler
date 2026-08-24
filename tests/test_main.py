import asyncio
import configparser
import subprocess
import tomllib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import git_operations
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


def test_check_downloads_a_bounded_recent_sample(mocker, capsys):
    questions = [
        main.SolvedQuestion(str(index), f"Question {index}", str(index), index)
        for index in range(1, 76)
    ]
    checked_slugs = []

    async def fetch_submission(_client, title_slug):
        checked_slugs.append(title_slug)
        return main.Submission(int(title_slug), "python3", int(title_slug))

    async def fetch_code(_client, submission_id):
        return f"print({submission_id})\n", submission_id

    mocker.patch.object(
        main,
        "fetch_solved_questions",
        new=mocker.AsyncMock(return_value=questions),
    )
    mocker.patch.object(
        main, "fetch_latest_accepted_submission", new=fetch_submission
    )
    mocker.patch.object(main, "fetch_submission_code", new=fetch_code)

    asyncio.run(
        main.run_check(mocker.MagicMock(), limit=50, concurrent_downloads=4)
    )

    assert set(checked_slugs) == {str(index) for index in range(26, 76)}
    assert len(checked_slugs) == 50
    assert (
        "75 solved problems found and 50 submissions downloaded"
        in capsys.readouterr().out
    )


def test_check_cli_defaults_to_one_submission_and_accepts_a_limit():
    parser = main.build_parser()

    assert parser.parse_args(["--check"]).check == 1
    assert parser.parse_args(["--check", "50"]).check == 50


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


def test_cli_login_timeout_overrides_config():
    config = main.load_config(Path("does-not-exist.ini"))
    config.set(main.SECTION_BROWSER, "LoginTimeoutSeconds", "300")
    args = SimpleNamespace(
        config=Path("config.ini"),
        submissions=Path("submissions"),
        browser_profile=Path(".browser-profile"),
        non_interactive=True,
        headless=None,
        push=False,
        browser_channel=None,
        manual_login=True,
        login_timeout_seconds=30,
    )

    settings = main.resolve_settings(args, config)

    assert settings.login_timeout_seconds == 30


def test_proxy_list_is_read_from_one_environment_variable(monkeypatch):
    monkeypatch.setenv(
        "LEETCODE_PROXY_LIST",
        "proxy-one.example:3128:user-one:password-one\n"
        "proxy-two.example:8080:user-two:password:with:colons",
    )

    proxies = main.resolve_proxy_settings()

    assert proxies == (
        main.ProxySettings(
            server="http://proxy-one.example:3128",
            username="user-one",
            password="password-one",
        ),
        main.ProxySettings(
            server="http://proxy-two.example:8080",
            username="user-two",
            password="password:with:colons",
        ),
    )


def test_proxy_list_rejects_an_invalid_entry_without_echoing_it(monkeypatch):
    monkeypatch.setenv(
        "LEETCODE_PROXY_LIST",
        "proxy-user:proxy-password@proxy.example:3128",
    )

    try:
        main.resolve_proxy_settings()
    except main.CrawlerError as exc:
        assert "entry 1" in str(exc)
        assert "proxy-password" not in str(exc)
    else:
        raise AssertionError("invalid proxy list entry was accepted")


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


def test_browser_and_api_use_the_same_proxy(mocker):
    proxy = main.ProxySettings(
        server="http://proxy.example:3128",
        username="proxy-user",
        password="proxy-password",
    )
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
        proxy=proxy,
    )
    playwright = mocker.MagicMock()
    browser_context = mocker.MagicMock()
    playwright.chromium.launch_persistent_context.return_value = browser_context

    assert main.launch_browser_context(playwright, settings) is browser_context
    browser_options = playwright.chromium.launch_persistent_context.call_args.kwargs
    assert browser_options["proxy"] == {
        "server": proxy.server,
        "username": proxy.username,
        "password": proxy.password,
    }

    session = mocker.MagicMock()
    session_context = mocker.MagicMock()
    session_context.__aenter__ = mocker.AsyncMock(return_value=session)
    session_context.__aexit__ = mocker.AsyncMock(return_value=None)
    async_session = mocker.patch(
        "curl_cffi.requests.AsyncSession", return_value=session_context
    )
    run_check = mocker.patch.object(
        main, "run_check", new=mocker.AsyncMock(return_value=None)
    )

    result = asyncio.run(
        main.run_authenticated("session-cookie", settings, checkpoint=0, check_limit=1)
    )

    assert result is None
    api_options = async_session.call_args.kwargs
    assert api_options["proxy"] == proxy.server
    assert api_options["proxy_auth"] == (proxy.username, proxy.password)
    run_check.assert_awaited_once()


def test_proxy_login_retries_the_whole_list_for_two_rounds(mocker):
    proxies = (
        main.ProxySettings("http://proxy-one.example:80", "user-one", "password"),
        main.ProxySettings("http://proxy-two.example:80", "user-two", "password"),
    )
    settings = main.Settings(
        config_path=Path("config.ini"),
        submissions_path=Path("submissions"),
        browser_profile_path=Path(".browser-profile"),
        username="user",
        password="password",
        headless=False,
        push=False,
        browser_channel=None,
        login_timeout_seconds=15,
        request_delay_seconds=0,
        manual_login=False,
        proxy_candidates=proxies,
    )
    authenticate = mocker.patch.object(
        main,
        "authenticate_candidate",
        side_effect=[
            main.CrawlerError("form failed"),
            main.CrawlerError("form failed"),
            main.CrawlerError("form failed"),
            "session-cookie",
        ],
    )

    session_cookie, selected = main.authenticate_with_proxies(
        mocker.MagicMock(), settings
    )

    assert session_cookie == "session-cookie"
    assert selected.proxy == proxies[1]
    attempted_settings = [call.args[1] for call in authenticate.call_args_list]
    assert [attempt.proxy for attempt in attempted_settings] == [
        proxies[0],
        proxies[1],
        proxies[0],
        proxies[1],
    ]
    assert attempted_settings[0].browser_profile_path == attempted_settings[2].browser_profile_path
    assert attempted_settings[0].browser_profile_path != attempted_settings[1].browser_profile_path


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
    clock = [0.0]
    mocker.patch.object(main.time, "monotonic", side_effect=lambda: clock[0])

    username = mocker.MagicMock()
    username.wait_for.side_effect = lambda **_kwargs: clock.__setitem__(0, 9.0)
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
    page.wait_for_timeout.side_effect = lambda milliseconds: clock.__setitem__(
        0, clock[0] + milliseconds / 1_000
    )
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
    username.wait_for.assert_called_once_with(state="visible", timeout=10_000)
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
    defer_requests = mocker.patch.object(
        client, "_defer_requests", new=mocker.AsyncMock()
    )

    data = asyncio.run(client.graphql_request("operation", "query", {}))

    assert data == {"answer": 42}
    assert session.post.await_count == 2
    defer_requests.assert_awaited_once_with(0.5)


def test_global_cooldown_extends_a_pending_request(mocker):
    async def exercise():
        client = main.LeetCodeClient(mocker.MagicMock(), "session-value", 0.02)
        await client._pace_request()
        pending_request = asyncio.create_task(client._pace_request())
        await asyncio.sleep(0)

        loop = asyncio.get_running_loop()
        cooldown_started = loop.time()
        await client._defer_requests(0.05)
        await pending_request
        return loop.time() - cooldown_started

    assert asyncio.run(exercise()) >= 0.04


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

    assert git_operations.git_commit(repo, [answer])
    committed = git("show", "--pretty=", "--name-only", "HEAD").stdout.splitlines()
    staged = git("diff", "--cached", "--name-only").stdout.splitlines()
    assert committed == [answer.name]
    assert staged == ["unrelated.txt"]


def test_submission_subfolder_uses_containing_git_worktree(tmp_path):
    repository = tmp_path / "repository"
    submissions = repository / "submissions"
    submissions.mkdir(parents=True)

    def git(*arguments):
        return subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "--quiet")
    git("config", "user.name", "Crawler Test")
    git("config", "user.email", "crawler-test@example.invalid")
    answer = submissions / "0001_20240101_000000.py"
    answer.write_text("answer", encoding="utf-8")

    worktree = git_operations.open_submission_repository(
        submissions, push_requested=False
    )

    assert worktree == repository.resolve()
    assert git_operations.git_commit(worktree, [answer])
    assert git("show", "--pretty=", "--name-only", "HEAD").stdout.splitlines() == [
        "submissions/0001_20240101_000000.py"
    ]


def test_missing_git_warns_without_running_a_git_command(tmp_path, capsys, mocker):
    run = mocker.patch.object(git_operations.subprocess, "run")
    mocker.patch.object(git_operations.shutil, "which", return_value=None)

    repository = git_operations.open_submission_repository(
        tmp_path, push_requested=True
    )

    assert repository is None
    run.assert_not_called()
    stderr = capsys.readouterr().err
    assert "Install Git" in stderr
    assert "no Git commands were run" in stderr
    assert "checkpoint will not advance" in stderr


def test_git_errors_are_not_crawler_errors():
    error = git_operations.GitOperationError("Git failed")
    unavailable = git_operations.GitUnavailableError("Git is unavailable")

    assert not isinstance(error, main.CrawlerError)
    assert isinstance(unavailable, git_operations.GitOperationError)
    assert not isinstance(unavailable, main.CrawlerError)


def test_git_command_failure_uses_git_operation_error(tmp_path, mocker):
    mocker.patch.object(git_operations, "_git_is_available", return_value=True)
    mocker.patch.object(
        git_operations.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(
            ["git", "status"], 1, "", "repository error"
        ),
    )

    try:
        git_operations._run_git(tmp_path, ["status"])
    except git_operations.GitOperationError as exc:
        assert "repository error" in str(exc)
    else:
        raise AssertionError("Git command failure was not reported as a Git error")


def test_main_reports_git_operation_errors(capsys, mocker):
    mocker.patch.object(
        main,
        "execute",
        side_effect=git_operations.GitOperationError("Git commit failed"),
    )

    assert main.main([]) == 1
    assert "Error: Git commit failed" in capsys.readouterr().err


def test_non_git_submission_folder_warns_clearly(tmp_path, capsys, mocker):
    mocker.patch.object(git_operations, "_git_is_available", return_value=True)
    mocker.patch.object(
        git_operations,
        "_run_git",
        return_value=subprocess.CompletedProcess(
            ["git", "rev-parse"], 128, "", "not a repository"
        ),
    )

    repository = git_operations.open_submission_repository(
        tmp_path, push_requested=True
    )

    assert repository is None
    stderr = capsys.readouterr().err
    assert "not inside a Git worktree" in stderr
    assert "Initialize or clone" in stderr
    assert "checkpoint will not advance" in stderr


def test_push_runs_bare_git_push_without_remote_precheck(tmp_path, mocker):
    run_git = mocker.patch.object(
        git_operations,
        "_run_git",
        return_value=subprocess.CompletedProcess(["git", "push"], 0, "", ""),
    )

    assert git_operations.git_push(tmp_path, tmp_path)

    run_git.assert_called_once_with(tmp_path, ["push"], check=False)


def test_push_without_target_warns_clearly(tmp_path, capsys, mocker):
    mocker.patch.object(
        git_operations,
        "_run_git",
        return_value=subprocess.CompletedProcess(
            ["git", "push"],
            128,
            "",
            "fatal: No configured push destination.",
        ),
    )

    assert not git_operations.git_push(tmp_path, tmp_path)

    stderr = capsys.readouterr().err
    assert "no configured push target" in stderr
    assert "Configure a remote" in stderr
    assert "checkpoint was not advanced" in stderr


def test_push_without_upstream_does_not_guess_one(tmp_path, capsys, mocker):
    mocker.patch.object(
        git_operations,
        "_run_git",
        return_value=subprocess.CompletedProcess(
            ["git", "push"],
            128,
            "",
            "fatal: The current branch main has no upstream branch.",
        ),
    )

    assert not git_operations.git_push(tmp_path, tmp_path)

    stderr = capsys.readouterr().err
    assert "has no upstream" in stderr
    assert "--set-upstream <remote> <branch>" in stderr
    assert "push.autoSetupRemote" in stderr
    assert "will not choose a remote" in stderr


def test_push_failure_warns_with_git_error(tmp_path, capsys, mocker):
    mocker.patch.object(
        git_operations,
        "_run_git",
        return_value=subprocess.CompletedProcess(
            ["git", "push"], 1, "", "remote rejected the update"
        ),
    )

    assert not git_operations.git_push(tmp_path, tmp_path)

    stderr = capsys.readouterr().err
    assert "Git push failed" in stderr
    assert "remote rejected the update" in stderr
    assert "checkpoint was not advanced" in stderr


def _git_settings(tmp_path, *, push):
    return main.Settings(
        config_path=tmp_path / "config.ini",
        submissions_path=tmp_path / "submissions",
        browser_profile_path=tmp_path / ".browser-profile",
        username="",
        password="",
        headless=True,
        push=push,
        browser_channel="chrome",
        login_timeout_seconds=10,
        request_delay_seconds=0,
        manual_login=True,
    )


def test_failed_requested_push_does_not_advance_checkpoint(
    tmp_path, capsys, mocker
):
    settings = _git_settings(tmp_path, push=True)
    config = main.load_config(settings.config_path)
    mocker.patch.object(main, "open_submission_repository", return_value=tmp_path)
    mocker.patch.object(main, "git_commit", return_value=True)
    mocker.patch.object(main, "git_push", return_value=False)
    save_checkpoint = mocker.patch.object(main, "save_checkpoint")

    committed = main.finalize_download(
        config,
        settings,
        checkpoint=100,
        submission_files=[settings.submissions_path / "answer.py"],
        watermark=101,
    )

    assert committed
    save_checkpoint.assert_not_called()
    assert "checkpoint was not advanced" in capsys.readouterr().err


def test_successful_requested_push_advances_checkpoint(tmp_path, mocker):
    settings = _git_settings(tmp_path, push=True)
    config = main.load_config(settings.config_path)
    mocker.patch.object(main, "open_submission_repository", return_value=tmp_path)
    mocker.patch.object(main, "git_commit", return_value=False)
    mocker.patch.object(main, "git_push", return_value=True)
    save_checkpoint = mocker.patch.object(main, "save_checkpoint")

    main.finalize_download(
        config,
        settings,
        checkpoint=100,
        submission_files=[],
        watermark=101,
    )

    save_checkpoint.assert_called_once_with(config, settings.config_path, 101)


def test_pixi_tasks_support_split_crawl_and_push_workflow():
    project = tomllib.loads(
        (Path(main.__file__).resolve().parent / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    tasks = project["tool"]["pixi"]["tasks"]

    assert "crawl" in tasks
    assert tasks["push"] == "python git_operations.py"
