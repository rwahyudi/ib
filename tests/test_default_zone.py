import importlib.machinery
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner


def load_ib_module():
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "ib"
    loader = importlib.machinery.SourceFileLoader("ib_cli_under_test", str(script_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ib = load_ib_module()


class DefaultZoneTests(unittest.TestCase):
    def test_dns_create_uses_configured_default_zone_when_zone_is_omitted(self):
        seen = {}

        class FakeClient:
            def request(self, method, object_type, payload=None):
                seen["request"] = (method, object_type, payload)
                return "record:a/example"

        def fake_create_payload(record_type, value, name, zone, ttl, comment, client):
            seen["zone"] = zone
            return "record:a", {"name": f"{name}.{zone}", "ipv4addr": value}

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "load_config", return_value={"default_zone": "example.com."}):
                with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                    with patch.object(ib, "create_payload", side_effect=fake_create_payload):
                        with patch.object(ib, "read_session_zone", return_value=None):
                            with patch.object(ib, "print_success"), patch.object(ib, "print_note"):
                                ib.run_dns_create("a", "192.0.2.10", "app", None, None, False, None)

        self.assertEqual(seen["zone"], "example.com")
        self.assertEqual(seen["request"][0], "POST")

    def test_dns_create_uses_matching_forward_zone_from_fqdn_name(self):
        seen = {"zone_queries": []}

        class FakeClient:
            view = "corp"

            def request(self, method, object_type, payload=None):
                seen["request"] = (method, object_type, payload)
                return "record:a/example"

        def fake_safe_query(client, object_type, params):
            seen["zone_queries"].append(params["fqdn"])
            if params["fqdn"] == "example-dns.com":
                return [{"fqdn": "example-dns.com", "zone_format": "FORWARD"}]
            return []

        def fake_create_payload(record_type, value, name, zone, ttl, comment, client):
            seen["zone"] = zone
            return "record:a", {"name": name, "ipv4addr": value}

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "load_config", return_value={"default_zone": "fallback.example"}):
                with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                    with patch.object(ib, "safe_query", side_effect=fake_safe_query):
                        with patch.object(ib, "create_payload", side_effect=fake_create_payload):
                            with patch.object(ib, "print_success"), patch.object(ib, "print_note"):
                                ib.run_dns_create(
                                    "a",
                                    "192.0.2.10",
                                    "host1.example-dns.com",
                                    None,
                                    None,
                                    False,
                                    None,
                                )

        self.assertEqual(seen["zone"], "example-dns.com")
        self.assertEqual(
            seen["zone_queries"][:2],
            ["host1.example-dns.com", "example-dns.com"],
        )
        self.assertEqual(seen["request"][0], "POST")

    def test_dns_create_uses_longest_matching_forward_zone_from_fqdn_name(self):
        class FakeClient:
            view = "corp"

        def fake_safe_query(client, object_type, params):
            if params["fqdn"] == "dev.example.com":
                return [{"fqdn": "dev.example.com", "zone_format": "FORWARD"}]
            if params["fqdn"] == "example.com":
                return [{"fqdn": "example.com", "zone_format": "FORWARD"}]
            return []

        with patch.object(ib, "safe_query", side_effect=fake_safe_query):
            zone = ib.resolve_dns_create_zone(
                {"default_zone": "fallback.example"},
                FakeClient(),
                "a",
                "app.dev.example.com",
            )

        self.assertEqual(zone, "dev.example.com")

    def test_dns_create_falls_back_when_fqdn_name_has_no_matching_forward_zone(self):
        class FakeClient:
            view = "corp"

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "read_session_zone", return_value=None):
                with patch.object(ib, "safe_query", return_value=[]):
                    zone = ib.resolve_dns_create_zone(
                        {"default_zone": "fallback.example"},
                        FakeClient(),
                        "a",
                        "host1.unknown.example",
                    )

        self.assertEqual(zone, "fallback.example")

    def test_dns_create_explicit_zone_skips_fqdn_name_zone_lookup(self):
        class FakeClient:
            view = "corp"

        with patch.object(ib, "safe_query") as safe_query:
            zone = ib.resolve_dns_create_zone(
                {"default_zone": "fallback.example"},
                FakeClient(),
                "a",
                "host1.example-dns.com",
                "explicit.example",
            )

        self.assertEqual(zone, "explicit.example")
        safe_query.assert_not_called()

    def test_dns_create_ptr_skips_fqdn_name_zone_lookup(self):
        class FakeClient:
            view = "corp"

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "read_session_zone", return_value=None):
                with patch.object(ib, "safe_query") as safe_query:
                    zone = ib.resolve_dns_create_zone(
                        {"default_zone": "fallback.example"},
                        FakeClient(),
                        "ptr",
                        "host1.example-dns.com",
                    )

        self.assertEqual(zone, "fallback.example")
        safe_query.assert_not_called()

    def test_dns_create_ignores_non_forward_zone_match_for_fqdn_name(self):
        class FakeClient:
            view = "corp"

        def fake_safe_query(client, object_type, params):
            if params["fqdn"] == "example-dns.com":
                return [{"fqdn": "example-dns.com", "zone_format": "IPV4"}]
            return []

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "read_session_zone", return_value=None):
                with patch.object(ib, "safe_query", side_effect=fake_safe_query):
                    zone = ib.resolve_dns_create_zone(
                        {"default_zone": "fallback.example"},
                        FakeClient(),
                        "a",
                        "host1.example-dns.com",
                    )

        self.assertEqual(zone, "fallback.example")

    def test_dns_create_host_payload_uses_ipv4addrs(self):
        class FakeClient:
            view = "corp"

        object_type, payload = ib.create_payload(
            "host",
            "192.0.2.10",
            "app",
            "example.com",
            300,
            "Application host",
            FakeClient(),
        )

        self.assertEqual(object_type, "record:host")
        self.assertEqual(payload["name"], "app.example.com")
        self.assertEqual(payload["view"], "corp")
        self.assertEqual(payload["ipv4addrs"], [{"ipv4addr": "192.0.2.10"}])
        self.assertEqual(payload["ttl"], 300)
        self.assertTrue(payload["use_ttl"])
        self.assertEqual(payload["comment"], "Application host")

    def test_dns_create_host_payload_uses_ipv6addrs(self):
        class FakeClient:
            view = "corp"

        object_type, payload = ib.create_payload(
            "host",
            "2001:db8::10",
            "app",
            "example.com",
            None,
            None,
            FakeClient(),
        )

        self.assertEqual(object_type, "record:host")
        self.assertEqual(payload["ipv6addrs"], [{"ipv6addr": "2001:db8::10"}])
        self.assertNotIn("ipv4addrs", payload)

    def test_dns_create_host_requires_ip_address_value(self):
        class FakeClient:
            view = "corp"

        with self.assertRaisesRegex(ib.CliError, "host value must be an IPv4 or IPv6 address"):
            ib.create_payload("host", "target.example.com", "app", "example.com", None, None, FakeClient())

    def test_dns_create_accepts_short_name_ttl_and_comment_options(self):
        runner = CliRunner()

        with patch.object(ib, "run_dns_create") as run_dns_create:
            result = runner.invoke(
                ib.cli,
                [
                    "dns",
                    "create",
                    "a",
                    "-n",
                    "app",
                    "192.0.2.10",
                    "-t",
                    "300",
                    "-c",
                    "Application VIP",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        run_dns_create.assert_called_once_with(
            "a",
            "192.0.2.10",
            "app",
            None,
            300,
            False,
            "Application VIP",
        )

    def test_dns_create_bash_completion_suggests_short_name_after_type(self):
        runner = CliRunner()

        result = runner.invoke(
            ib.cli,
            [],
            prog_name="ib",
            env={
                "_IB_COMPLETE": "bash_complete",
                "COMP_WORDS": "ib dns create a ",
                "COMP_CWORD": "4",
            },
        )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("plain,-n", result.output.splitlines())
        self.assertIn("plain,--name", result.output.splitlines())

    def test_dns_create_rejects_non_integer_ttl(self):
        runner = CliRunner()

        with patch.object(ib, "run_dns_create") as run_dns_create:
            result = runner.invoke(
                ib.cli,
                ["dns", "create", "a", "--name", "app", "192.0.2.10", "-t", "soon"],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("not a valid integer", result.output)
        run_dns_create.assert_not_called()

    def test_configure_help_explains_repeated_runs_keep_existing_values(self):
        runner = CliRunner()

        result = runner.invoke(ib.cli, ["configure", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("run this command multiple times", result.output)
        self.assertIn("pressing Enter keeps the current value", result.output)
        self.assertIn("password prompt is left blank", result.output)

    def test_dns_create_rejects_negative_ttl_option(self):
        runner = CliRunner()

        with patch.object(ib, "run_dns_create") as run_dns_create:
            result = runner.invoke(
                ib.cli,
                ["dns", "create", "a", "--name", "app", "192.0.2.10", "-t", "-1"],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("ttl must be 0 or greater", result.output)
        run_dns_create.assert_not_called()

    def test_dns_create_rejects_invalid_comment_option(self):
        runner = CliRunner()

        with patch.object(ib, "run_dns_create") as run_dns_create:
            result = runner.invoke(
                ib.cli,
                ["dns", "create", "a", "--name", "app", "192.0.2.10", "-c", "bad|comment"],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("comment may only contain", result.output)
        run_dns_create.assert_not_called()

    def test_dns_create_click_error_help_includes_all_options(self):
        try:
            ib.cli.main(
                args=["dns", "create", "a", "192.0.2.10"],
                prog_name="ib",
                standalone_mode=False,
            )
        except ib.click.ClickException as exc:
            help_text = ib.dns_create_click_error_help(exc)
        else:
            self.fail("expected click error")

        self.assertIsNotNone(help_text)
        self.assertIn("Create Record Usage", help_text)
        self.assertIn("-n, --name TEXT", help_text)
        self.assertIn("--zone TEXT", help_text)
        self.assertIn("-t, --ttl INTEGER", help_text)
        self.assertIn("--noptr", help_text)
        self.assertIn("-c, --comment TEXT", help_text)

    def test_dns_search_missing_keyword_error_prints_current_context(self):
        with patch.object(ib.sys, "argv", ["ib", "dns", "search"]):
            with patch.object(ib, "dns_context_panel", return_value="context") as dns_context_panel:
                with patch.object(ib.err_console, "print") as err_print:
                    exit_code = ib.main()

        self.assertEqual(exit_code, 2)
        dns_context_panel.assert_called_once_with()
        printed_texts = [call.args[0] for call in err_print.call_args_list if call.args]
        error_lines = [
            item
            for item in printed_texts
            if isinstance(item, ib.Text) and "Error:" in item.plain
        ]
        self.assertTrue(error_lines)
        self.assertEqual(str(error_lines[0].style), "white")
        err_print.assert_any_call("context")

    def test_click_error_only_usage_prefix_is_cyan(self):
        try:
            ib.cli.main(args=["dns", "search"], prog_name="ib", standalone_mode=False)
        except ib.click.ClickException as exc:
            with patch.object(ib.err_console, "print") as err_print:
                ib.print_click_exception(exc)
        else:
            self.fail("expected click error")

        printed_texts = [call.args[0] for call in err_print.call_args_list if call.args]
        usage_line = next(item for item in printed_texts if isinstance(item, ib.Text) and "Usage:" in item.plain)
        error_line = next(item for item in printed_texts if isinstance(item, ib.Text) and "Error:" in item.plain)
        self.assertEqual(usage_line.spans[0].start, 0)
        self.assertEqual(usage_line.spans[0].end, len("Usage: "))
        self.assertEqual(str(usage_line.spans[0].style), "cyan")
        self.assertTrue(
            all("cyan" not in str(span.style) for span in usage_line.spans[1:]),
            usage_line.spans,
        )
        self.assertEqual(str(error_line.style), "white")

    def test_dns_create_runtime_error_context_includes_all_options(self):
        class FakeClient:
            view = "corp"

            def request(self, method, object_type, payload=None):
                raise ib.WapiError("ERROR: duplicate record", status=400)

        with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
            with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                with patch.object(
                    ib,
                    "dns_context_status",
                    return_value={
                        "view": "corp",
                        "view_source": "configured",
                        "active_zone": "example.com",
                        "active_zone_source": "configured default",
                        "has_active_zone": True,
                    },
                ):
                    with self.assertRaises(ib.DnsCreateError) as caught:
                        ib.run_dns_create(
                            "a",
                            "192.0.2.10",
                            "app",
                            "example.com",
                            300,
                            True,
                            "Application VIP",
                        )

        context = caught.exception.context
        self.assertEqual(context["record_type"], "a")
        self.assertEqual(context["value"], "192.0.2.10")
        self.assertEqual(context["--name"], "app")
        self.assertEqual(context["--zone"], "example.com")
        self.assertEqual(context["target_record"], "app.example.com")
        self.assertEqual(context["resolved_zone"], "example.com")
        self.assertEqual(context["view"], "corp")
        self.assertEqual(context["active_zone"], "example.com")
        self.assertEqual(context["-t/--ttl"], 300)
        self.assertIs(context["--noptr"], True)
        self.assertEqual(context["-c/--comment"], "Application VIP")
        self.assertEqual(context["wapi_object"], "record:a")

        test_console = ib.Console(width=120, force_terminal=False)
        with test_console.capture() as capture:
            test_console.print(ib.dns_create_error_context_panel(context))
        output = capture.get()
        for expected in (
            "--name",
            "--zone",
            "target record",
            "Context",
            "View=",
            "Zone=",
            "-t/--ttl",
            "--noptr",
            "-c/--comment",
            "-n/--name",
        ):
            self.assertIn(expected, output)

    def test_dns_create_network_association_error_includes_hint(self):
        class FakeClient:
            view = "corp"

            def request(self, method, object_type, payload=None):
                raise ib.WapiError(
                    "ERROR: Infoblox WAPI 400: The IP address 10.1.1.1 cannot be used "
                    "for the zone latrobe-test.edu.au. Verify the network association "
                    "in the zone properties.",
                    status=400,
                )

        with patch.object(ib, "load_config", return_value={"default_zone": "latrobe-test.edu.au"}):
            with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                with patch.object(
                    ib,
                    "dns_context_status",
                    return_value={
                        "view": "corp",
                        "view_source": "configured",
                        "active_zone": "latrobe-test.edu.au",
                        "active_zone_source": "configured default",
                        "has_active_zone": True,
                    },
                ):
                    with self.assertRaises(ib.DnsCreateError) as caught:
                        ib.run_dns_create(
                            "a",
                            "10.1.1.1",
                            "test",
                            None,
                            None,
                            False,
                            None,
                        )

        context = caught.exception.context
        self.assertEqual(context["target_record"], "test.latrobe-test.edu.au")
        self.assertEqual(context["resolved_zone"], "latrobe-test.edu.au")
        self.assertIn("IP address 10.1.1.1", context["hint"])
        self.assertIn("DNS zone latrobe-test.edu.au", context["hint"])
        self.assertIn("--zone or a fully qualified -n name", context["hint"])

        test_console = ib.Console(width=120, force_terminal=False)
        with test_console.capture() as capture:
            test_console.print(ib.dns_create_error_context_panel(context))
        output = capture.get()
        self.assertIn("Hint", output)
        self.assertIn("target record", output)
        self.assertIn("test.latrobe-test.edu.au", output)

    def test_dns_create_rejects_invalid_comment_characters(self):
        class FakeClient:
            view = "corp"

        for comment in ("bad" + chr(233), "bad|comment", "bad\ncomment"):
            with self.subTest(comment=comment):
                with self.assertRaisesRegex(ib.CliError, "comment may only contain"):
                    ib.create_payload(
                        "a",
                        "192.0.2.10",
                        "app",
                        "example.com",
                        None,
                        comment,
                        FakeClient(),
                    )

    def test_dns_create_rejects_non_integer_ttl_in_payload_builder(self):
        class FakeClient:
            view = "corp"

        with self.assertRaisesRegex(ib.CliError, "ttl must be an integer"):
            ib.create_payload(
                "a",
                "192.0.2.10",
                "app",
                "example.com",
                "300",
                None,
                FakeClient(),
            )

    def test_explicit_zone_still_wins(self):
        with patch.dict(os.environ, {ib.DEFAULT_ZONE_ENV: "env.example.com"}, clear=False):
            zone = ib.resolve_dns_zone({"default_zone": "configured.example.com"}, "cli.example.com")

        self.assertEqual(zone, "cli.example.com")

    def test_environment_zone_overrides_configured_default_zone(self):
        with patch.dict(os.environ, {ib.DEFAULT_ZONE_ENV: "env.example.com"}, clear=False):
            with patch.object(ib, "read_session_zone", return_value=None):
                zone = ib.resolve_dns_zone({"default_zone": "configured.example.com"})

        self.assertEqual(zone, "env.example.com")

    def test_session_zone_overrides_environment_and_configured_default_zone(self):
        with patch.dict(os.environ, {ib.DEFAULT_ZONE_ENV: "env.example.com"}, clear=False):
            with patch.object(ib, "read_session_zone", return_value="session.example.com"):
                zone = ib.resolve_dns_zone({"default_zone": "configured.example.com"})

        self.assertEqual(zone, "session.example.com")

    def test_session_zone_is_scoped_to_parent_shell_pid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "session_zone_dir", return_value=Path(tmpdir)):
                with patch.object(ib.os, "getppid", return_value=111):
                    ib.write_session_zone("test.local")
                    self.assertEqual(ib.read_session_zone(), "test.local")
                with patch.object(ib.os, "getppid", return_value=222):
                    self.assertIsNone(ib.read_session_zone())
                    with patch.dict(os.environ, {}, clear=False):
                        os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
                        zone = ib.resolve_dns_zone({"default_zone": "configured.example.com"})

        self.assertEqual(zone, "configured.example.com")

    def test_zone_use_writes_session_zone(self):
        with patch.object(ib, "write_session_zone") as write_session_zone:
            with patch.object(ib, "print_success") as print_success:
                with patch.object(ib, "print_note") as print_note:
                    ib.run_dns_zone_use("test.local.")

        write_session_zone.assert_called_once_with("test.local")
        print_success.assert_called_once()
        print_note.assert_called_once()

    def test_bash_dns_completion_includes_active_zone_status_item(self):
        with patch.dict(
            os.environ,
            {ib.DEFAULT_ZONE_ENV: "example.com", "_IB_COMPLETE": "bash_complete"},
            clear=False,
        ):
            with patch.object(ib, "read_session_zone", return_value=None):
                with ib.dns.make_context("dns", [], resilient_parsing=True) as ctx:
                    items = ib.dns.shell_complete(ctx, "")

        self.assertEqual(items[0].value, "active-zone=example.com")

    def test_dns_completion_help_includes_active_zone_for_description_shells(self):
        with patch.dict(
            os.environ,
            {ib.DEFAULT_ZONE_ENV: "example.com", "_IB_COMPLETE": "zsh_complete"},
            clear=False,
        ):
            with patch.object(ib, "read_session_zone", return_value=None):
                with ib.dns.make_context("dns", [], resilient_parsing=True) as ctx:
                    items = ib.dns.shell_complete(ctx, "")

        create_item = next(item for item in items if item.value == "create")
        self.assertIn("Active zone: example.com (IB_ZONE)", create_item.help)
        self.assertNotIn("active-zone=example.com", [item.value for item in items])

    def test_root_completion_starts_background_search_cache_prewarm_for_empty_completion(self):
        with patch.object(ib, "start_search_cache_prewarm") as start_prewarm:
            with ib.cli.make_context("ib", [], resilient_parsing=True) as ctx:
                items = ib.cli.shell_complete(ctx, "")

        start_prewarm.assert_called_once_with()
        self.assertIn("dns", [item.value for item in items])
        self.assertNotIn("_prewarm-search-cache", [item.value for item in items])

    def test_root_completion_with_incomplete_command_does_not_start_prewarm(self):
        with patch.object(ib, "start_search_cache_prewarm") as start_prewarm:
            with ib.cli.make_context("ib", [], resilient_parsing=True) as ctx:
                items = ib.cli.shell_complete(ctx, "d")

        start_prewarm.assert_not_called()
        self.assertEqual([item.value for item in items], ["dns"])

    def test_start_search_cache_prewarm_runs_hidden_command_without_completion_env(self):
        with patch.dict(os.environ, {"_IB_COMPLETE": "bash_complete", "KEEP_ME": "1"}, clear=False):
            with patch.object(ib.subprocess, "Popen") as popen:
                ib.start_search_cache_prewarm()

        command = popen.call_args.args[0]
        options = popen.call_args.kwargs
        self.assertEqual(command[0], ib.sys.executable)
        self.assertEqual(command[-1], "_prewarm-search-cache")
        self.assertEqual(options["stdin"], ib.subprocess.DEVNULL)
        self.assertEqual(options["stdout"], ib.subprocess.DEVNULL)
        self.assertEqual(options["stderr"], ib.subprocess.DEVNULL)
        self.assertNotIn("_IB_COMPLETE", options["env"])
        self.assertEqual(options["env"]["KEEP_ME"], "1")

    def test_hidden_prewarm_command_calls_runner(self):
        self.assertTrue(ib.cli.commands["_prewarm-search-cache"].hidden)
        with patch.object(ib, "run_search_cache_prewarm") as run_prewarm:
            ib.cli.commands["_prewarm-search-cache"].callback()

        run_prewarm.assert_called_once_with()

    def test_search_cache_prewarm_missing_config_exits_without_lock(self):
        with patch.object(ib, "load_config", return_value=None):
            with patch.object(ib, "acquire_search_cache_prewarm_lock") as acquire_lock:
                with patch.object(ib, "warm_global_search_cache") as warm_cache:
                    ib.run_search_cache_prewarm()

        acquire_lock.assert_not_called()
        warm_cache.assert_not_called()

    def test_search_cache_prewarm_skips_when_fresh_lock_exists(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                ib.ensure_allrecords_cache_dir()
                ib.search_cache_prewarm_lock_file().write_text("existing\n", encoding="utf-8")
                with patch.object(ib, "load_config", return_value={"server": "ignored"}):
                    with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                        with patch.object(ib, "warm_global_search_cache") as warm_cache:
                            ib.run_search_cache_prewarm()

        warm_cache.assert_not_called()

    def test_search_cache_prewarm_uses_global_view_scope(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        client = FakeClient()
        zones = [{"fqdn": "example.com", "primary_type": "Grid", "soa_serial_number": 1}]
        with patch.object(ib, "load_config", return_value={"server": "ignored"}):
            with patch.object(ib, "InfobloxClient", return_value=client):
                with patch.object(ib, "acquire_search_cache_prewarm_lock", return_value=(123, "token")):
                    with patch.object(ib, "release_search_cache_prewarm_lock") as release_lock:
                        with patch.object(ib, "search_zones", return_value=zones) as search_zones:
                            with patch.object(
                                ib,
                                "allrecords_search_entries_for_zone",
                                return_value=[],
                            ) as warm_zone:
                                ib.run_search_cache_prewarm()

        search_zones.assert_called_once_with(client, None)
        warm_zone.assert_called_once_with(client, zones[0])
        release_lock.assert_called_once_with((123, "token"))

    def test_usage_help_includes_current_view_and_active_zone(self):
        with patch.dict(os.environ, {ib.DEFAULT_ZONE_ENV: "example.com"}, clear=False):
            with patch.object(ib, "default_config_values", return_value={"dns_view": "corp"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    with ib.cli.make_context("ib", [], resilient_parsing=True) as ctx:
                        help_text = ib.cli.get_help(ctx)

        self.assertIn("DNS Context", help_text)
        self.assertIn("corp", help_text)
        self.assertIn("example.com", help_text)

    def test_dns_create_help_includes_colorful_usage_context(self):
        runner = CliRunner()
        with patch.dict(os.environ, {ib.DEFAULT_ZONE_ENV: "example.com"}, clear=False):
            with patch.object(ib, "default_config_values", return_value={"dns_view": "corp"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    result = runner.invoke(ib.cli, ["dns", "create", "--help"], color=True)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Create Record Usage", result.output)
        self.assertIn("ib dns create", result.output)
        self.assertIn("<type>", result.output)
        self.assertIn("<name>", result.output)
        self.assertIn("<value>", result.output)
        self.assertIn("Context", result.output)
        self.assertIn("View", result.output)
        self.assertIn("corp", result.output)
        self.assertIn("Zone=", result.output)
        self.assertIn("example.com", result.output)
        self.assertIn("Without --zone", result.output)
        self.assertIn("Example", result.output)
        self.assertIn("-n app", result.output)
        self.assertIn("Command:", result.output)
        self.assertIn("Creates:", result.output)
        self.assertIn("A record", result.output)
        self.assertIn("app.example.com", result.output)
        self.assertIn("Application VIP", result.output)
        with patch.dict(os.environ, {ib.DEFAULT_ZONE_ENV: "example.com"}, clear=False):
            with patch.object(ib, "default_config_values", return_value={"dns_view": "corp"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    with ib.cli.make_context("ib", [], resilient_parsing=True, color=True) as ctx:
                        color_output = ib.render_help_text(
                            ctx,
                            ib.dns_create_usage_panel(),
                            force_color=True,
                        )
        self.assertIn("\x1b[", color_output)

    def test_dns_create_help_guides_when_active_zone_is_missing(self):
        runner = CliRunner()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "default_config_values", return_value={"dns_view": "corp"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    result = runner.invoke(ib.cli, ["dns", "create", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Zone=", result.output)
        self.assertIn("not set", result.output)
        self.assertIn("Set a target with", result.output)
        self.assertIn("ib dns zone use", result.output)

    def test_dns_context_panel_uses_one_shared_borderless_line(self):
        with patch.object(ib, "current_view_status", return_value=("corp", "configured")):
            with patch.object(ib, "active_zone_status", return_value=("example.com", "shell session")):
                context_line = ib.dns_context_panel("Current DNS Context")

        self.assertIsInstance(context_line, ib.Text)
        self.assertEqual(
            context_line.plain,
            "Current DNS Context: View=corp | Zone=example.com",
        )
        self.assertNotIn("\n", context_line.plain)
        self.assertFalse(any(span.style and "on " in str(span.style) for span in context_line.spans))

    def test_dns_context_omits_redundant_missing_active_zone_source(self):
        with patch.object(ib, "current_view_status", return_value=("corp", "configured")):
            with patch.object(ib, "active_zone_status", return_value=(None, "not set")):
                context_line = ib.dns_context_panel("Current DNS Context")

        self.assertEqual(
            context_line.plain,
            "Current DNS Context: View=corp | Zone=not set",
        )
        self.assertNotIn("not set (not set)", context_line.plain)

    def test_dns_zone_list_prints_context_before_zone_table(self):
        class FakeClient:
            view = "corp"

        zones = [{"fqdn": "example.com", "view": "corp", "zone_format": "FORWARD"}]
        with patch.object(ib, "load_config", return_value={"dns_view": "corp"}):
            with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                with patch.object(ib, "query_zones", return_value=zones):
                    with patch.object(ib, "dns_context_panel", return_value="context"):
                        with patch.object(ib, "zone_table", return_value="zones"):
                            with patch.object(ib.console, "print") as print_mock:
                                ib.run_dns_zone_list()

        printed = [call.args[0] for call in print_mock.call_args_list]
        self.assertEqual(printed, ["context", "zones"])

    def test_dns_search_searches_active_zone_and_child_zones(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        table_records = []

        def fake_paged_query(client, object_type, params, warn_on_skip=True):
            if object_type == ib.ZONE_OBJECT:
                return [
                    {"fqdn": "example.com", "primary_type": "Grid", "soa_serial_number": 10},
                    {"fqdn": "dev.example.com", "primary_type": "Grid", "soa_serial_number": 20},
                    {"fqdn": "backup.example.com", "primary_type": "External", "soa_serial_number": 25},
                    {"fqdn": "other.example.net", "primary_type": "Grid", "soa_serial_number": 30},
                ]
            if object_type == ib.ALLRECORDS_OBJECT:
                zone_name = params["zone"]
                self.assertNotEqual(zone_name, "backup.example.com")
                return [
                    {
                        "_ref": f"allrecords/{zone_name}",
                        "type": "record:a",
                        "name": f"app.{zone_name}",
                        "zone": zone_name,
                        "address": "192.0.2.10",
                    }
                ]
            raise AssertionError(f"unexpected object type: {object_type}")

        def fake_record_table(records):
            table_records.extend(records)
            return "records"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
                    with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                        with patch.object(ib, "read_session_zone", return_value=None):
                            with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                                with patch.object(ib, "paged_query", side_effect=fake_paged_query):
                                    with patch.object(ib, "safe_query") as safe_query:
                                        with patch.object(ib, "record_table", side_effect=fake_record_table):
                                            with patch.object(ib, "dns_context_panel", return_value="context"):
                                                with patch.object(ib.console, "print") as print_mock:
                                                    ib.run_dns_search("app")

        safe_query.assert_not_called()
        result_refs = {item["_ref"] for _record_type, item in table_records}
        self.assertEqual(result_refs, {"allrecords/example.com", "allrecords/dev.example.com"})
        printed = [call.args[0] for call in print_mock.call_args_list]
        self.assertEqual(printed, ["context", "records"])

    def test_dns_search_uses_allrecords_cache_when_serial_matches(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        client = FakeClient()
        cached_records = [
            {
                "_ref": "allrecords/cached",
                "type": "record:a",
                "name": "cached.example.com",
                "zone": "example.com",
                "address": "192.0.2.55",
            }
        ]
        table_records = []

        def fake_paged_query(client, object_type, params, warn_on_skip=True):
            if object_type == ib.ZONE_OBJECT:
                return [{"fqdn": "example.com", "primary_type": "Grid", "soa_serial_number": 99}]
            if object_type == ib.ALLRECORDS_OBJECT:
                raise AssertionError("cache hit should not query allrecords")
            raise AssertionError(f"unexpected object type: {object_type}")

        def fake_record_table(records):
            table_records.extend(records)
            return "records"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                ib.write_allrecords_cache(client, "example.com", "99", cached_records)
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
                    with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                        with patch.object(ib, "read_session_zone", return_value=None):
                            with patch.object(ib, "InfobloxClient", return_value=client):
                                with patch.object(ib, "paged_query", side_effect=fake_paged_query):
                                    with patch.object(ib, "record_table", side_effect=fake_record_table):
                                        with patch.object(ib, "dns_context_panel", return_value="context"):
                                            with patch.object(ib.console, "print") as print_mock:
                                                ib.run_dns_search("cached")

        result_refs = {item["_ref"] for _record_type, item in table_records}
        self.assertEqual(result_refs, {"allrecords/cached"})
        printed = [call.args[0] for call in print_mock.call_args_list]
        self.assertEqual(printed, ["context", "records"])

    def test_dns_search_uses_normalized_cache_entries_without_rebuilding_values(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        client = FakeClient()
        cached_record = {
            "_ref": "allrecords/cached-normalized",
            "type": "record:cname",
            "name": "alias",
            "zone": "example.com",
            "record": {"name": "alias.example.com", "canonical": "target.example.net"},
        }
        table_records = []

        def fake_paged_query(client, object_type, params, warn_on_skip=True):
            if object_type == ib.ZONE_OBJECT:
                return [{"fqdn": "example.com", "primary_type": "Grid", "soa_serial_number": 7}]
            if object_type == ib.ALLRECORDS_OBJECT:
                raise AssertionError("normalized cache hit should not query allrecords")
            raise AssertionError(f"unexpected object type: {object_type}")

        def fake_record_table(records):
            table_records.extend(records)
            return "records"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                ib.write_allrecords_cache(
                    client,
                    "example.com",
                    "7",
                    [cached_record],
                    [
                        {
                            "type": "cname",
                            "zone": "example.com",
                            "name": "alias.example.com",
                            "value": "target.example.net",
                            "comment": "",
                            "record": cached_record,
                        }
                    ],
                )
                self.assertTrue(ib.allrecords_cache_db_file().exists())
                self.assertFalse(ib.allrecords_cache_file(client, "example.com").exists())
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
                    with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                        with patch.object(ib, "read_session_zone", return_value=None):
                            with patch.object(ib, "InfobloxClient", return_value=client):
                                with patch.object(ib, "paged_query", side_effect=fake_paged_query):
                                    with patch.object(
                                        ib,
                                        "allrecord_display_value",
                                        side_effect=AssertionError(
                                            "normalized cache should already have display values"
                                        ),
                                    ):
                                        with patch.object(ib, "record_table", side_effect=fake_record_table):
                                            with patch.object(ib, "dns_context_panel", return_value="context"):
                                                with patch.object(ib.console, "print") as print_mock:
                                                    ib.run_dns_search("target")

        self.assertEqual(table_records, [("cname", cached_record)])
        printed = [call.args[0] for call in print_mock.call_args_list]
        self.assertEqual(printed, ["context", "records"])

    def test_dns_search_upgrades_legacy_allrecords_cache_to_search_entries(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        client = FakeClient()
        cached_record = {
            "_ref": "allrecords/legacy",
            "type": "record:a",
            "name": "legacy.example.com",
            "zone": "example.com",
            "address": "192.0.2.60",
        }
        table_records = []

        def fake_paged_query(client, object_type, params, warn_on_skip=True):
            if object_type == ib.ZONE_OBJECT:
                return [{"fqdn": "example.com", "primary_type": "Grid", "soa_serial_number": 8}]
            if object_type == ib.ALLRECORDS_OBJECT:
                raise AssertionError("legacy cache with matching serial should not query allrecords")
            raise AssertionError(f"unexpected object type: {object_type}")

        def fake_record_table(records):
            table_records.extend(records)
            return "records"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                cache_file = ib.allrecords_cache_file(client, "example.com")
                cache_file.write_text(
                    json.dumps(
                        {
                            "created_at": 1,
                            "server": client.server,
                            "wapi_version": client.wapi_version,
                            "view": client.view,
                            "zone": "example.com",
                            "soa_serial_number": "8",
                            "records": [cached_record],
                        }
                    ),
                    encoding="utf-8",
                )
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
                    with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                        with patch.object(ib, "read_session_zone", return_value=None):
                            with patch.object(ib, "InfobloxClient", return_value=client):
                                with patch.object(ib, "paged_query", side_effect=fake_paged_query):
                                    with patch.object(ib, "record_table", side_effect=fake_record_table):
                                        with patch.object(ib, "dns_context_panel", return_value="context"):
                                            with patch.object(ib.console, "print"):
                                                ib.run_dns_search("legacy")

                upgraded = ib.read_sqlite_allrecords_search_entries(client, "example.com", "8")

        self.assertEqual(table_records, [("a", cached_record)])
        self.assertIsNotNone(upgraded)
        self.assertEqual(upgraded[0]["name"], "legacy.example.com")
        self.assertEqual(upgraded[0]["value"], "192.0.2.60")

    def test_dns_search_refreshes_allrecords_cache_when_serial_changes(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        client = FakeClient()
        table_records = []
        allrecords_queries = 0

        def fake_paged_query(client, object_type, params, warn_on_skip=True):
            nonlocal allrecords_queries
            if object_type == ib.ZONE_OBJECT:
                return [{"fqdn": "example.com", "primary_type": "Grid", "soa_serial_number": 2}]
            if object_type == ib.ALLRECORDS_OBJECT:
                allrecords_queries += 1
                return [
                    {
                        "_ref": "allrecords/new",
                        "type": "record:a",
                        "name": "new.example.com",
                        "zone": "example.com",
                        "address": "192.0.2.56",
                    }
                ]
            raise AssertionError(f"unexpected object type: {object_type}")

        def fake_record_table(records):
            table_records.extend(records)
            return "records"

        stale_records = [
            {
                "_ref": "allrecords/old",
                "type": "record:a",
                "name": "old.example.com",
                "zone": "example.com",
                "address": "192.0.2.57",
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                ib.write_allrecords_cache(client, "example.com", "1", stale_records)
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
                    with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                        with patch.object(ib, "read_session_zone", return_value=None):
                            with patch.object(ib, "InfobloxClient", return_value=client):
                                with patch.object(ib, "paged_query", side_effect=fake_paged_query):
                                    with patch.object(ib, "record_table", side_effect=fake_record_table):
                                        with patch.object(ib, "dns_context_panel", return_value="context"):
                                            with patch.object(ib.console, "print") as print_mock:
                                                ib.run_dns_search("new")

        self.assertEqual(allrecords_queries, 1)
        result_refs = {item["_ref"] for _record_type, item in table_records}
        self.assertEqual(result_refs, {"allrecords/new"})
        printed = [call.args[0] for call in print_mock.call_args_list]
        self.assertEqual(printed, ["context", "records"])

    def test_dns_search_rebuilds_normalized_cache_when_serial_changes(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        client = FakeClient()
        stale_records = [
            {
                "_ref": "allrecords/stale",
                "type": "record:a",
                "name": "stale.example.com",
                "zone": "example.com",
                "address": "192.0.2.61",
            }
        ]
        fresh_record = {
            "_ref": "allrecords/fresh",
            "type": "record:txt",
            "name": "fresh",
            "zone": "example.com",
            "record": {
                "name": "fresh.example.com",
                "text": "fresh-token",
            },
        }

        def fake_paged_query(client, object_type, params, warn_on_skip=True):
            if object_type == ib.ZONE_OBJECT:
                return [{"fqdn": "example.com", "primary_type": "Grid", "soa_serial_number": 10}]
            if object_type == ib.ALLRECORDS_OBJECT:
                return [fresh_record]
            raise AssertionError(f"unexpected object type: {object_type}")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                ib.write_allrecords_cache(client, "example.com", "9", stale_records)
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
                    with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                        with patch.object(ib, "read_session_zone", return_value=None):
                            with patch.object(ib, "InfobloxClient", return_value=client):
                                with patch.object(ib, "paged_query", side_effect=fake_paged_query):
                                    with patch.object(ib, "record_table", return_value="records"):
                                        with patch.object(ib, "dns_context_panel", return_value="context"):
                                            with patch.object(ib.console, "print"):
                                                ib.run_dns_search("fresh-token")

                refreshed = ib.read_sqlite_allrecords_search_entries(client, "example.com", "10")

        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed[0]["type"], "txt")
        self.assertEqual(refreshed[0]["value"], "fresh-token")

    def test_dns_search_global_flag_ignores_active_zone_scope(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        table_records = []

        def fake_paged_query(client, object_type, params, warn_on_skip=True):
            if object_type == ib.ZONE_OBJECT:
                return [
                    {"fqdn": "example.com", "primary_type": "Grid", "soa_serial_number": 10},
                    {"fqdn": "other.example.net", "primary_type": "Grid", "soa_serial_number": 30},
                    {"fqdn": "secondary.example.net", "primary_type": "External", "soa_serial_number": 40},
                ]
            if object_type == ib.ALLRECORDS_OBJECT:
                zone_name = params["zone"]
                self.assertNotEqual(zone_name, "secondary.example.net")
                return [
                    {
                        "_ref": f"allrecords/{zone_name}",
                        "type": "record:a",
                        "name": f"app.{zone_name}",
                        "zone": zone_name,
                        "address": "192.0.2.10",
                    }
                ]
            raise AssertionError(f"unexpected object type: {object_type}")

        def fake_record_table(records):
            table_records.extend(records)
            return "records"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
                    with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                        with patch.object(ib, "read_session_zone", return_value=None):
                            with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                                with patch.object(ib, "paged_query", side_effect=fake_paged_query):
                                    with patch.object(ib, "record_table", side_effect=fake_record_table):
                                        with patch.object(ib, "dns_context_panel") as dns_context_panel:
                                            with patch.object(ib.console, "print") as print_mock:
                                                ib.run_dns_search("app", global_search=True)

        result_refs = {item["_ref"] for _record_type, item in table_records}
        self.assertEqual(result_refs, {"allrecords/example.com", "allrecords/other.example.net"})
        dns_context_panel.assert_not_called()
        print_mock.assert_called_once_with("records")

    def test_dns_search_returns_deterministic_order_after_parallel_zone_work(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        records_by_zone = {
            "beta.example.com": {
                "_ref": "allrecords/beta",
                "type": "record:a",
                "name": "app.beta.example.com",
                "zone": "beta.example.com",
                "address": "192.0.2.71",
            },
            "alpha.example.com": {
                "_ref": "allrecords/alpha",
                "type": "record:a",
                "name": "app.alpha.example.com",
                "zone": "alpha.example.com",
                "address": "192.0.2.70",
            },
        }

        def fake_matches_for_zone(client, zone_info, keyword, case_sensitive, root_zone):
            zone_name = zone_info["fqdn"]
            return [ib.allrecord_search_entry(records_by_zone[zone_name], zone_name)]

        with patch.object(
            ib,
            "search_zones",
            return_value=[
                {"fqdn": "beta.example.com"},
                {"fqdn": "alpha.example.com"},
            ],
        ):
            with patch.object(ib, "matching_search_entries_for_zone", side_effect=fake_matches_for_zone):
                records = ib.collect_dns_search_results(FakeClient(), "app", False, None)

        self.assertEqual(
            [item["name"] for _record_type, item in records],
            ["app.alpha.example.com", "app.beta.example.com"],
        )

    def test_dns_search_includes_host_records(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        table_records = []

        def fake_paged_query(client, object_type, params, warn_on_skip=True):
            if object_type == ib.ZONE_OBJECT:
                return [{"fqdn": "example.com", "primary_type": "Grid", "soa_serial_number": 10}]
            if object_type == ib.ALLRECORDS_OBJECT:
                return [
                    {
                        "_ref": "allrecords/host",
                        "type": "record:host",
                        "name": "app.example.com",
                        "zone": "example.com",
                        "address": "192.0.2.50",
                    }
                ]
            raise AssertionError(f"unexpected object type: {object_type}")

        def fake_record_table(records):
            table_records.extend(records)
            return "records"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
                    with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                        with patch.object(ib, "read_session_zone", return_value=None):
                            with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                                with patch.object(ib, "paged_query", side_effect=fake_paged_query):
                                    with patch.object(ib, "record_table", side_effect=fake_record_table):
                                        with patch.object(ib, "dns_context_panel", return_value="context"):
                                            with patch.object(ib.console, "print") as print_mock:
                                                ib.run_dns_search("app")

        self.assertEqual(table_records[0][0], "host")
        self.assertEqual(ib.record_value("host", table_records[0][1]), "192.0.2.50")
        printed = [call.args[0] for call in print_mock.call_args_list]
        self.assertEqual(printed, ["context", "records"])

    def test_dns_search_formats_cname_and_txt_allrecords(self):
        cname = {
            "_ref": "allrecords/cname",
            "type": "record:cname",
            "name": "alias",
            "zone": "example.com",
            "address": "192.0.2.10",
            "record": {
                "name": "alias.example.com",
                "canonical": "target.example.net",
            },
        }
        txt = {
            "_ref": "allrecords/txt",
            "type": "record:txt",
            "name": "spf",
            "zone": "example.com",
            "record": {
                "name": "spf.example.com",
                "text": "v=spf1 include:example.net -all",
            },
        }

        self.assertEqual(ib.record_name(cname), "alias.example.com")
        self.assertEqual(ib.record_value("cname", cname), "target.example.net")
        self.assertEqual(ib.record_value("shared-cname", cname), "target.example.net")
        self.assertEqual(ib.record_name(txt), "spf.example.com")
        self.assertEqual(ib.record_value("txt", txt), "v=spf1 include:example.net -all")

    def test_dns_search_matches_allrecords_name_value_or_comment_only(self):
        txt = {
            "_ref": "allrecords/internal-token",
            "type": "record:txt",
            "name": "spf",
            "zone": "internal-token.example.com",
            "comment": "",
            "record": {
                "name": "spf.example.com",
                "text": "v=spf1 include:example.net -all",
            },
        }

        self.assertTrue(ib.allrecord_matches_keyword(txt, "spf1", case_sensitive=False))
        self.assertFalse(ib.allrecord_matches_keyword(txt, "record:txt", case_sensitive=False))
        self.assertFalse(ib.allrecord_matches_keyword(txt, "internal-token", case_sensitive=False))

    def test_dns_search_without_active_zone_uses_all_zones_in_view(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        table_records = []

        def fake_paged_query(client, object_type, params, warn_on_skip=True):
            if object_type == ib.ZONE_OBJECT:
                return [{"fqdn": "example.net", "primary_type": "Grid", "soa_serial_number": 1}]
            if object_type == ib.ALLRECORDS_OBJECT:
                return [
                    {
                        "_ref": "allrecords/global",
                        "type": "record:a",
                        "name": "app.example.net",
                        "zone": "example.net",
                        "address": "192.0.2.20",
                    }
                ]
            raise AssertionError(f"unexpected object type: {object_type}")

        def fake_record_table(records):
            table_records.extend(records)
            return "records"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
                    with patch.object(ib, "load_config", return_value={}):
                        with patch.object(ib, "read_session_zone", return_value=None):
                            with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                                with patch.object(ib, "paged_query", side_effect=fake_paged_query):
                                    with patch.object(ib, "record_table", side_effect=fake_record_table):
                                        with patch.object(ib, "dns_context_panel", return_value="context"):
                                            with patch.object(ib.console, "print") as print_mock:
                                                ib.run_dns_search("app")

        result_refs = {item["_ref"] for _record_type, item in table_records}
        self.assertEqual(result_refs, {"allrecords/global"})
        printed = [call.args[0] for call in print_mock.call_args_list]
        self.assertEqual(printed, ["context", "records"])

    def test_dns_search_skips_secondary_root_zone(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        allrecords_queries = []

        def fake_paged_query(client, object_type, params, warn_on_skip=True):
            if object_type == ib.ZONE_OBJECT:
                return [{"fqdn": "example.com", "primary_type": "External", "soa_serial_number": 1}]
            if object_type == ib.ALLRECORDS_OBJECT:
                allrecords_queries.append(params["zone"])
                return []
            raise AssertionError(f"unexpected object type: {object_type}")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
                    with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                        with patch.object(ib, "read_session_zone", return_value=None):
                            with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                                with patch.object(ib, "paged_query", side_effect=fake_paged_query):
                                    with patch.object(ib, "dns_context_panel", return_value="context"):
                                        with patch.object(ib.console, "print") as print_mock:
                                            with patch.object(ib, "print_warning") as print_warning:
                                                ib.run_dns_search("app")

        self.assertEqual(allrecords_queries, [])
        print_mock.assert_called_once_with("context")
        print_warning.assert_called_once_with("No records found.")

    def test_dns_zone_view_queries_and_prints_zone_details(self):
        class FakeClient:
            view = "corp"

            def request(self, method, object_type, params=None, payload=None):
                self.association_params = params
                return [
                    {
                        "network_view": "default",
                        "network_associations": [{"network": "10.1.1.0/24"}],
                    }
                ]

        zone_info = {"fqdn": "example.com", "view": "corp", "zone_format": "FORWARD"}
        fake_client = FakeClient()
        table_inputs = []

        def fake_zone_detail_table(item):
            table_inputs.append(item)
            return "details"

        with patch.object(ib, "load_config", return_value={"dns_view": "corp"}):
            with patch.object(ib, "InfobloxClient", return_value=fake_client):
                with patch.object(ib, "safe_query", return_value=[zone_info]) as safe_query:
                    with patch.object(ib, "zone_detail_table", side_effect=fake_zone_detail_table):
                        with patch.object(ib.console, "print") as print_mock:
                            ib.run_dns_zone_view("example.com.")

        params = safe_query.call_args.args[2]
        self.assertEqual(params["fqdn"], "example.com")
        self.assertEqual(params["view"], "corp")
        self.assertEqual(params["_return_fields"], ib.ZONE_DETAIL_RETURN_FIELDS)
        self.assertIn("soa_serial_number", params["_return_fields"])
        self.assertIn("member_soa_mnames", params["_return_fields"])
        self.assertIn("soa_negative_ttl", params["_return_fields"])
        self.assertIn("network_view", params["_return_fields"])
        self.assertNotIn("network_associations", params["_return_fields"])
        self.assertEqual(fake_client.association_params["fqdn"], "example.com")
        self.assertEqual(fake_client.association_params["view"], "corp")
        self.assertEqual(
            fake_client.association_params["_return_fields"],
            ib.ZONE_NETWORK_ASSOCIATION_RETURN_FIELDS,
        )
        self.assertEqual(table_inputs[0]["network_view"], "default")
        self.assertEqual(table_inputs[0]["network_associations"], [{"network": "10.1.1.0/24"}])
        print_mock.assert_called_once_with("details")

    def test_dns_zone_view_does_not_fail_when_network_association_query_returns_500(self):
        class FakeClient:
            view = "corp"

            def request(self, method, object_type, params=None, payload=None):
                raise ib.WapiError("ERROR: Infoblox WAPI 500: Internal Error", status=500)

        zone_info = {"fqdn": "example.com", "view": "corp", "zone_format": "FORWARD"}
        table_inputs = []

        def fake_zone_detail_table(item):
            table_inputs.append(item)
            return "details"

        with patch.object(ib, "load_config", return_value={"dns_view": "corp"}):
            with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                with patch.object(ib, "safe_query", return_value=[zone_info]):
                    with patch.object(ib, "zone_detail_table", side_effect=fake_zone_detail_table):
                        with patch.object(ib.console, "print") as print_mock:
                            ib.run_dns_zone_view("example.com.")

        print_mock.assert_called_once_with("details")
        self.assertIn("Unavailable: Infoblox WAPI 500", table_inputs[0]["network_associations"])

    def test_zone_serial_query_includes_primary_type(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                with patch.object(ib, "paged_query", return_value=[]) as paged_query:
                    ib.query_zone_serials(FakeClient())

        params = paged_query.call_args.args[2]
        self.assertIn("primary_type", params["_return_fields"])

    def test_zone_serial_query_uses_30_second_cache(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        zones = [{"fqdn": "example.com", "primary_type": "Grid", "soa_serial_number": 1}]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                with patch.object(ib, "paged_query", return_value=zones) as paged_query:
                    with patch.object(ib.time, "time", return_value=100):
                        self.assertEqual(ib.query_zone_serials(FakeClient()), zones)
                    with patch.object(ib.time, "time", return_value=129):
                        self.assertEqual(ib.query_zone_serials(FakeClient()), zones)

        paged_query.assert_called_once()

    def test_zone_serial_query_refreshes_after_30_second_cache_expires(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        first = [{"fqdn": "example.com", "primary_type": "Grid", "soa_serial_number": 1}]
        second = [{"fqdn": "example.com", "primary_type": "Grid", "soa_serial_number": 2}]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                with patch.object(ib, "paged_query", side_effect=[first, second]) as paged_query:
                    with patch.object(ib.time, "time", return_value=100):
                        self.assertEqual(ib.query_zone_serials(FakeClient()), first)
                    with patch.object(ib.time, "time", return_value=131):
                        self.assertEqual(ib.query_zone_serials(FakeClient()), second)

        self.assertEqual(paged_query.call_count, 2)

    def test_dns_search_worker_count_defaults_to_8(self):
        self.assertEqual(ib.DNS_SEARCH_WORKERS, 8)

    def test_zone_detail_table_includes_soa_settings(self):
        table = ib.zone_detail_table(
            {
                "fqdn": "example.com",
                "view": "corp",
                "zone_format": "FORWARD",
                "member_soa_mnames": [{"mname": "ns1.example.com"}],
                "network_view": "default",
                "network_associations": [
                    {"network": "10.1.1.0/24", "network_view": "default"},
                    {
                        "network": "10.1.2.0/24",
                        "network_view": "default",
                        "comment": "Lab network",
                    },
                ],
                "soa_email": "hostmaster.example.com",
                "soa_refresh": 10800,
                "soa_retry": 3600,
                "soa_expire": 604800,
                "soa_negative_ttl": 300,
                "soa_serial_number": 42,
            }
        )

        rows = dict(zip(table.columns[0]._cells, table.columns[1]._cells))
        self.assertEqual(rows["Serial Number"], "42")
        self.assertEqual(rows["SOA MNAME"], "ns1.example.com")
        self.assertEqual(rows["SOA RNAME"], "hostmaster.example.com")
        self.assertEqual(rows["Refresh"], "10800")
        self.assertEqual(rows["Retry"], "3600")
        self.assertEqual(rows["Expiry"], "604800")
        self.assertEqual(rows["Negative Caching TTL"], "300")
        self.assertEqual(rows["Network View"], "default")
        self.assertIn("10.1.1.0/24 (view: default)", rows["Network Associations"])
        self.assertIn(
            "10.1.2.0/24 (view: default, comment: Lab network)",
            rows["Network Associations"],
        )

    def test_dns_zone_view_uses_zone_name_completion(self):
        zone_view = ib.zone.commands["view"]

        self.assertIs(zone_view.params[0]._custom_shell_complete, ib.complete_zone_names)


if __name__ == "__main__":
    unittest.main()
