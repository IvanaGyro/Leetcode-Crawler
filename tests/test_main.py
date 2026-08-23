import asyncio
import configparser
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import main
import pygit2
import pytest


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


def _create_repository(path: Path) -> pygit2.Repository:
    repository = pygit2.init_repository(str(path))
    repository.config["user.name"] = "Crawler Test"
    repository.config["user.email"] = "crawler-test@example.invalid"
    return repository


def _commit_file(
    repository: pygit2.Repository, path: Path, message: str
) -> pygit2.Oid:
    repository.index.add(path.name)
    repository.index.write()
    signature = repository.default_signature
    parents = [] if repository.head_is_unborn else [repository.head.target]
    return repository.create_commit(
        "HEAD", signature, signature, message, repository.index.write_tree(), parents
    )


def test_commit_contains_only_updated_submission_files(tmp_path):
    repository = _create_repository(tmp_path)
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("baseline", encoding="utf-8")
    _commit_file(repository, baseline, "baseline")

    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("unrelated", encoding="utf-8")
    repository.index.add(unrelated.name)
    repository.index.write()
    answer = tmp_path / "0001_20240101_000000.py"
    answer.write_text("answer", encoding="utf-8")

    assert main.git_commit(repository, [answer])
    committed = repository[repository.head.target]
    changed = repository.diff(committed.parents[0].tree, committed.tree)
    staged = repository.index.diff_to_tree(committed.tree)
    assert [patch.delta.new_file.path for patch in changed] == [answer.name]
    assert [patch.delta.new_file.path for patch in staged] == [unrelated.name]


def test_non_git_submission_folder_warns(tmp_path, capsys):
    _create_repository(tmp_path)
    submissions_path = tmp_path / "submissions"
    submissions_path.mkdir()

    repository = main.open_submission_repository(
        submissions_path, push_requested=True
    )

    assert repository is None
    stderr = capsys.readouterr().err
    assert "Warning: submission folder" in stderr
    assert "is not a Git repository" in stderr
    assert "requested push is unavailable" in stderr


def test_push_without_remote_warns_and_is_skipped(tmp_path, capsys):
    repository = _create_repository(tmp_path)
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("baseline", encoding="utf-8")
    _commit_file(repository, baseline, "baseline")

    assert not main.git_push(repository, tmp_path)

    stderr = capsys.readouterr().err
    assert "Warning: Git push was requested" in stderr
    assert "no configured remote" in stderr


def test_scp_style_remote_preserves_relative_path_semantics(tmp_path):
    repository = _create_repository(tmp_path)
    remote = repository.remotes.create(
        "origin", "git@example.com:owner/submissions.git"
    )

    transport = main._push_transport(repository, remote)

    assert remote.url == "git@example.com:owner/submissions.git"
    assert transport is remote


def test_push_warns_when_pygit2_lacks_remote_protocol_support(
    tmp_path, capsys, mocker
):
    repository = _create_repository(tmp_path)
    answer = tmp_path / "0001_20240101_000000.py"
    answer.write_text("answer", encoding="utf-8")
    _commit_file(repository, answer, "answer")
    repository.remotes.create("origin", "https://example.invalid/repo.git")
    transport = mocker.MagicMock()
    transport.push.side_effect = pygit2.GitError("unsupported URL protocol")
    mocker.patch("main._push_transport", return_value=transport)

    assert not main.git_push(repository, tmp_path)

    stderr = capsys.readouterr().err
    assert "Warning: Git push was requested" in stderr
    assert "pygit2 does not support" in stderr


def test_push_uses_a_configured_remote_and_sets_upstream(tmp_path):
    repo_path = tmp_path / "submissions"
    remote_path = tmp_path / "remote.git"
    repository = _create_repository(repo_path)
    remote_repository = pygit2.init_repository(str(remote_path), bare=True)
    answer = repo_path / "0001_20240101_000000.py"
    answer.write_text("answer", encoding="utf-8")
    _commit_file(repository, answer, "answer")
    branch = repository.branches.get(repository.head.shorthand)
    assert branch is not None
    remote = repository.remotes.create("origin", str(remote_path))

    assert main.git_push(repository, repo_path)

    pushed = remote_repository.references[
        f"refs/heads/{branch.branch_name}"
    ].target
    assert pushed == repository.head.target
    assert branch.upstream_name == f"refs/remotes/{remote.name}/{branch.branch_name}"


