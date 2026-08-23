import configparser
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import main


class TimestampTests(unittest.TestCase):
    def test_parse_timestamp_accepts_epoch_and_iso(self):
        self.assertEqual(main.parse_timestamp("1700000000"), 1700000000)
        self.assertEqual(
            main.parse_timestamp("2024-01-01T00:00:00Z"), 1704067200
        )

    def test_numeric_checkpoint_takes_precedence(self):
        config = main.load_config(Path("does-not-exist.ini"))
        config.set(main.SECTION_RECORD, main.RECORD_LAST_UPDATE, "20200101_000000")
        config.set(
            main.SECTION_RECORD,
            main.RECORD_LAST_SUBMISSION_TIMESTAMP,
            "1700000000",
        )
        self.assertEqual(main.get_last_update(config), 1700000000)

    def test_epoch_legacy_checkpoint_is_supported_on_windows(self):
        config = main.load_config(Path("does-not-exist.ini"))
        config.set(main.SECTION_RECORD, main.RECORD_LAST_UPDATE, "19700101_000000")
        self.assertEqual(main.get_last_update(config), 0)


class SelectionTests(unittest.TestCase):
    def test_only_new_questions_are_selected_and_watermark_is_stable(self):
        questions = [
            main.SolvedQuestion("1", "Old", "old", 100),
            main.SolvedQuestion("2", "Newer", "newer", 300),
            main.SolvedQuestion("3", "New", "new", 200),
        ]
        changed, watermark = main.questions_since(questions, checkpoint=100)
        self.assertEqual(
            [question.title_slug for question in changed], ["new", "newer"]
        )
        self.assertEqual(watermark, 300)

    def test_missing_timestamp_is_retried_instead_of_skipped(self):
        question = main.SolvedQuestion("1", "Unknown", "unknown", None)
        changed, watermark = main.questions_since([question], checkpoint=500)
        self.assertEqual(changed, [question])
        self.assertEqual(watermark, 500)


class CrawlTests(unittest.TestCase):
    def test_retry_includes_an_unchanged_file_for_git_recovery(self):
        timestamp = int(datetime(2024, 1, 2, 3, 4, 5).timestamp())
        question = main.SolvedQuestion("42", "Example", "example", timestamp)
        submission = main.Submission(123, "python3", timestamp)

        with tempfile.TemporaryDirectory() as directory:
            submissions_path = Path(directory)
            path = submissions_path / main.submission_filename(
                question.frontend_id, timestamp, submission.language
            )
            path.write_bytes(b"print('answer')\n")
            settings = main.Settings(
                config_path=Path("config.ini"),
                submissions_path=submissions_path,
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

            with (
                patch("main.fetch_solved_questions", return_value=[question]),
                patch(
                    "main.fetch_latest_accepted_submission",
                    return_value=submission,
                ),
                patch(
                    "main.fetch_submission_code",
                    return_value=("print('answer')\n", timestamp),
                ),
            ):
                files, updated_count, watermark = main.crawl(
                    MagicMock(), settings, checkpoint=0
                )

        self.assertEqual(files, [path])
        self.assertEqual(updated_count, 0)
        self.assertEqual(watermark, timestamp)


class SettingsTests(unittest.TestCase):
    def test_password_is_read_from_config(self):
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

        with patch.dict(main.os.environ, {}, clear=True):
            settings = main.resolve_settings(args, config)

        self.assertEqual(settings.username, "configured-user")
        self.assertEqual(settings.password, "configured-password")

    def test_manual_login_does_not_read_configured_credentials(self):
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

        with patch.dict(main.os.environ, {}, clear=True):
            settings = main.resolve_settings(args, config)

        self.assertEqual(settings.username, "")
        self.assertEqual(settings.password, "")
        self.assertTrue(settings.manual_login)


class LoginTests(unittest.TestCase):
    def test_session_cookie_is_sufficient_even_before_redirect(self):
        cookies = [
            {"name": "csrftoken", "value": "csrf"},
            {"name": "LEETCODE_SESSION", "value": "session"},
        ]
        self.assertTrue(main.has_leetcode_session(cookies))

    def test_cloudflare_messages_are_not_treated_as_credential_errors(self):
        self.assertIsNone(
            main.credential_error(["Verification successful", "Checking your browser"])
        )
        self.assertEqual(
            main.credential_error(["Incorrect username or password"]),
            "Incorrect username or password",
        )

    def test_login_waits_for_cloudflare_to_enable_sign_in(self):
        page = MagicMock()
        page.url = main.LOGIN_URL

        username = MagicMock()
        password = MagicMock()
        button = MagicMock()
        button.is_visible.return_value = True
        button.is_enabled.side_effect = [False, False, True]
        alerts = MagicMock()
        alerts.all_text_contents.return_value = []
        turnstile = MagicMock()
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

        self.assertEqual(button.is_enabled.call_count, 3)
        button.click.assert_called_once_with(timeout=5_000)
        page.goto.assert_any_call(main.LEETCODE_URL, wait_until="domcontentloaded")


class FilenameTests(unittest.TestCase):
    def test_numeric_problem_ids_are_zero_padded(self):
        timestamp = int(datetime(2024, 1, 2, 3, 4, 5).timestamp())
        self.assertEqual(
            main.submission_filename("42", timestamp, "python3"),
            "0042_20240102_030405.py",
        )

    def test_non_numeric_ids_and_unknown_languages_are_safe(self):
        timestamp = int(datetime(2024, 1, 2, 3, 4, 5).timestamp())
        self.assertEqual(
            main.submission_filename("LCR 001", timestamp, "Foo++"),
            "LCR-001_20240102_030405.foo",
        )


class FileTests(unittest.TestCase):
    def test_write_submission_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "answer.py"
            self.assertTrue(main.write_submission(path, "print('ok')\n"))
            self.assertFalse(main.write_submission(path, "print('ok')\n"))
            self.assertEqual(path.read_text(encoding="utf-8"), "print('ok')\n")
            self.assertFalse(path.with_name("answer.py.tmp").exists())

    def test_checkpoint_preserves_configured_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.ini"
            config = main.load_config(path)
            config.set(main.SECTION_USER, main.USER_USERNAME, "example")
            config.set(main.SECTION_USER, main.USER_PASSWORD, "secret")
            main.save_checkpoint(config, path, 1700000000)

            written = configparser.ConfigParser()
            written.optionxform = str
            written.read(path, encoding="utf-8")
            self.assertEqual(
                written.get(main.SECTION_USER, main.USER_PASSWORD), "secret"
            )
            self.assertEqual(
                written.get(
                    main.SECTION_RECORD,
                    main.RECORD_LAST_SUBMISSION_TIMESTAMP,
                ),
                "1700000000",
            )


class GitTests(unittest.TestCase):
    def test_commit_contains_only_updated_submission_files(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)

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

            self.assertTrue(main.git_commit(repo, [answer]))
            committed = git(
                "show", "--pretty=", "--name-only", "HEAD"
            ).stdout.splitlines()
            staged = git("diff", "--cached", "--name-only").stdout.splitlines()
            self.assertEqual(committed, [answer.name])
            self.assertEqual(staged, ["unrelated.txt"])


if __name__ == "__main__":
    unittest.main()
