import ast
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wqb_agent import credentials
from wqb_agent.client import WQBAuthError, WQBClient


class TestCredentialResolver(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.home_file = Path(self.tmp.name) / "brain_credentials.txt"

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write_home(self, text):
        self.home_file.write_text(text, encoding="utf-8")
        if os.name == "posix":
            self.home_file.chmod(0o600)

    def test_complete_environment_pair_wins(self):
        os.environ["WQB_USERNAME"] = "env-user"
        os.environ["WQB_PASSWORD"] = "env-password"
        self.write_home("file-user\nfile-password\n")

        source = credentials.resolve_credentials(
            credentials_file=str(self.home_file)
        )

        self.assertEqual((source.username, source.password),
                         ("env-user", "env-password"))
        self.assertEqual(source.source, "environment")

    def test_environment_values_keep_current_unstripped_behavior(self):
        os.environ["WQB_USERNAME"] = " env-user "
        os.environ["WQB_PASSWORD"] = " env-password "

        source = credentials.resolve_credentials(
            credentials_file=str(self.home_file)
        )

        self.assertEqual((source.username, source.password),
                         (" env-user ", " env-password "))

    def test_partial_environment_fails_without_home_fallback(self):
        self.write_home("file-user\nfile-password\n")

        for key, value in (("WQB_USERNAME", "env-user"), ("WQB_PASSWORD", "env-password")):
            with self.subTest(key=key):
                os.environ[key] = value
                with self.assertRaises(credentials.CredentialError) as ctx:
                    credentials.resolve_credentials(
                        credentials_file=str(self.home_file)
                    )

                self.assertIn("Incomplete WQB credentials in environment", str(ctx.exception))
                self.assertNotIn(value, str(ctx.exception))
                self.assertNotIn("file-password", str(ctx.exception))
                os.environ.pop(key)

    def test_home_file_is_stripped_and_used_when_environment_absent(self):
        self.write_home("  file-user  \n  file-password  \n")

        source = credentials.resolve_credentials(
            credentials_file=str(self.home_file)
        )

        self.assertEqual((source.username, source.password),
                         ("file-user", "file-password"))
        self.assertEqual(source.source, "home credentials file")

    def test_missing_home_file_returns_no_source(self):
        source = credentials.resolve_credentials(
            credentials_file=str(self.home_file)
        )

        self.assertIsNone(source)

    def test_existing_empty_or_one_line_home_file_fails(self):
        for text in ("", "only-user\n"):
            with self.subTest(text=repr(text)):
                self.write_home(text)
                with self.assertRaises(credentials.CredentialError) as ctx:
                    credentials.resolve_credentials(
                        credentials_file=str(self.home_file)
                    )
                self.assertIn("Credential file exists but does not contain", str(ctx.exception))

    def test_unreadable_home_file_fails_instead_of_falling_through(self):
        with mock.patch.object(credentials.os.path, "exists", return_value=True), \
                mock.patch("builtins.open", side_effect=OSError("permission denied")):
            with self.assertRaises(credentials.CredentialError) as ctx:
                credentials.resolve_credentials(
                    credentials_file=str(self.home_file)
                )

        self.assertEqual(str(ctx.exception), "Credential file exists but is unreadable")

    def test_explicit_env_file_is_the_only_dotenv_path(self):
        env_file = Path(self.tmp.name) / "explicit.env"
        env_file.write_text(
            "BRAIN_USERNAME=dotenv-user\nBRAIN_PASSWORD=dotenv-password\n",
            encoding="utf-8",
        )
        if os.name == "posix":
            env_file.chmod(0o600)
        os.environ["WQB_CREDENTIALS_ENV_FILE"] = str(env_file)

        source = credentials.resolve_credentials(
            credentials_file=str(self.home_file)
        )

        self.assertEqual((source.username, source.password),
                         ("dotenv-user", "dotenv-password"))
        self.assertEqual(source.source, "explicit environment file")

    def test_explicit_env_file_missing_or_malformed_fails_closed(self):
        os.environ["WQB_CREDENTIALS_ENV_FILE"] = str(
            Path(self.tmp.name) / "missing.env"
        )
        self.write_home("file-user\nfile-password\n")

        with self.assertRaises(credentials.CredentialError) as ctx:
            credentials.resolve_credentials(
                credentials_file=str(self.home_file)
            )
        self.assertIn("Explicit credential environment file is unreadable", str(ctx.exception))

        env_file = Path(self.tmp.name) / "malformed.env"
        env_file.write_text("WQB_USERNAME=only-user\n", encoding="utf-8")
        if os.name == "posix":
            env_file.chmod(0o600)
        os.environ["WQB_CREDENTIALS_ENV_FILE"] = str(env_file)
        with self.assertRaises(credentials.CredentialError) as ctx:
            credentials.resolve_credentials(
                credentials_file=str(self.home_file)
            )
        self.assertIn("Incomplete credentials in explicit environment file", str(ctx.exception))

    def test_explicit_env_file_path_must_be_absolute(self):
        os.environ["WQB_CREDENTIALS_ENV_FILE"] = ".env"

        with self.assertRaises(credentials.CredentialError) as ctx:
            credentials.resolve_credentials(
                credentials_file=str(self.home_file)
            )

        self.assertEqual(
            str(ctx.exception),
            "Explicit credential environment file path must be absolute",
        )

    @unittest.skipUnless(os.name == "posix", "POSIX permission contract")
    def test_overbroad_permissions_fail_closed(self):
        self.write_home("file-user\nfile-password\n")
        self.home_file.chmod(0o644)
        with self.assertRaisesRegex(credentials.CredentialError, "permissions are unsafe"):
            credentials.resolve_credentials(credentials_file=str(self.home_file))

    @unittest.skipUnless(os.name == "posix", "POSIX symlink contract")
    def test_credential_symlink_fails_closed(self):
        target = Path(self.tmp.name) / "target"
        target.write_text("file-user\nfile-password\n", encoding="utf-8")
        target.chmod(0o600)
        self.home_file.symlink_to(target)
        with self.assertRaisesRegex(credentials.CredentialError, "symbolic link"):
            credentials.resolve_credentials(credentials_file=str(self.home_file))

    def test_implicit_cwd_and_parent_dotenv_files_are_ignored(self):
        cwd_parent = Path(self.tmp.name) / "cwd-parent"
        cwd_child = cwd_parent / "cwd-child"
        cwd_child.mkdir(parents=True)
        (cwd_child / ".env").write_text(
            "WQB_USERNAME=cwd-user\nWQB_PASSWORD=cwd-password\n",
            encoding="utf-8",
        )
        (cwd_parent / ".env").write_text(
            "WQB_USERNAME=parent-user\nWQB_PASSWORD=parent-password\n",
            encoding="utf-8",
        )
        package_parent = Path(self.tmp.name) / "package-parent"
        package_dir = package_parent / "wqb_agent"
        package_dir.mkdir(parents=True)
        (package_dir / ".env").write_text(
            "WQB_USERNAME=package-user\nWQB_PASSWORD=package-password\n",
            encoding="utf-8",
        )
        (package_parent / ".env").write_text(
            "WQB_USERNAME=package-parent-user\nWQB_PASSWORD=package-parent-password\n",
            encoding="utf-8",
        )
        old_cwd = os.getcwd()
        try:
            os.chdir(cwd_child)
            with mock.patch.object(
                credentials, "__file__", str(package_dir / "client.py")
            ):
                source = credentials.resolve_credentials(
                    credentials_file=str(self.home_file)
                )
        finally:
            os.chdir(old_cwd)

        self.assertIsNone(source)

    def test_result_and_errors_never_repr_secrets(self):
        self.write_home("secret-user\nsecret-password\n")
        source = credentials.resolve_credentials(
            credentials_file=str(self.home_file)
        )
        self.assertNotIn("secret-user", repr(source))
        self.assertNotIn("secret-password", repr(source))

        os.environ["WQB_USERNAME"] = "secret-env-user"
        with self.assertRaises(credentials.CredentialError) as ctx:
            credentials.resolve_credentials(
                credentials_file=str(self.home_file)
            )
        self.assertNotIn("secret-env-user", str(ctx.exception))
        self.assertNotIn("secret-password", str(ctx.exception))

    def test_resolver_is_cwd_independent(self):
        self.write_home("file-user\nfile-password\n")
        first_dir = Path(self.tmp.name) / "one"
        second_dir = Path(self.tmp.name) / "two"
        first_dir.mkdir()
        second_dir.mkdir()
        old_cwd = os.getcwd()
        try:
            os.chdir(first_dir)
            first = credentials.resolve_credentials(
                credentials_file=str(self.home_file)
            )
            os.chdir(second_dir)
            second = credentials.resolve_credentials(
                credentials_file=str(self.home_file)
            )
        finally:
            os.chdir(old_cwd)

        self.assertEqual(first, second)


class TestClientCredentialBoundary(unittest.TestCase):
    def test_explicit_complete_pair_does_not_call_resolver(self):
        with mock.patch(
            "wqb_agent.client.resolve_credentials",
            side_effect=AssertionError("explicit credentials invoked discovery"),
        ):
            client = WQBClient(username="explicit-user", password="explicit-password")

        self.assertEqual(client.username, "explicit-user")
        self.assertEqual(client.password, "explicit-password")

    def test_partial_explicit_pair_fails_without_discovery(self):
        with mock.patch(
            "wqb_agent.client.resolve_credentials",
            side_effect=AssertionError("partial credentials invoked discovery"),
        ):
            with self.assertRaises(WQBAuthError) as ctx:
                WQBClient(username="explicit-user", password=None)

        self.assertIn("Incomplete explicit WQB credentials", str(ctx.exception))
        self.assertNotIn("explicit-user", str(ctx.exception))

    def test_client_without_arguments_resolves_once(self):
        source = credentials.CredentialSource(
            "resolved-user", "resolved-password", "test source"
        )
        with mock.patch(
            "wqb_agent.client.resolve_credentials", return_value=source
        ) as resolver:
            client = WQBClient()

        resolver.assert_called_once()
        self.assertEqual((client.username, client.password),
                         ("resolved-user", "resolved-password"))

    def test_no_credentials_error_is_category_only(self):
        with mock.patch("wqb_agent.client.resolve_credentials", return_value=None):
            with self.assertRaises(WQBAuthError) as ctx:
                WQBClient()

        message = str(ctx.exception)
        self.assertIn("No credentials found", message)
        self.assertNotIn("secret", message.lower())


class TestCredentialArchitecture(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]

    def test_credentials_module_is_local_only(self):
        source = (self.root / "wqb_agent" / "credentials.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        for forbidden in ("requests", "Agent", "Simulator", "proposal_execution", "state"):
            self.assertNotIn(forbidden, imports)

    def test_client_no_longer_contains_implicit_dotenv_search(self):
        source = (self.root / "wqb_agent" / "client.py").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            "os.getcwd()",
            "Path.cwd()",
            ".parents",
            "os.path.join(cwd",
            "os.path.join(pkg_dir",
            "_load_from_env_file",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