def _set_upstream(
    repository: pygit2.Repository,
    remote: pygit2.Remote,
    branch: pygit2.Branch,
    upstream_name: str,
) -> pygit2.Branch:
    repository.references.create(
        f"refs/remotes/{remote.name}/{upstream_name}",
        repository.head.target,
        force=True,
    )
    upstream = repository.branches.get(f"{remote.name}/{upstream_name}")
    assert upstream is not None
    branch.upstream = upstream
    return upstream


def test_push_remote_overrides_take_precedence_over_upstream(tmp_path):
    repository = _create_repository(tmp_path)
    answer = tmp_path / "answer.py"
    answer.write_text("answer", encoding="utf-8")
    _commit_file(repository, answer, "answer")
    branch = repository.branches.get(repository.head.shorthand)
    assert branch is not None
    origin = repository.remotes.create("origin", str(tmp_path / "origin.git"))
    default_remote = repository.remotes.create(
        "default", str(tmp_path / "default.git")
    )
    branch_remote = repository.remotes.create(
        "fork", str(tmp_path / "fork.git")
    )
    _set_upstream(repository, origin, branch, branch.branch_name)

    repository.config["remote.pushDefault"] = default_remote.name
    selected, _ = main._push_remote(repository, branch)
    assert selected.name == default_remote.name

    repository.config[f"branch.{branch.branch_name}.pushRemote"] = branch_remote.name
    selected, _ = main._push_remote(repository, branch)
    assert selected.name == branch_remote.name


def test_push_refuses_mismatched_upstream_branch_by_default(tmp_path):
    repository = _create_repository(tmp_path)
    remote_path = tmp_path / "remote.git"
    pygit2.init_repository(str(remote_path), bare=True)
    answer = tmp_path / "answer.py"
    answer.write_text("answer", encoding="utf-8")
    _commit_file(repository, answer, "answer")
    branch = repository.branches.get(repository.head.shorthand)
    assert branch is not None
    remote = repository.remotes.create("origin", str(remote_path))
    _set_upstream(repository, remote, branch, "deploy")

    with pytest.raises(main.CrawlerError, match="different name"):
        main.git_push(repository, tmp_path)


def test_push_default_upstream_uses_the_remote_tracking_branch_name(tmp_path):
    repository = _create_repository(tmp_path)
    remote_path = tmp_path / "remote.git"
    remote_repository = pygit2.init_repository(str(remote_path), bare=True)
    answer = tmp_path / "answer.py"
    answer.write_text("answer", encoding="utf-8")
    _commit_file(repository, answer, "answer")
    branch = repository.branches.get(repository.head.shorthand)
    assert branch is not None
    remote = repository.remotes.create("origin", str(remote_path))
    _set_upstream(repository, remote, branch, "deploy")
    repository.config["push.default"] = "upstream"

    assert main.git_push(repository, tmp_path)

    assert remote_repository.references["refs/heads/deploy"].target == repository.head.target
    with pytest.raises(KeyError):
        remote_repository.references["refs/heads/origin/deploy"]


def test_push_reuses_an_existing_upstream_without_a_remote_prefix(tmp_path):
    repository = _create_repository(tmp_path)
    remote_path = tmp_path / "remote.git"
    remote_repository = pygit2.init_repository(str(remote_path), bare=True)
    answer = tmp_path / "answer.py"
    answer.write_text("answer", encoding="utf-8")
    _commit_file(repository, answer, "answer")
    branch = repository.branches.get(repository.head.shorthand)
    assert branch is not None
    repository.remotes.create("origin", str(remote_path))

    assert main.git_push(repository, tmp_path)
    answer.write_text("updated answer", encoding="utf-8")
    _commit_file(repository, answer, "updated answer")
    assert main.git_push(repository, tmp_path)

    assert (
        remote_repository.references[f"refs/heads/{branch.branch_name}"].target
        == repository.head.target
    )
    with pytest.raises(KeyError):
        remote_repository.references[f"refs/heads/origin/{branch.branch_name}"]


def test_push_honors_configured_remote_push_refspecs(tmp_path):
    repository = _create_repository(tmp_path)
    remote_path = tmp_path / "remote.git"
    remote_repository = pygit2.init_repository(str(remote_path), bare=True)
    answer = tmp_path / "answer.py"
    answer.write_text("answer", encoding="utf-8")
    _commit_file(repository, answer, "answer")
    branch = repository.branches.get(repository.head.shorthand)
    assert branch is not None
    repository.remotes.create("origin", str(remote_path))
    repository.config["remote.origin.push"] = f"{branch.name}:refs/heads/deploy"

    assert main.git_push(repository, tmp_path)

    assert remote_repository.references["refs/heads/deploy"].target == repository.head.target


def test_push_updates_every_configured_push_url(tmp_path):
    repository = _create_repository(tmp_path)
    first_remote_path = tmp_path / "first.git"
    second_remote_path = tmp_path / "second.git"
    first_remote = pygit2.init_repository(str(first_remote_path), bare=True)
    second_remote = pygit2.init_repository(str(second_remote_path), bare=True)
    answer = tmp_path / "answer.py"
    answer.write_text("answer", encoding="utf-8")
    _commit_file(repository, answer, "answer")
    branch = repository.branches.get(repository.head.shorthand)
    assert branch is not None
    repository.remotes.create("origin", first_remote_path.as_posix())
    repository.config["remote.origin.pushurl"] = first_remote_path.as_posix()
    repository.config.set_multivar(
        "remote.origin.pushurl", "^$", second_remote_path.as_posix()
    )

    assert main.git_push(repository, tmp_path)

    refname = f"refs/heads/{branch.branch_name}"
    assert first_remote.references[refname].target == repository.head.target
    assert second_remote.references[refname].target == repository.head.target


def test_commit_refuses_ignored_submission_files(tmp_path):
    repository = _create_repository(tmp_path)
    (tmp_path / ".gitignore").write_text("*.py\n", encoding="utf-8")
    answer = tmp_path / "answer.py"
    answer.write_text("answer", encoding="utf-8")

    with pytest.raises(main.CrawlerError, match="ignored by repository rules"):
        main.git_commit(repository, [answer])


def test_commit_fails_clearly_when_a_commit_hook_is_configured(tmp_path):
    repository = _create_repository(tmp_path)
    hooks_path = Path(repository.path) / "hooks"
    hooks_path.mkdir(exist_ok=True)
    (hooks_path / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    answer = tmp_path / "answer.py"
    answer.write_text("answer", encoding="utf-8")

    with pytest.raises(main.CrawlerError, match="pre-commit hook"):
        main.git_commit(repository, [answer])


def test_commit_fails_clearly_when_signing_is_required(tmp_path):
    repository = _create_repository(tmp_path)
    repository.config["commit.gpgSign"] = "true"
    answer = tmp_path / "answer.py"
    answer.write_text("answer", encoding="utf-8")

    with pytest.raises(main.CrawlerError, match="signing is enabled"):
        main.git_commit(repository, [answer])


def test_push_fails_clearly_when_a_pre_push_hook_is_configured(tmp_path):
    repository = _create_repository(tmp_path)
    remote_path = tmp_path / "remote.git"
    pygit2.init_repository(str(remote_path), bare=True)
    answer = tmp_path / "answer.py"
    answer.write_text("answer", encoding="utf-8")
    _commit_file(repository, answer, "answer")
    repository.remotes.create("origin", str(remote_path))
    hooks_path = Path(repository.path) / "hooks"
    hooks_path.mkdir(exist_ok=True)
    (hooks_path / "pre-push").write_text("#!/bin/sh\n", encoding="utf-8")

    with pytest.raises(main.CrawlerError, match="pre-push hook"):
        main.git_push(repository, tmp_path)


def test_push_callback_uses_explicit_https_credentials(monkeypatch, mocker):
    sentinel = object()
    userpass = mocker.patch.object(
        main.pygit2, "UserPass", return_value=sentinel
    )
    monkeypatch.setenv(main.PYGIT2_USERNAME_ENV, "token-user")
    monkeypatch.setenv(main.PYGIT2_PASSWORD_ENV, "token-value")

    credentials = main._PushCallbacks().credentials(
        "https://example.invalid/repository.git",
        None,
        pygit2.enums.CredentialType.USERPASS_PLAINTEXT,
    )

    assert credentials is sentinel
    userpass.assert_called_once_with("token-user", "token-value")


def test_push_callback_uses_an_explicit_ssh_key(tmp_path, monkeypatch, mocker):
    private_key = tmp_path / "id_ed25519"
    public_key = tmp_path / "id_ed25519.pub"
    private_key.write_text("private", encoding="utf-8")
    public_key.write_text("public", encoding="utf-8")
    sentinel = object()
    keypair = mocker.patch.object(
        main.pygit2, "Keypair", return_value=sentinel
    )
    monkeypatch.setenv(main.PYGIT2_SSH_KEY_PATH_ENV, str(private_key))
    monkeypatch.setenv(main.PYGIT2_SSH_PUBLIC_KEY_PATH_ENV, str(public_key))

    credentials = main._PushCallbacks().credentials(
        "ssh://git@example.invalid/repository.git",
        "git",
        pygit2.enums.CredentialType.SSH_KEY,
    )

    assert credentials is sentinel
    keypair.assert_called_once_with("git", str(public_key), str(private_key), "")
