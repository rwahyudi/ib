import importlib.machinery
import importlib.util
import configparser
import io
import json
import os
import tempfile
import tomllib
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
ORIGINAL_REFRESH_DNS_CACHE_AFTER_UPDATE = ib.refresh_dns_cache_after_update


class DefaultZoneTests(unittest.TestCase):
    def setUp(self):
        self.cache_refresh_patcher = patch.object(ib, "refresh_dns_cache_after_update")
        self.cache_refresh = self.cache_refresh_patcher.start()
        self.addCleanup(self.cache_refresh_patcher.stop)

    def config_path_patch(self, tmpdir):
        config_dir = Path(tmpdir) / ".ib"
        return patch.multiple(
            ib,
            CONFIG_DIR=config_dir,
            CONFIG_FILE=config_dir / "config",
            CONFIG_KEY_FILE=config_dir / "key",
        )

    def assert_command_tree_has_output_option(self, command, path="ib"):
        option_names = set()
        for param in command.params:
            option_names.update(getattr(param, "opts", ()))
        self.assertIn("--output", option_names, path)
        self.assertIn("-o", option_names, path)

        for name, subcommand in getattr(command, "commands", {}).items():
            self.assert_command_tree_has_output_option(subcommand, f"{path} {name}")

    def test_pyproject_installs_existing_ib_script(self):
        pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"

        with pyproject_path.open("rb") as handle:
            pyproject = tomllib.load(handle)

        self.assertEqual(pyproject["project"]["name"], "ib")
        self.assertEqual(pyproject["project"]["requires-python"], ">=3.9")
        self.assertEqual(
            set(pyproject["project"]["dependencies"]),
            {"click>=8.1", "cryptography>=42", "rich>=13.7"},
        )
        self.assertEqual(pyproject["tool"]["setuptools"]["py-modules"], [])
        self.assertEqual(pyproject["tool"]["setuptools"]["script-files"], ["ib"])

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
        self.cache_refresh.assert_called_once_with()

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

    def test_dns_create_srv_payload_uses_priority_weight_port_and_target(self):
        class FakeClient:
            view = "corp"

        object_type, payload = ib.create_payload(
            "srv",
            "10 20 5060 sip.example.com.",
            "_sip._tcp",
            "example.com",
            300,
            "SIP service",
            FakeClient(),
        )

        self.assertEqual(object_type, "record:srv")
        self.assertEqual(
            payload,
            {
                "name": "_sip._tcp.example.com",
                "view": "corp",
                "ttl": 300,
                "use_ttl": True,
                "comment": "SIP service",
                "priority": 10,
                "weight": 20,
                "port": 5060,
                "target": "sip.example.com",
            },
        )

    def test_dns_create_srv_rejects_malformed_value(self):
        class FakeClient:
            view = "corp"

        bad_values = [
            ("10 20 5060", "SRV value must be quoted"),
            ("high 20 5060 sip.example.com", "SRV priority must be an integer"),
            ("10 heavy 5060 sip.example.com", "SRV weight must be an integer"),
            ("10 20 sip sip.example.com", "SRV port must be an integer"),
            ("10 20 70000 sip.example.com", "SRV port must be between 0 and 65535"),
        ]

        for value, message in bad_values:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ib.CliError, message):
                    ib.create_payload("srv", value, "_sip._tcp", "example.com", None, None, FakeClient())

    def test_dns_edit_srv_value_payload_uses_priority_weight_port_and_target(self):
        self.assertEqual(
            ib.update_value_payload("srv", "10 20 5061 sip2.example.com."),
            {
                "priority": 10,
                "weight": 20,
                "port": 5061,
                "target": "sip2.example.com",
            },
        )

    def test_dns_create_cname_checks_target_resolution_without_warning_when_resolved(self):
        seen = {}

        class FakeClient:
            view = "corp"

            def request(self, method, object_type, payload=None):
                seen["request"] = (method, object_type, payload)
                return "record:cname/example"

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                        with patch.object(ib.socket, "getaddrinfo", return_value=[object()]) as getaddrinfo:
                            with patch.object(ib, "print_warning") as print_warning:
                                with patch.object(ib, "print_success"), patch.object(ib, "print_note"):
                                    ib.run_dns_create(
                                        "cname",
                                        "target.example.net.",
                                        "www",
                                        None,
                                        None,
                                        False,
                                        None,
                                    )

        getaddrinfo.assert_called_once_with("target.example.net", None)
        print_warning.assert_not_called()
        self.assertEqual(seen["request"][1], "record:cname")
        self.assertEqual(seen["request"][2]["canonical"], "target.example.net")

    def test_dns_create_cname_warns_when_target_does_not_resolve_but_continues(self):
        seen = {}

        class FakeClient:
            view = "corp"

            def request(self, method, object_type, payload=None):
                seen["request"] = (method, object_type, payload)
                return "record:cname/example"

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                        with patch.object(
                            ib.socket,
                            "getaddrinfo",
                            side_effect=ib.socket.gaierror("Name or service not known"),
                        ):
                            with patch.object(ib, "print_warning") as print_warning:
                                with patch.object(ib, "print_success"), patch.object(ib, "print_note"):
                                    ib.run_dns_create(
                                        "cname",
                                        "missing.example.net",
                                        "www",
                                        None,
                                        None,
                                        False,
                                        None,
                                    )

        print_warning.assert_called_once()
        warning = print_warning.call_args.args[0]
        self.assertIn("WARNING: CNAME target missing.example.net does not resolve", warning)
        self.assertIn("Record creation will continue", warning)
        self.assertEqual(seen["request"][1], "record:cname")

    def test_dns_create_non_cname_does_not_check_target_resolution(self):
        class FakeClient:
            view = "corp"

            def request(self, method, object_type, payload=None):
                return "record:a/example"

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                        with patch.object(ib.socket, "getaddrinfo") as getaddrinfo:
                            with patch.object(ib, "print_success"), patch.object(ib, "print_note"):
                                ib.run_dns_create("a", "192.0.2.10", "app", None, None, False, None)

        getaddrinfo.assert_not_called()

    def test_dns_create_accepts_positional_name_ttl_and_comment_options(self):
        runner = CliRunner()

        with patch.object(ib, "run_dns_create") as run_dns_create:
            result = runner.invoke(
                ib.cli,
                [
                    "dns",
                    "create",
                    "a",
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

    def test_dns_create_accepts_legacy_name_option(self):
        runner = CliRunner()

        with patch.object(ib, "run_dns_create") as run_dns_create:
            result = runner.invoke(
                ib.cli,
                ["dns", "create", "a", "-n", "app", "192.0.2.10"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        run_dns_create.assert_called_once_with(
            "a",
            "192.0.2.10",
            "app",
            None,
            None,
            False,
            None,
        )

    def test_dns_create_accepts_srv_quoted_value(self):
        runner = CliRunner()

        with patch.object(ib, "run_dns_create") as run_dns_create:
            result = runner.invoke(
                ib.cli,
                ["dns", "create", "srv", "_sip._tcp", "10 20 5060 sip.example.com"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        run_dns_create.assert_called_once_with(
            "srv",
            "10 20 5060 sip.example.com",
            "_sip._tcp",
            None,
            None,
            False,
            None,
        )

    def test_dns_edit_accepts_name_first_and_create_style_options(self):
        runner = CliRunner()

        with patch.object(ib, "run_dns_edit") as run_dns_edit:
            result = runner.invoke(
                ib.cli,
                [
                    "dns",
                    "edit",
                    "app",
                    "host",
                    "192.0.2.20",
                    "--zone",
                    "example.com",
                    "-t",
                    "300",
                    "--noptr",
                    "-c",
                    "Application host",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        run_dns_edit.assert_called_once_with(
            "app",
            "host",
            "192.0.2.20",
            "example.com",
            300,
            True,
            "Application host",
        )

    def test_dns_edit_accepts_srv_quoted_value(self):
        runner = CliRunner()

        with patch.object(ib, "run_dns_edit") as run_dns_edit:
            result = runner.invoke(
                ib.cli,
                ["dns", "edit", "_sip._tcp", "srv", "10 20 5061 sip2.example.com"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        run_dns_edit.assert_called_once_with(
            "_sip._tcp",
            "srv",
            "10 20 5061 sip2.example.com",
            None,
            None,
            False,
            None,
        )

    def test_dns_edit_updates_existing_record_by_ref(self):
        seen = {}

        class FakeClient:
            view = "corp"

            def request(self, method, object_type, payload=None, params=None):
                seen["request"] = (method, object_type, payload)
                return "record:a/app"

        def fake_safe_query(client, object_type, params):
            seen.setdefault("queries", []).append((object_type, params))
            if object_type == "record:a":
                return [
                    {
                        "_ref": "record:a/app",
                        "name": "app.example.com",
                        "zone": "example.com",
                        "ipv4addr": "192.0.2.10",
                    }
                ]
            return []

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                        with patch.object(ib, "safe_query", side_effect=fake_safe_query):
                            with patch.object(ib, "print_success"), patch.object(ib, "print_note"):
                                ib.run_dns_edit(
                                    "app",
                                    "a",
                                    "192.0.2.20",
                                    None,
                                    300,
                                    False,
                                    "Application host",
                                )

        self.assertIn(
            (
                "record:a",
                {
                    "_return_fields": ib.RECORD_TYPES["a"]["return_fields"],
                    "view": "corp",
                    "name": "app.example.com",
                },
            ),
            seen["queries"],
        )
        self.assertEqual(seen["request"][0], "PUT")
        self.assertEqual(seen["request"][1], "record:a/app")
        self.assertEqual(
            seen["request"][2],
            {
                "ipv4addr": "192.0.2.20",
                "ttl": 300,
                "use_ttl": True,
                "comment": "Application host",
            },
        )
        self.cache_refresh.assert_called_once_with()

    def test_dns_edit_accepts_metadata_only_without_type_or_value(self):
        runner = CliRunner()

        with patch.object(ib, "run_dns_edit") as run_dns_edit:
            result = runner.invoke(
                ib.cli,
                ["dns", "edit", "app", "-t", "600", "-c", "Updated comment"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        run_dns_edit.assert_called_once_with(
            "app",
            None,
            None,
            None,
            600,
            False,
            "Updated comment",
        )

    def test_dns_edit_updates_ttl_and_comment_without_type(self):
        seen = {}

        class FakeClient:
            view = "corp"

            def request(self, method, object_type, payload=None, params=None):
                seen["request"] = (method, object_type, payload)
                return "record:host/app"

        def fake_safe_query(client, object_type, params):
            if object_type == "record:host":
                return [
                    {
                        "_ref": "record:host/app",
                        "name": "app.example.com",
                        "zone": "example.com",
                        "ipv4addrs": [{"ipv4addr": "192.0.2.10"}],
                    }
                ]
            return []

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                        with patch.object(ib, "safe_query", side_effect=fake_safe_query):
                            with patch.object(ib, "print_success"), patch.object(ib, "print_note"):
                                ib.run_dns_edit(
                                    "app",
                                    None,
                                    None,
                                    None,
                                    600,
                                    False,
                                    "Updated comment",
                                )

        self.assertEqual(seen["request"][0], "PUT")
        self.assertEqual(seen["request"][1], "record:host/app")
        self.assertEqual(
            seen["request"][2],
            {"ttl": 600, "use_ttl": True, "comment": "Updated comment"},
        )
        self.cache_refresh.assert_called_once_with()

    def test_dns_edit_rejects_type_that_does_not_match_selected_record(self):
        class FakeClient:
            view = "corp"

            def request(self, method, object_type, payload=None, params=None):
                raise AssertionError("type mismatch must not update Infoblox")

        def fake_safe_query(client, object_type, params):
            if object_type == "record:host":
                return [
                    {
                        "_ref": "record:host/app",
                        "name": "app.example.com",
                        "zone": "example.com",
                        "ipv4addrs": [{"ipv4addr": "192.0.2.10"}],
                    }
                ]
            return []

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                        with patch.object(ib, "safe_query", side_effect=fake_safe_query):
                            with self.assertRaises(ib.CliError) as caught:
                                ib.run_dns_edit(
                                    "app",
                                    "a",
                                    "192.0.2.20",
                                    None,
                                    None,
                                    False,
                                    None,
                                )

        self.assertIn("app.example.com is a HOST record", str(caught.exception))
        self.assertIn("edit type must be HOST", str(caught.exception))
        self.cache_refresh.assert_not_called()

    def test_dns_edit_requires_a_change_when_type_and_value_are_omitted(self):
        class FakeClient:
            view = "corp"

            def request(self, method, object_type, payload=None, params=None):
                raise AssertionError("empty edit must not update Infoblox")

        def fake_safe_query(client, object_type, params):
            if object_type == "record:a":
                return [
                    {
                        "_ref": "record:a/app",
                        "name": "app.example.com",
                        "zone": "example.com",
                    }
                ]
            return []

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                        with patch.object(ib, "safe_query", side_effect=fake_safe_query):
                            with self.assertRaisesRegex(ib.CliError, "nothing to update"):
                                ib.run_dns_edit("app", None, None, None, None, False, None)

        self.cache_refresh.assert_not_called()

    def test_dns_create_bash_completion_no_longer_suggests_name_option_after_type(self):
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
        self.assertNotIn("plain,-n", result.output.splitlines())
        self.assertNotIn("plain,--name", result.output.splitlines())

    def test_dns_create_rejects_non_integer_ttl(self):
        runner = CliRunner()

        with patch.object(ib, "run_dns_create") as run_dns_create:
            result = runner.invoke(
                ib.cli,
                ["dns", "create", "a", "app", "192.0.2.10", "-t", "soon"],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("not a valid integer", result.output)
        run_dns_create.assert_not_called()

    def test_read_masked_password_echoes_stars_and_handles_backspace(self):
        class FakeConsole:
            is_terminal = True

            def __init__(self):
                self.file = io.StringIO()

            def print(self, *objects, end="\n", **kwargs):
                self.file.write(" ".join(str(item) for item in objects))
                self.file.write(end)

        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"ab\x7fc\n")
            os.close(write_fd)
            write_fd = -1
            fake_console = FakeConsole()
            with patch.object(ib, "console", fake_console):
                with patch.object(ib.sys.stdin, "fileno", return_value=read_fd):
                    with patch("termios.tcgetattr", return_value="settings"):
                        with patch("termios.tcsetattr") as tcsetattr:
                            with patch("tty.setcbreak") as setcbreak:
                                password = ib.read_masked_password("Password")
        finally:
            os.close(read_fd)
            if write_fd != -1:
                os.close(write_fd)

        self.assertEqual(password, "ac")
        self.assertEqual(fake_console.file.getvalue(), "Password: **\b \b*\n")
        setcbreak.assert_called_once_with(read_fd)
        tcsetattr.assert_called_once()
        self.assertEqual(tcsetattr.call_args.args[0], read_fd)
        self.assertEqual(tcsetattr.call_args.args[2], "settings")

    def test_prompt_password_uses_rich_password_fallback_without_terminal(self):
        with patch.object(ib, "terminal_supports_masked_password", return_value=False):
            with patch.object(ib.Prompt, "ask", return_value="secret") as prompt_ask:
                password = ib.prompt_password("Password")

        self.assertEqual(password, "secret")
        prompt_ask.assert_called_once_with("Password", password=True)

    def test_prompt_password_allows_blank_fallback_for_existing_password(self):
        with patch.object(ib, "terminal_supports_masked_password", return_value=False):
            with patch.object(ib.Prompt, "ask", return_value="") as prompt_ask:
                password = ib.prompt_password(
                    "Password (leave blank to keep current)",
                    allow_blank=True,
                )

        self.assertEqual(password, "")
        prompt_ask.assert_called_once_with(
            "Password (leave blank to keep current)",
            default="",
            password=True,
            show_default=False,
        )

    def test_configure_help_explains_repeated_runs_keep_existing_values(self):
        runner = CliRunner()

        result = runner.invoke(ib.cli, ["configure", "--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("run this command multiple times", result.output)
        self.assertIn("pressing", result.output)
        self.assertIn("Enter keeps the current value", result.output)
        self.assertIn("password", result.output)
        self.assertIn("prompt is left blank", result.output)
        self.assertIn("new", result.output)
        self.assertIn("edit", result.output)
        self.assertIn("delete", result.output)

    def test_command_help_smoke_surfaces_registered_groups_and_output_option(self):
        runner = CliRunner()
        help_cases = [
            (["--help"], ["Infoblox command line client", "configure", "dns", "--output"]),
            (["dns", "--help"], ["Manage Infoblox DNS records", "create", "view", "zone", "--output"]),
            (["configure", "--help"], ["new", "edit", "delete", "use", "--output"]),
            (["dns", "view", "--help"], ["DNS views", "list", "use", "--output"]),
            (["dns", "zone", "--help"], ["DNS zones", "create", "list", "delete", "use", "--output"]),
        ]

        for args, expected_fragments in help_cases:
            with self.subTest(args=args):
                result = runner.invoke(ib.cli, args)

                self.assertEqual(result.exit_code, 0, result.output)
                for fragment in expected_fragments:
                    self.assertIn(fragment, result.output)

    def test_output_option_is_registered_on_all_commands(self):
        self.assert_command_tree_has_output_option(ib.cli)

    def test_structured_output_data_excludes_ref_fields_recursively(self):
        data = {
            "_ref": "record:a/app",
            "ref": "record:a/app",
            "name": "app.example.com",
            "nested": [{"_ref": "record:cname/www", "name": "www.example.com"}],
        }

        self.assertEqual(
            ib.structured_output_data(data),
            {"name": "app.example.com", "nested": [{"name": "www.example.com"}]},
        )

    def test_legacy_default_config_loads_as_default_profile_and_migrates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                ib.CONFIG_DIR.mkdir(mode=0o700)
                ib.CONFIG_FILE.write_text(
                    "\n".join(
                        [
                            "[default]",
                            "server = infoblox.example.com",
                            "username = admin",
                            "password = secret",
                            "wapi_version = v2.12.3",
                            "dns_view = corp",
                            "default_zone = example.com",
                            "verify_ssl = false",
                            "timeout = 17",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )

                cfg = ib.load_config()
                parser = configparser.ConfigParser()
                parser.read(ib.CONFIG_FILE)

        self.assertEqual(cfg["profile"], "default")
        self.assertEqual(cfg["server"], "https://infoblox.example.com")
        self.assertEqual(cfg["password"], "secret")
        self.assertEqual(cfg["dns_view"], "corp")
        self.assertIn(ib.CONFIG_META_SECTION, parser)
        self.assertIn("profile:default", parser)
        self.assertNotIn("default", parser)
        self.assertTrue(parser["profile:default"]["password"].startswith(ib.ENCRYPTED_PASSWORD_PREFIX))

    def test_multi_profile_config_loads_selected_default_profile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                ib.write_config_profiles(
                    "lab",
                    {
                        "prod": {
                            "server": "https://prod.example.com",
                            "username": "prod-user",
                            "password": "prod-secret",
                            "dns_view": "prod-view",
                            "default_zone": "prod.example.com",
                        },
                        "lab": {
                            "server": "https://lab.example.com",
                            "username": "lab-user",
                            "password": "lab-secret",
                            "dns_view": "lab-view",
                            "default_zone": "lab.example.com",
                        },
                    },
                )

                cfg = ib.load_config()

        self.assertEqual(cfg["profile"], "lab")
        self.assertEqual(cfg["server"], "https://lab.example.com")
        self.assertEqual(cfg["username"], "lab-user")
        self.assertEqual(cfg["password"], "lab-secret")
        self.assertEqual(cfg["dns_view"], "lab-view")
        self.assertEqual(cfg["default_zone"], "lab.example.com")

    def test_configure_use_changes_default_profile(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                ib.write_config_profiles(
                    "prod",
                    {
                        "prod": {
                            "server": "https://prod.example.com",
                            "username": "prod-user",
                            "password": "prod-secret",
                        },
                        "lab": {
                            "server": "https://lab.example.com",
                            "username": "lab-user",
                            "password": "lab-secret",
                        },
                    },
                )

                result = runner.invoke(ib.cli, ["configure", "use", "lab"])
                default_profile, _profiles, _legacy = ib.read_config_profiles()

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(default_profile, "lab")
        self.assertIn("SUCCESS", result.output)

    def test_configure_delete_rejects_current_default_profile(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                ib.write_config_profiles(
                    "prod",
                    {
                        "prod": {
                            "server": "https://prod.example.com",
                            "username": "prod-user",
                            "password": "prod-secret",
                        },
                        "lab": {
                            "server": "https://lab.example.com",
                            "username": "lab-user",
                            "password": "lab-secret",
                        },
                    },
                )

                with self.assertRaisesRegex(ib.CliError, "cannot delete default profile"):
                    ib.delete_config_profile("prod")
                default_profile, profiles, _legacy = ib.read_config_profiles()

        self.assertEqual(default_profile, "prod")
        self.assertIn("prod", profiles)

    def test_configure_new_default_creates_profile_and_sets_default(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                with patch.object(ib, "prompt_text", side_effect=["https://new.example.com", "new-user"]):
                    with patch.object(
                        ib.Prompt,
                        "ask",
                        side_effect=["new-secret", "v2.12.3", "new-view"],
                    ):
                        with patch.object(ib.Confirm, "ask", side_effect=[True, False]):
                            with patch.object(
                                ib,
                                "query_dns_view_names_for_config",
                                return_value=["default", "new-view"],
                            ) as query_views:
                                result = runner.invoke(
                                    ib.cli,
                                    ["configure", "new", "new-prof", "--default"],
                                )

                default_profile, profiles, _legacy = ib.read_config_profiles(decrypt_passwords=True)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(default_profile, "new-prof")
        self.assertEqual(profiles["new-prof"]["server"], "https://new.example.com")
        self.assertEqual(profiles["new-prof"]["username"], "new-user")
        self.assertEqual(profiles["new-prof"]["password"], "new-secret")
        self.assertEqual(profiles["new-prof"]["dns_view"], "new-view")
        query_config = query_views.call_args.args[0]
        self.assertEqual(query_config["server"], "https://new.example.com")
        self.assertEqual(query_config["username"], "new-user")
        self.assertEqual(query_config["password"], "new-secret")
        self.assertEqual(query_config["wapi_version"], "v2.12.3")
        self.assertEqual(query_config["verify_ssl"], "true")

    def test_configure_new_falls_back_to_manual_dns_view_when_lookup_fails(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                with patch.object(ib, "prompt_text", side_effect=["https://new.example.com", "new-user"]):
                    with patch.object(
                        ib.Prompt,
                        "ask",
                        side_effect=["new-secret", "v2.12.3", "manual-view"],
                    ):
                        with patch.object(ib.Confirm, "ask", side_effect=[True, False]):
                            with patch.object(
                                ib,
                                "query_dns_view_names_for_config",
                                side_effect=ib.CliError("ERROR: cannot reach Infoblox: timed out"),
                            ):
                                result = runner.invoke(
                                    ib.cli,
                                    ["configure", "new", "new-prof"],
                                )

                _default_profile, profiles, _legacy = ib.read_config_profiles(decrypt_passwords=True)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(profiles["new-prof"]["dns_view"], "manual-view")
        self.assertIn("could not load DNS views", result.output)

    def test_first_configure_fetches_dns_views_for_default_profile(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                with patch.object(ib, "prompt_text", side_effect=["https://new.example.com", "new-user"]):
                    with patch.object(
                        ib.Prompt,
                        "ask",
                        side_effect=["new-secret", "v2.12.3", "corp", "new.example.com"],
                    ):
                        with patch.object(ib.Confirm, "ask", return_value=False):
                            with patch.object(
                                ib,
                                "query_dns_view_names_for_config",
                                return_value=["corp", "default"],
                            ) as query_views:
                                result = runner.invoke(ib.cli, ["configure"])

                default_profile, profiles, _legacy = ib.read_config_profiles(decrypt_passwords=True)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(default_profile, "default")
        self.assertEqual(profiles["default"]["dns_view"], "corp")
        query_config = query_views.call_args.args[0]
        self.assertEqual(query_config["verify_ssl"], "false")

    def test_configure_new_can_choose_default_zone_from_search_results(self):
        runner = CliRunner()
        zones = [
            {"fqdn": "zebra.example.com", "zone_format": "FORWARD", "comment": ""},
            {"fqdn": "app.example.com", "zone_format": "FORWARD", "comment": "Apps"},
            {"fqdn": "1.168.192.in-addr.arpa", "zone_format": "IPV4", "comment": ""},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                with patch.object(ib, "prompt_text", side_effect=["https://new.example.com", "new-user"]):
                    with patch.object(
                        ib.Prompt,
                        "ask",
                        side_effect=["new-secret", "v2.12.3", "corp", "example", "1"],
                    ):
                        with patch.object(ib.Confirm, "ask", side_effect=[True, True]) as confirm_ask:
                            with patch.object(
                                ib,
                                "query_dns_view_names_for_config",
                                return_value=["corp"],
                            ):
                                with patch.object(
                                    ib,
                                    "query_default_zone_candidates_for_config",
                                    return_value=ib.selectable_zone_records(zones),
                                ) as query_zones:
                                    result = runner.invoke(
                                        ib.cli,
                                        ["configure", "new", "new-prof"],
                                    )

                _default_profile, profiles, _legacy = ib.read_config_profiles(decrypt_passwords=True)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(profiles["new-prof"]["dns_view"], "corp")
        self.assertEqual(profiles["new-prof"]["default_zone"], "app.example.com")
        query_config = query_zones.call_args.args[0]
        self.assertEqual(query_config["dns_view"], "corp")
        self.assertTrue(confirm_ask.call_args_list[1].kwargs["default"])

    def test_configure_new_default_zone_falls_back_to_manual_when_zone_lookup_fails(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                with patch.object(ib, "prompt_text", side_effect=["https://new.example.com", "new-user"]):
                    with patch.object(
                        ib.Prompt,
                        "ask",
                        side_effect=[
                            "new-secret",
                            "v2.12.3",
                            "corp",
                            "manual.example.com",
                        ],
                    ):
                        with patch.object(ib.Confirm, "ask", side_effect=[True, True]):
                            with patch.object(
                                ib,
                                "query_dns_view_names_for_config",
                                return_value=["corp"],
                            ):
                                with patch.object(
                                    ib,
                                    "query_default_zone_candidates_for_config",
                                    side_effect=ib.CliError("ERROR: cannot reach Infoblox: timed out"),
                                ):
                                    result = runner.invoke(
                                        ib.cli,
                                        ["configure", "new", "new-prof"],
                                    )

                _default_profile, profiles, _legacy = ib.read_config_profiles(decrypt_passwords=True)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(profiles["new-prof"]["default_zone"], "manual.example.com")
        self.assertIn("could not load DNS zones", result.output)

    def test_query_default_zone_candidates_uses_selected_view_and_keeps_only_forward_zones(self):
        class FakeClient:
            view = "corp"

            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        client = FakeClient()
        config = {
            "server": "https://infoblox.example.com",
            "username": "admin",
            "password": "secret",
            "wapi_version": "v2.12.3",
            "dns_view": "corp",
        }
        zones = [
            {"fqdn": "zebra.example.com"},
            {"fqdn": "dev.example.com", "zone_format": "FORWARD"},
            {"fqdn": "1.168.192.in-addr.arpa", "zone_format": "FORWARD"},
            {"fqdn": "168.192.in-addr.arpa", "zone_format": "IPV4"},
            {"fqdn": ""},
            {"fqdn": "app.example.com"},
        ]

        with patch.object(ib, "InfobloxClient", return_value=client) as client_factory:
            with patch.object(ib, "query_zones", return_value=zones) as query_zones:
                candidates = ib.query_default_zone_candidates_for_config(config)

        self.assertEqual(
            [zone["fqdn"] for zone in candidates],
            ["app.example.com", "dev.example.com", "zebra.example.com"],
        )
        self.assertTrue(client.closed)
        client_factory.assert_called_once_with(config)
        query_zones.assert_called_once_with(client)

    def test_filter_default_zone_candidates_matches_names_and_comments(self):
        zones = ib.selectable_zone_records(
            [
                {"fqdn": "app.example.com", "zone_format": "FORWARD", "comment": "Production apps"},
                {"fqdn": "dev.example.com", "zone_format": "FORWARD", "comment": "Sandbox"},
                {"fqdn": "prod.example.com", "zone_format": "FORWARD", "comment": ""},
                {"fqdn": "1.168.192.in-addr.arpa", "zone_format": "IPV4", "comment": "Reverse"},
            ]
        )

        self.assertEqual(
            [zone["fqdn"] for zone in ib.filter_default_zone_candidates(zones, "prod")],
            ["prod.example.com", "app.example.com"],
        )
        self.assertEqual(
            [zone["fqdn"] for zone in ib.filter_default_zone_candidates(zones, "dev example")],
            ["dev.example.com"],
        )

    def test_zone_picker_key_normalizes_down_arrow_variants(self):
        self.assertEqual(ib.normalize_zone_picker_key("\x1b[B"), "\x1b[B")
        self.assertEqual(ib.normalize_zone_picker_key("\x1bOB"), "\x1b[B")
        self.assertEqual(ib.normalize_zone_picker_key("\x1b[1;2B"), "\x1b[B")
        self.assertEqual(ib.normalize_zone_picker_key("\x1b[A"), "\x1b[A")
        self.assertEqual(ib.normalize_zone_picker_key("\x1bOA"), "\x1b[A")

    def test_zone_picker_key_reads_full_down_arrow_sequence(self):
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"\x1b[B")
            os.close(write_fd)
            write_fd = -1
            with patch.object(ib.sys.stdin, "fileno", return_value=read_fd):
                self.assertEqual(ib.read_zone_picker_key(), "\x1b[B")
        finally:
            os.close(read_fd)
            if write_fd != -1:
                os.close(write_fd)

    def test_realtime_zone_picker_down_arrow_selects_next_zone(self):
        class FakeLive:
            def __init__(self, *args, **kwargs):
                self.updates = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def update(self, renderable):
                self.updates.append(renderable)

        zones = ib.selectable_zone_records(
            [
                {"fqdn": "app.example.com", "zone_format": "FORWARD"},
                {"fqdn": "dev.example.com", "zone_format": "FORWARD"},
            ]
        )

        with patch.object(ib.sys.stdin, "fileno", return_value=0):
            with patch("termios.tcgetattr", return_value="settings"):
                with patch("termios.tcsetattr"):
                    with patch("tty.setcbreak"):
                        with patch.object(ib, "Live", FakeLive):
                            with patch.object(ib, "read_zone_picker_key", side_effect=["\x1b", "\x1b[B", "\n"]):
                                with patch.object(ib.console, "print"):
                                    selected = ib.prompt_default_zone_realtime(zones)

        self.assertEqual(selected, "dev.example.com")

    def test_query_dns_view_names_for_config_uses_view_object_and_closes_client(self):
        class FakeClient:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        client = FakeClient()
        config = {
            "server": "https://infoblox.example.com",
            "username": "admin",
            "password": "secret",
            "wapi_version": "v2.12.3",
            "dns_view": "default",
        }
        records = [{"name": "corp"}, {"name": "default"}, {"name": "corp"}, {"name": ""}]

        with patch.object(ib, "InfobloxClient", return_value=client) as client_factory:
            with patch.object(ib, "paged_query", return_value=records) as paged_query:
                names = ib.query_dns_view_names_for_config(config)

        self.assertEqual(names, ["corp", "default"])
        self.assertTrue(client.closed)
        client_factory.assert_called_once_with(config)
        paged_query.assert_called_once_with(
            client,
            ib.DNS_VIEW_OBJECT,
            ib.dns_view_query_params(),
            warn_on_skip=False,
        )

    def test_configure_edit_updates_existing_profile_and_keeps_blank_password(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                ib.write_config_profiles(
                    "prod",
                    {
                        "prod": {
                            "server": "https://old.example.com",
                            "username": "old-user",
                            "password": "old-secret",
                            "wapi_version": "v2.12.3",
                            "dns_view": "old-view",
                            "default_zone": "old.example.com",
                            "verify_ssl": "true",
                        }
                    },
                )

                with patch.object(ib, "prompt_text", side_effect=["https://new.example.com", "new-user"]):
                    with patch.object(
                        ib.Prompt,
                        "ask",
                        side_effect=["", "v2.13", "new-view", "new.example.com"],
                    ):
                        with patch.object(ib.Confirm, "ask", return_value=False):
                            result = runner.invoke(ib.cli, ["configure", "edit", "prod"])

                default_profile, profiles, _legacy = ib.read_config_profiles(decrypt_passwords=True)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(default_profile, "prod")
        self.assertEqual(profiles["prod"]["server"], "https://new.example.com")
        self.assertEqual(profiles["prod"]["username"], "new-user")
        self.assertEqual(profiles["prod"]["password"], "old-secret")
        self.assertEqual(profiles["prod"]["wapi_version"], "v2.13")
        self.assertEqual(profiles["prod"]["dns_view"], "new-view")
        self.assertEqual(profiles["prod"]["default_zone"], "new.example.com")
        self.assertEqual(profiles["prod"]["verify_ssl"], "false")

    def test_configure_list_outputs_profiles_without_passwords(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                ib.write_config_profiles(
                    "prod",
                    {
                        "prod": {
                            "server": "https://prod.example.com",
                            "username": "prod-user",
                            "password": "prod-secret",
                            "dns_view": "corp",
                            "default_zone": "example.com",
                        }
                    },
                )

                result = runner.invoke(ib.cli, ["configure", "list", "-o", "csv"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            result.output.splitlines(),
            ["profile,default,server,dns_view,default_zone", "prod,True,https://prod.example.com,corp,example.com"],
        )
        self.assertNotIn("secret", result.output)

    def test_configure_list_outputs_empty_json_when_no_profiles_configured(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                result = runner.invoke(ib.cli, ["-o", "jq", "configure", "list"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output), [])

    def test_configure_list_outputs_json_without_passwords(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                ib.write_config_profiles(
                    "lab",
                    {
                        "prod": {
                            "server": "https://prod.example.com",
                            "username": "prod-user",
                            "password": "prod-secret",
                            "dns_view": "prod-view",
                            "default_zone": "prod.example.com",
                        },
                        "lab": {
                            "server": "https://lab.example.com",
                            "username": "lab-user",
                            "password": "lab-secret",
                            "dns_view": "lab-view",
                            "default_zone": "lab.example.com",
                        },
                    },
                )

                result = runner.invoke(ib.cli, ["configure", "list", "-o", "jq"])

        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)
        self.assertEqual(
            data,
            [
                {
                    "profile": "lab",
                    "default": True,
                    "server": "https://lab.example.com",
                    "dns_view": "lab-view",
                    "default_zone": "lab.example.com",
                },
                {
                    "profile": "prod",
                    "default": False,
                    "server": "https://prod.example.com",
                    "dns_view": "prod-view",
                    "default_zone": "prod.example.com",
                },
            ],
        )
        self.assertNotIn("secret", result.output)
        self.assertNotIn("password", result.output)

    def test_configure_use_outputs_json_and_updates_default_profile(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                ib.write_config_profiles(
                    "prod",
                    {
                        "prod": {
                            "server": "https://prod.example.com",
                            "username": "prod-user",
                            "password": "prod-secret",
                        },
                        "lab": {
                            "server": "https://lab.example.com",
                            "username": "lab-user",
                            "password": "lab-secret",
                        },
                    },
                )

                result = runner.invoke(ib.cli, ["configure", "use", "lab", "-o", "jq"])
                default_profile, _profiles, _legacy = ib.read_config_profiles()

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(default_profile, "lab")
        self.assertEqual(
            json.loads(result.output),
            {
                "status": "success",
                "action": "use",
                "profile": "lab",
                "default": True,
                "message": "default profile set to 'lab'",
            },
        )

    def test_configure_delete_outputs_csv_and_removes_profile(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                ib.write_config_profiles(
                    "prod",
                    {
                        "prod": {
                            "server": "https://prod.example.com",
                            "username": "prod-user",
                            "password": "prod-secret",
                        },
                        "lab": {
                            "server": "https://lab.example.com",
                            "username": "lab-user",
                            "password": "lab-secret",
                        },
                    },
                )

                result = runner.invoke(ib.cli, ["configure", "-o", "csv", "delete", "lab"])
                default_profile, profiles, _legacy = ib.read_config_profiles()

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(default_profile, "prod")
        self.assertNotIn("lab", profiles)
        self.assertEqual(
            result.output.splitlines(),
            [
                "status,action,profile,default,message",
                "success,delete,lab,False,profile 'lab' deleted",
            ],
        )

    def test_configure_delete_completion_suggests_only_non_default_profiles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                ib.write_config_profiles(
                    "prod",
                    {
                        "prod": {
                            "server": "https://prod.example.com",
                            "username": "prod-user",
                            "password": "prod-secret",
                        },
                        "lab": {
                            "server": "https://lab.example.com",
                            "username": "lab-user",
                            "password": "lab-secret",
                        },
                        "dev": {
                            "server": "https://dev.example.com",
                            "username": "dev-user",
                            "password": "dev-secret",
                        },
                    },
                )

                items = ib.complete_deletable_profile_names(None, None, "")

        self.assertEqual(items, ["dev", "lab"])

    def test_configure_new_completion_suggests_unused_common_profile_names(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                ib.write_config_profiles(
                    "prod",
                    {
                        "prod": {
                            "server": "https://prod.example.com",
                            "username": "prod-user",
                            "password": "prod-secret",
                        },
                        "lab": {
                            "server": "https://lab.example.com",
                            "username": "lab-user",
                            "password": "lab-secret",
                        },
                    },
                )

                items = ib.complete_new_profile_names(None, None, "p")

        self.assertEqual(items, ["production"])

    def test_configure_profile_completion_fails_quietly_without_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                existing_items = ib.complete_existing_profile_names(None, None, "")
                delete_items = ib.complete_deletable_profile_names(None, None, "")
                new_items = ib.complete_new_profile_names(None, None, "l")

        self.assertEqual(existing_items, [])
        self.assertEqual(delete_items, [])
        self.assertEqual(new_items, ["lab"])

    def test_infoblox_client_reuses_https_connection(self):
        class FakeResponse:
            status = 200

            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

        class FakeHTTPSConnection:
            instances = []

            def __init__(self, host, port=None, timeout=None, context=None):
                self.host = host
                self.port = port
                self.timeout = timeout
                self.context = context
                self.requests = []
                self.closed = False
                FakeHTTPSConnection.instances.append(self)

            def request(self, method, path, body=None, headers=None):
                self.requests.append(
                    {
                        "method": method,
                        "path": path,
                        "body": body,
                        "headers": headers,
                    }
                )

            def getresponse(self):
                return FakeResponse(json.dumps({"request_count": len(self.requests)}).encode("utf-8"))

            def close(self):
                self.closed = True

        config = {
            "server": "https://infoblox.example.com",
            "username": "admin",
            "password": "secret",
            "wapi_version": "v2.12.3",
            "dns_view": "corp",
            "verify_ssl": "true",
            "timeout": "17",
        }
        with patch.object(ib.http.client, "HTTPSConnection", FakeHTTPSConnection):
            client = ib.InfobloxClient(config)
            first = client.request("GET", "record:a", params={"view": "corp"})
            second = client.request("GET", "record:host")

        self.assertEqual(first, {"request_count": 1})
        self.assertEqual(second, {"request_count": 2})
        self.assertEqual(len(FakeHTTPSConnection.instances), 1)
        connection = FakeHTTPSConnection.instances[0]
        self.assertEqual(connection.host, "infoblox.example.com")
        self.assertEqual(connection.timeout, 17)
        self.assertEqual(
            [request["path"] for request in connection.requests],
            ["/wapi/v2.12.3/record:a?view=corp", "/wapi/v2.12.3/record:host"],
        )
        self.assertIn("Authorization", connection.requests[0]["headers"])

    def test_cloned_infoblox_client_does_not_share_connection(self):
        config = {
            "server": "https://infoblox.example.com",
            "username": "admin",
            "password": "secret",
            "wapi_version": "v2.12.3",
            "dns_view": "corp",
            "verify_ssl": "false",
            "timeout": "17",
        }
        client = ib.InfobloxClient(config)
        client._connection = object()

        clone = ib.clone_infoblox_client(client)

        self.assertIsInstance(clone, ib.InfobloxClient)
        self.assertIsNone(clone._connection)
        self.assertEqual(clone.server, client.server)
        self.assertEqual(clone.view, client.view)
        self.assertEqual(clone.timeout, client.timeout)
        self.assertEqual(clone.verify_ssl, client.verify_ssl)

    def test_dns_create_rejects_negative_ttl_option(self):
        runner = CliRunner()

        with patch.object(ib, "run_dns_create") as run_dns_create:
            result = runner.invoke(
                ib.cli,
                ["dns", "create", "a", "app", "192.0.2.10", "-t", "-1"],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("ttl must be 0 or greater", result.output)
        run_dns_create.assert_not_called()

    def test_dns_create_rejects_invalid_comment_option(self):
        runner = CliRunner()

        with patch.object(ib, "run_dns_create") as run_dns_create:
            result = runner.invoke(
                ib.cli,
                ["dns", "create", "a", "app", "192.0.2.10", "-c", "bad|comment"],
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
        self.assertIn("NAME VALUE", help_text)
        self.assertNotIn("-n, --name TEXT", help_text)
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
        self.assertEqual(error_lines[0].spans[0].start, 0)
        self.assertEqual(error_lines[0].spans[0].end, len("Error:"))
        self.assertEqual(str(error_lines[0].spans[0].style), "bold red")
        err_print.assert_any_call("context")

    def test_dns_command_missing_config_guides_to_configure(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                result = runner.invoke(ib.cli, ["dns", "search", "app"])

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIsInstance(result.exception, ib.CliError)
        self.assertIn("Run: ib configure", str(result.exception))

    def test_dns_delete_missing_record_name_error_prints_ptr_hint(self):
        with patch.object(ib.sys, "argv", ["ib", "dns", "delete"]):
            with patch.object(ib, "dns_context_panel", return_value="context") as dns_context_panel:
                with patch.object(ib.err_console, "print") as err_print:
                    exit_code = ib.main()

        self.assertEqual(exit_code, 2)
        dns_context_panel.assert_called_once_with()
        printed_texts = [call.args[0] for call in err_print.call_args_list if call.args]
        self.assertTrue(
            any(
                isinstance(item, ib.Text)
                and "Missing argument 'RECORD_NAME'" in item.plain
                for item in printed_texts
            )
        )
        self.assertTrue(
            any(
                isinstance(item, ib.Text)
                and "ib dns delete ptr IP_ADDRESS" in item.plain
                for item in printed_texts
            )
        )
        self.assertTrue(
            any(
                isinstance(item, ib.Text)
                and "ib dns delete ptr <ip-address>" in item.plain
                for item in printed_texts
            )
        )
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
        self.assertEqual(error_line.spans[0].start, 0)
        self.assertEqual(error_line.spans[0].end, len("Error:"))
        self.assertEqual(str(error_line.spans[0].style), "bold red")
        self.assertTrue(
            all("red" not in str(span.style) for span in error_line.spans[1:]),
            error_line.spans,
        )

    def test_error_keyword_formatter_styles_every_error_keyword(self):
        text = ib.styled_error_keyword_text("Error: outer Error: inner")
        self.assertEqual(text.plain, "Error: outer Error: inner")
        red_spans = [span for span in text.spans if str(span.style) == "bold red"]
        self.assertEqual(len(red_spans), 2)
        self.assertEqual(text.plain[red_spans[0].start:red_spans[0].end], "Error:")
        self.assertEqual(text.plain[red_spans[1].start:red_spans[1].end], "Error:")

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
        self.assertEqual(context["name"], "app")
        self.assertEqual(context["--zone"], "example.com")
        self.assertEqual(context["target_record"], "app.example.com")
        self.assertEqual(context["resolved_zone"], "example.com")
        self.assertEqual(context["view"], "corp")
        self.assertEqual(context["active_zone"], "example.com")
        self.assertEqual(context["-t/--ttl"], 300)
        self.assertIs(context["--noptr"], True)
        self.assertEqual(context["-c/--comment"], "Application VIP")
        self.assertEqual(context["wapi_object"], "record:a")

        test_console = ib.Console(width=180, force_terminal=False)
        with test_console.capture() as capture:
            test_console.print(ib.dns_create_error_context_panel(context))
        output = capture.get()
        for expected in (
            "name",
            "--zone",
            "target record",
            "Context",
            "View=",
            "Zone=",
            "-t/--ttl",
            "--noptr",
            "-c/--comment",
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
        self.assertEqual(
            context["hints"],
            [
                "Infoblox rejected the A record because IP address 10.1.1.1 is not allowed "
                "for DNS zone latrobe-test.edu.au.",
                "Use an IP associated with that zone.",
                "Choose the correct zone with --zone or a fully qualified name.",
                "Update the zone network association in Infoblox.",
                "Run `ib dns zone view latrobe-test.edu.au` to view the network association "
                "for this zone.",
            ],
        )
        self.assertIn("IP address 10.1.1.1", context["hint"])
        self.assertIn("DNS zone latrobe-test.edu.au", context["hint"])
        self.assertIn("--zone or a fully qualified name", context["hint"])
        self.assertIn(
            "Run `ib dns zone view latrobe-test.edu.au` to view the network association",
            context["hint"],
        )
        self.assertEqual(
            str(ib.dns_create_error_hint_value(context["hints"])),
            "- Infoblox rejected the A record because IP address 10.1.1.1 is not allowed "
            "for DNS zone latrobe-test.edu.au.\n"
            "- Use an IP associated with that zone.\n"
            "- Choose the correct zone with --zone or a fully qualified name.\n"
            "- Update the zone network association in Infoblox.\n"
            "- Run `ib dns zone view latrobe-test.edu.au` to view the network association "
            "for this zone.",
        )

        test_console = ib.Console(width=180, force_terminal=False)
        with test_console.capture() as capture:
            test_console.print(ib.dns_create_error_context_panel(context))
        output = capture.get()
        self.assertIn("Hints", output)
        self.assertIn("target record", output)
        self.assertIn("test.latrobe-test.edu.au", output)
        self.assertIn("- Use an IP associated with that zone.", output)
        self.assertIn("- Choose the correct zone with --zone or a fully qualified name.", output)
        self.assertIn("- Update the zone network association in Infoblox.", output)
        self.assertIn("ib dns zone view latrobe-test.edu.au", output)
        self.assertIn("network association", output)

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

    def test_environment_view_overrides_configured_view(self):
        with patch.dict(os.environ, {ib.DEFAULT_VIEW_ENV: "env-view"}, clear=False):
            with patch.object(ib, "read_session_view", return_value=None):
                view = ib.resolve_dns_view({"dns_view": "configured-view"})

        self.assertEqual(view, "env-view")

    def test_session_view_overrides_environment_and_configured_view(self):
        with patch.dict(os.environ, {ib.DEFAULT_VIEW_ENV: "env-view"}, clear=False):
            with patch.object(ib, "read_session_view", return_value="session-view"):
                view = ib.resolve_dns_view({"dns_view": "configured-view"})

        self.assertEqual(view, "session-view")

    def test_load_config_applies_session_view_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                ib.write_config_profiles(
                    "prod",
                    {
                        "prod": {
                            "server": "https://prod.example.com",
                            "username": "prod-user",
                            "password": "prod-secret",
                            "dns_view": "configured-view",
                        }
                    },
                )
                with patch.object(ib, "read_session_view", return_value="session-view"):
                    with patch.dict(os.environ, {}, clear=False):
                        os.environ.pop(ib.DEFAULT_VIEW_ENV, None)
                        cfg = ib.load_config()

        self.assertEqual(cfg["dns_view"], "session-view")

    def test_session_zone_is_scoped_to_parent_shell_pid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "session_zone_dir", return_value=Path(tmpdir)):
                with patch.object(ib.os, "getppid", return_value=111):
                    ib.write_session_zone("test.local", "prod")
                    self.assertEqual(ib.read_session_zone("prod"), "test.local")
                with patch.object(ib.os, "getppid", return_value=222):
                    self.assertIsNone(ib.read_session_zone("prod"))
                    with patch.dict(os.environ, {}, clear=False):
                        os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
                        zone = ib.resolve_dns_zone(
                            {"profile": "prod", "default_zone": "configured.example.com"}
                        )

        self.assertEqual(zone, "configured.example.com")

    def test_session_zone_is_scoped_to_selected_profile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "session_zone_dir", return_value=Path(tmpdir)):
                with patch.object(ib.os, "getppid", return_value=111):
                    ib.write_session_zone("prod.example.com", "prod")
                    with patch.dict(os.environ, {}, clear=False):
                        os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
                        prod_zone = ib.resolve_dns_zone(
                            {"profile": "prod", "default_zone": "prod-default.example.com"}
                        )
                        lab_zone = ib.resolve_dns_zone(
                            {"profile": "lab", "default_zone": "lab.example.com"}
                        )

        self.assertEqual(prod_zone, "prod.example.com")
        self.assertEqual(lab_zone, "lab.example.com")

    def test_active_zone_status_ignores_session_zone_from_other_profile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.config_path_patch(tmpdir):
                ib.write_config_profiles(
                    "lab",
                    {
                        "prod": {
                            "server": "https://prod.example.com",
                            "username": "prod-user",
                            "password": "prod-secret",
                            "default_zone": "prod.example.com",
                        },
                        "lab": {
                            "server": "https://lab.example.com",
                            "username": "lab-user",
                            "password": "lab-secret",
                            "default_zone": "lab.example.com",
                        },
                    },
                )
                with patch.object(ib, "session_zone_dir", return_value=Path(tmpdir) / "sessions"):
                    ib.write_session_zone("prod-session.example.com", "prod")
                    with patch.dict(os.environ, {}, clear=False):
                        os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
                        zone, source = ib.active_zone_status()

        self.assertEqual(zone, "lab.example.com")
        self.assertEqual(source, "configured default")

    def test_session_view_is_scoped_to_parent_shell_pid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "session_view_dir", return_value=Path(tmpdir)):
                with patch.object(ib.os, "getppid", return_value=111):
                    ib.write_session_view("session-view")
                    self.assertEqual(ib.read_session_view(), "session-view")
                with patch.object(ib.os, "getppid", return_value=222):
                    self.assertIsNone(ib.read_session_view())
                    with patch.dict(os.environ, {}, clear=False):
                        os.environ.pop(ib.DEFAULT_VIEW_ENV, None)
                        view = ib.resolve_dns_view({"dns_view": "configured-view"})

        self.assertEqual(view, "configured-view")

    def test_zone_use_writes_session_zone(self):
        with patch.object(ib, "write_session_zone") as write_session_zone:
            with patch.object(ib, "session_profile_name", return_value="prod"):
                with patch.object(ib, "print_success") as print_success:
                    with patch.object(ib, "print_note") as print_note:
                        ib.run_dns_zone_use("test.local.")

        write_session_zone.assert_called_once_with("test.local", "prod")
        print_success.assert_called_once()
        print_note.assert_called_once()

    def test_dns_view_use_writes_session_view_after_validating(self):
        class FakeClient:
            def close(self):
                pass

        with patch.object(ib, "load_config", return_value={"dns_view": "corp"}):
            with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                with patch.object(ib, "query_dns_view_names", return_value=["corp", "Lab View"]):
                    with patch.object(ib, "write_session_view") as write_session_view:
                        with patch.object(ib, "print_success") as print_success:
                            with patch.object(ib, "print_note") as print_note:
                                ib.run_dns_view_use("lab view")

        write_session_view.assert_called_once_with("Lab View")
        print_success.assert_called_once()
        self.assertEqual(print_note.call_count, 2)
        self.assertIn(ib.DEFAULT_VIEW_ENV, print_note.call_args_list[1].args[0])

    def test_dns_view_use_rejects_unknown_view(self):
        class FakeClient:
            def close(self):
                pass

        with patch.object(ib, "load_config", return_value={"dns_view": "corp"}):
            with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                with patch.object(ib, "query_dns_view_names", return_value=["corp"]):
                    with self.assertRaisesRegex(ib.CliError, "was not found"):
                        ib.run_dns_view_use("missing")

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

    def test_start_zone_serial_cache_revalidation_runs_hidden_command_without_completion_env(self):
        with patch.dict(os.environ, {"_IB_COMPLETE": "bash_complete", "KEEP_ME": "1"}, clear=False):
            with patch.object(ib.subprocess, "Popen") as popen:
                ib.start_zone_serial_cache_revalidation()

        command = popen.call_args.args[0]
        options = popen.call_args.kwargs
        self.assertEqual(command[0], ib.sys.executable)
        self.assertEqual(command[-1], "_refresh-zone-serial-cache")
        self.assertEqual(options["stdin"], ib.subprocess.DEVNULL)
        self.assertEqual(options["stdout"], ib.subprocess.DEVNULL)
        self.assertEqual(options["stderr"], ib.subprocess.DEVNULL)
        self.assertNotIn("_IB_COMPLETE", options["env"])
        self.assertEqual(options["env"]["KEEP_ME"], "1")

    def test_hidden_zone_serial_refresh_command_calls_runner(self):
        self.assertTrue(ib.cli.commands["_refresh-zone-serial-cache"].hidden)
        with patch.object(ib, "run_zone_serial_cache_revalidation") as run_refresh:
            ib.cli.commands["_refresh-zone-serial-cache"].callback()

        run_refresh.assert_called_once_with()

    def test_clear_dns_cache_removes_search_and_completion_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "allrecords-cache"
            cache_dir.mkdir()
            (cache_dir / "cache.sqlite3").write_text("cache", encoding="utf-8")
            (cache_dir / "legacy.json").write_text("cache", encoding="utf-8")
            zone_cache = Path(tmpdir) / "zone-completion-cache.json"
            zone_cache.write_text("cache", encoding="utf-8")

            with patch.object(ib, "ALLRECORDS_CACHE_DIR", cache_dir):
                with patch.object(ib, "ZONE_COMPLETION_CACHE_FILE", zone_cache):
                    ib.clear_dns_cache()

            self.assertFalse(cache_dir.exists())
            self.assertFalse(zone_cache.exists())

    def test_search_cache_db_uses_wal_journal_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                with ib.connect_search_cache_db() as conn:
                    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                    synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]

        self.assertEqual(str(journal_mode).lower(), "wal")
        self.assertEqual(synchronous, 1)

    def test_refresh_dns_cache_after_update_clears_then_warms_in_background(self):
        with patch.object(ib, "clear_dns_cache") as clear_cache:
            with patch.object(ib, "start_search_cache_prewarm") as start_prewarm:
                ORIGINAL_REFRESH_DNS_CACHE_AFTER_UPDATE()

        clear_cache.assert_called_once_with()
        start_prewarm.assert_called_once_with()

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

    def test_zone_serial_cache_revalidation_refreshes_serials_under_lock(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        client = FakeClient()
        zones = [{"fqdn": "example.com", "primary_type": "Grid", "soa_serial_number": 2}]
        with patch.object(ib, "load_config", return_value={"server": "ignored"}):
            with patch.object(ib, "InfobloxClient", return_value=client):
                with patch.object(
                    ib,
                    "acquire_zone_serial_cache_revalidate_lock",
                    return_value=(123, "token"),
                ):
                    with patch.object(ib, "release_zone_serial_cache_revalidate_lock") as release_lock:
                        with patch.object(ib, "fetch_zone_serials", return_value=zones) as fetch:
                            with patch.object(ib, "write_zone_serial_cache") as write_cache:
                                ib.run_zone_serial_cache_revalidation()

        fetch.assert_called_once_with(client)
        write_cache.assert_called_once_with(client, zones)
        release_lock.assert_called_once_with(client, (123, "token"))

    def test_zone_serial_cache_revalidation_skips_when_fresh_lock_exists(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                ib.ensure_allrecords_cache_dir()
                ib.zone_serial_cache_revalidate_lock_file(client).write_text(
                    "existing\n",
                    encoding="utf-8",
                )
                with patch.object(ib, "load_config", return_value={"server": "ignored"}):
                    with patch.object(ib, "InfobloxClient", return_value=client):
                        with patch.object(ib, "fetch_zone_serials") as fetch:
                            with patch.object(ib, "write_zone_serial_cache") as write_cache:
                                ib.run_zone_serial_cache_revalidation()

        fetch.assert_not_called()
        write_cache.assert_not_called()

    def test_dns_delete_completion_searches_global_records(self):
        class FakeClient:
            view = "corp"

        client = FakeClient()
        records = [
            (
                "a",
                {
                    "type": "record:a",
                    "name": "app.example.com",
                    "zone": "example.com",
                    "address": "192.0.2.10",
                    "comment": "web",
                },
            ),
            (
                "cname",
                {
                    "type": "record:cname",
                    "name": "www.other.example.net",
                    "zone": "other.example.net",
                    "record": {"canonical": "app.example.com"},
                },
            ),
            (
                "ptr",
                {
                    "type": "record:ptr",
                    "name": "10.2.0.192.in-addr.arpa",
                    "zone": "2.0.192.in-addr.arpa",
                    "ipv4addr": "192.0.2.10",
                    "ptrdname": "app.example.com",
                },
            ),
            (
                "record",
                {
                    "type": "record:ns",
                    "name": "example.com",
                    "zone": "example.com",
                    "record": {"nameserver": "ns1.example.com"},
                },
            ),
            (
                "record",
                {
                    "type": "sharedrecord:ns",
                    "name": "shared.example.com",
                    "zone": "example.com",
                    "record": {"nameserver": "ns2.example.com"},
                },
            ),
            (
                "unsupported",
                {
                    "type": "UNSUPPORTED",
                    "name": "ns1.example.com",
                    "zone": "example.com",
                    "record": None,
                },
            ),
        ]

        with patch.object(ib, "load_config", return_value={"server": "ignored"}):
            with patch.object(ib, "InfobloxClient", return_value=client):
                with patch.object(ib, "collect_dns_search_results", return_value=records) as collect:
                    items = ib.complete_dns_delete_records(None, None, "app")

        collect.assert_called_once_with(client, "app", False, None, zone_filter=ib.is_forward_zone)
        self.assertEqual([item.value for item in items], ["app.example.com", "www.other.example.net"])
        self.assertNotIn("10.2.0.192.in-addr.arpa", [item.value for item in items])
        self.assertNotIn("example.com", [item.value for item in items])
        self.assertNotIn("shared.example.com", [item.value for item in items])
        self.assertNotIn("ns1.example.com", [item.value for item in items])
        self.assertIn("A", items[0].help)
        self.assertIn("192.0.2.10", items[0].help)
        self.assertIn("zone=example.com", items[0].help)
        self.assertIn("web", items[0].help)

    def test_dns_delete_completion_includes_ptr_command_without_reverse_records(self):
        records = [
            (
                "ptr",
                {
                    "type": "record:ptr",
                    "name": "3.1.168.192.in-addr.arpa",
                    "zone": "1.168.192.in-addr.arpa",
                    "ipv4addr": "192.168.1.3",
                    "ptrdname": "host.example.com",
                },
            ),
        ]

        with patch.object(ib, "load_config", return_value={"server": "ignored"}):
            with patch.object(ib, "InfobloxClient"):
                with patch.object(ib, "collect_dns_search_results", return_value=records):
                    items = ib.complete_dns_delete_records(None, None, "p")

        self.assertEqual([item.value for item in items], ["ptr"])
        self.assertIn("full IP address", items[0].help)

    def test_dns_delete_completion_searches_forward_zones_only(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        queried_zones = []

        def fake_paged_query(client, object_type, params, warn_on_skip=True):
            if object_type == ib.ZONE_OBJECT:
                return [
                    {
                        "fqdn": "example.com",
                        "zone_format": "FORWARD",
                        "primary_type": "Grid",
                        "soa_serial_number": 1,
                    },
                    {
                        "fqdn": "2.0.192.in-addr.arpa",
                        "zone_format": "IPV4",
                        "primary_type": "Grid",
                        "soa_serial_number": 2,
                    },
                    {
                        "fqdn": "8.b.d.0.1.0.0.2.ip6.arpa",
                        "zone_format": "IPV6",
                        "primary_type": "Grid",
                        "soa_serial_number": 3,
                    },
                ]
            if object_type == ib.ALLRECORDS_OBJECT:
                queried_zones.append(params["zone"])
                self.assertEqual(params["zone"], "example.com")
                return [
                    {
                        "_ref": "allrecords/app",
                        "type": "record:a",
                        "name": "app.example.com",
                        "zone": "example.com",
                        "address": "192.0.2.10",
                    }
                ]
            raise AssertionError(f"unexpected object type: {object_type}")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                with patch.object(ib, "load_config", return_value={"server": "ignored"}):
                    with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                        with patch.object(ib, "paged_query", side_effect=fake_paged_query):
                            items = ib.complete_dns_delete_records(None, None, "app")

        self.assertEqual(queried_zones, ["example.com"])
        self.assertEqual([item.value for item in items], ["app.example.com"])

    def test_dns_delete_second_argument_does_not_complete_zones_for_ptr_mode(self):
        class FakeContext:
            params = {"record_name": "ptr"}

        with patch.object(ib, "complete_forward_zone_names") as complete_forward_zone_names:
            items = ib.complete_dns_delete_zone_or_ip(FakeContext(), None, "")

        self.assertEqual(items, [])
        complete_forward_zone_names.assert_not_called()

    def test_dns_delete_second_argument_completes_forward_zones_only(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        class FakeContext:
            params = {"record_name": "app"}

        zones = [
            {
                "fqdn": "example.com",
                "zone_format": "FORWARD",
                "primary_type": "Grid",
                "soa_serial_number": 1,
            },
            {
                "fqdn": "2.0.192.in-addr.arpa",
                "zone_format": "IPV4",
                "primary_type": "Grid",
                "soa_serial_number": 2,
            },
            {
                "fqdn": "8.b.d.0.1.0.0.2.ip6.arpa",
                "zone_format": "IPV6",
                "primary_type": "Grid",
                "soa_serial_number": 3,
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                with patch.object(ib, "load_config", return_value={"server": "ignored"}):
                    with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                        with patch.object(ib, "query_zone_serials", return_value=zones):
                            items = ib.complete_dns_delete_zone_or_ip(FakeContext(), None, "")

        self.assertEqual(items, ["example.com"])

    def test_dns_delete_accepts_completed_fqdn_without_active_zone(self):
        class FakeClient:
            view = "corp"

            def __init__(self):
                self.deleted_ref = None

            def request(self, method, object_type, payload=None, params=None):
                if method == "DELETE":
                    self.deleted_ref = object_type
                    return None
                raise AssertionError("run_dns_delete should use patched safe_query for GET requests")

        client = FakeClient()
        queries = []

        def fake_safe_query(client, object_type, params):
            queries.append((object_type, params["name"]))
            if object_type == "record:a" and params["name"] == "app.other.example.net":
                return [
                    {
                        "_ref": "record:a/app",
                        "name": "app.other.example.net",
                        "zone": "other.example.net",
                        "ipv4addr": "192.0.2.10",
                    }
                ]
            return []

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "load_config", return_value={}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    with patch.object(ib, "InfobloxClient", return_value=client):
                        with patch.object(ib, "safe_query", side_effect=fake_safe_query):
                            with patch.object(ib, "print_success"):
                                ib.run_dns_delete("app.other.example.net", None)

        self.assertEqual(client.deleted_ref, "record:a/app")
        self.assertTrue(all(name == "app.other.example.net" for _object_type, name in queries))
        self.cache_refresh.assert_called_once_with()

    def test_dns_delete_no_match_for_ip_suggests_ptr_delete_command(self):
        class FakeClient:
            view = "corp"

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                        with patch.object(ib, "safe_query", return_value=[]):
                            with self.assertRaises(ib.CliError) as caught:
                                ib.run_dns_delete("192.168.1.3", None)

        message = str(caught.exception)
        self.assertIn("ERROR: no forward DNS record found", message)
        self.assertIn("HINT:", message)
        self.assertIn("ib dns delete ptr 192.168.1.3", message)
        self.cache_refresh.assert_not_called()

    def test_dns_delete_no_match_for_name_suggests_ptr_delete_form(self):
        class FakeClient:
            view = "corp"

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                        with patch.object(ib, "safe_query", return_value=[]):
                            with self.assertRaises(ib.CliError) as caught:
                                ib.run_dns_delete("missing", None)

        message = str(caught.exception)
        self.assertIn("ERROR: no forward DNS record found", message)
        self.assertIn("ib dns delete ptr <ip-address>", message)
        self.cache_refresh.assert_not_called()

    def test_dns_delete_ptr_deletes_reverse_record_by_ip_address(self):
        class FakeClient:
            view = "corp"

            def __init__(self):
                self.deleted_ref = None

            def request(self, method, object_type, payload=None, params=None):
                if method == "DELETE":
                    self.deleted_ref = object_type
                    return None
                raise AssertionError("run_dns_delete_ptr should use patched safe_query for GET requests")

        client = FakeClient()
        seen_params = {}

        def fake_safe_query(client, object_type, params):
            seen_params.update(params)
            self.assertEqual(object_type, "record:ptr")
            return [
                {
                    "_ref": "record:ptr/reverse",
                    "ipv4addr": "192.168.1.3",
                    "ptrdname": "host.example.com",
                    "zone": "1.168.192.in-addr.arpa",
                }
            ]

        zones = [
            {"fqdn": "168.192.in-addr.arpa", "zone_format": "IPV4"},
            {"fqdn": "1.168.192.in-addr.arpa", "zone_format": "IPV4"},
            {"fqdn": "example.com", "zone_format": "FORWARD"},
        ]

        with patch.object(ib, "load_config", return_value={"server": "ignored"}):
            with patch.object(ib, "InfobloxClient", return_value=client):
                with patch.object(ib, "query_zones", return_value=zones) as query_zones:
                    with patch.object(ib, "safe_query", side_effect=fake_safe_query):
                        with patch.object(ib, "print_success") as print_success:
                            ib.run_dns_delete("ptr", "192.168.1.3")

        query_zones.assert_called_once_with(client)
        self.assertEqual(seen_params["ipv4addr"], "192.168.1.3")
        self.assertEqual(client.deleted_ref, "record:ptr/reverse")
        print_success.assert_called_once_with(
            "SUCCESS: deleted PTR record 192.168.1.3 from reverse zone 1.168.192.in-addr.arpa"
        )
        self.cache_refresh.assert_called_once_with()

    def test_dns_delete_ptr_requires_full_ip_address(self):
        with self.assertRaisesRegex(ib.CliError, "Use: ib dns delete ptr <ip-address>"):
            ib.run_dns_delete("ptr", None)

        with self.assertRaisesRegex(ib.CliError, "Use: ib dns delete ptr <ip-address>"):
            ib.run_dns_delete("ptr", "192.168.1.0/24")

    def test_dns_zone_create_refreshes_cache_after_success(self):
        class FakeClient:
            view = "corp"

            def request(self, method, object_type, payload=None, params=None):
                self.request_args = (method, object_type, payload)
                return "zone_auth/example"

        client = FakeClient()
        with patch.object(ib, "load_config", return_value={"server": "ignored"}):
            with patch.object(ib, "InfobloxClient", return_value=client):
                with patch.object(ib, "print_success"):
                    with patch.object(ib, "print_note"):
                        ib.run_dns_zone_create("example.com", "FORWARD", None, None)

        self.assertEqual(client.request_args[0], "POST")
        self.assertEqual(client.request_args[1], ib.ZONE_OBJECT)
        self.cache_refresh.assert_called_once_with()

    def test_dns_zone_delete_refreshes_cache_after_success(self):
        class FakeClient:
            view = "corp"

            def __init__(self):
                self.deleted_ref = None

            def request(self, method, object_type, payload=None, params=None):
                if method == "DELETE":
                    self.deleted_ref = object_type
                    return None
                raise AssertionError("run_dns_zone_delete should use patched safe_query for GET requests")

        client = FakeClient()
        with patch.object(ib, "load_config", return_value={"server": "ignored"}):
            with patch.object(ib, "InfobloxClient", return_value=client):
                with patch.object(ib, "safe_query", return_value=[{"_ref": "zone_auth/example"}]):
                    with patch.object(ib, "print_success"):
                        ib.run_dns_zone_delete("example.com")

        self.assertEqual(client.deleted_ref, "zone_auth/example")
        self.cache_refresh.assert_called_once_with()

    def test_usage_help_includes_current_view_and_active_zone(self):
        with patch.dict(os.environ, {ib.DEFAULT_ZONE_ENV: "example.com"}, clear=False):
            with patch.object(ib, "default_config_values", return_value={"dns_view": "corp"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    with ib.cli.make_context("ib", [], resilient_parsing=True) as ctx:
                        help_text = ib.cli.get_help(ctx)

        self.assertIn("DNS Context", help_text)
        self.assertIn("corp", help_text)
        self.assertIn("example.com", help_text)

    def test_root_help_includes_global_output_option(self):
        runner = CliRunner()
        result = runner.invoke(ib.cli, ["--help"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("-o, --output", result.output)
        self.assertIn("jq", result.output)
        self.assertIn("csv", result.output)

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
        self.assertIn("Zone rule", result.output)
        self.assertIn("Current target", result.output)
        self.assertIn("without --zone", result.output)
        self.assertIn("Example", result.output)
        self.assertIn("Setup:", result.output)
        self.assertIn("ib dns zone use example.com", result.output)
        self.assertIn("ib dns create host", result.output)
        self.assertIn("app 192.0.2.10", result.output)
        self.assertIn("Command:", result.output)
        self.assertIn("Creates:", result.output)
        self.assertIn("HOST record", result.output)
        self.assertIn("app.example.com", result.output)
        self.assertIn("Application host", result.output)
        self.assertIn("-n/--name", result.output)
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

    def test_dns_edit_help_explains_type_change_limitation(self):
        runner = CliRunner()
        result = runner.invoke(ib.cli, ["dns", "edit", "--help"], prog_name="ib")

        self.assertEqual(result.exit_code, 0, result.output)
        normalized_output = " ".join(result.output.split())
        self.assertIn("TYPE completion", normalized_output)
        self.assertIn("Infoblox does not support changing", normalized_output)
        self.assertIn("delete and recreate", normalized_output.lower())

    def test_help_overrides_structured_output_mode(self):
        runner = CliRunner()
        result = runner.invoke(ib.cli, ["-o", "jq", "dns", "edit", "-h"], prog_name="ib")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Usage: ib dns edit", result.output)
        self.assertIn("Edit an existing DNS record", result.output)
        self.assertFalse(result.output.lstrip().startswith(("{", "[")))

    def test_dns_delete_help_explains_forward_and_reverse_usage(self):
        runner = CliRunner()
        with patch.dict(os.environ, {ib.DEFAULT_ZONE_ENV: "example.com"}, clear=False):
            with patch.object(ib, "default_config_values", return_value={"dns_view": "corp"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    result = runner.invoke(
                        ib.cli,
                        ["dns", "delete", "--help"],
                        color=True,
                        prog_name="ib",
                    )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Usage: ib dns delete [OPTIONS] RECORD_NAME [ZONE]", result.output)
        self.assertIn("ib dns delete ptr IP_ADDRESS", result.output)
        self.assertIn("Delete Record Usage", result.output)
        self.assertIn("Forward", result.output)
        self.assertIn("Reverse PTR", result.output)
        self.assertIn("ib dns delete app", result.output)
        self.assertIn("app.example.com", result.output)
        self.assertIn("ib dns delete ptr", result.output)
        self.assertIn("192.168.1.3", result.output)
        self.assertIn("Normal delete is for forward records only", result.output)
        self.assertIn("Use the Reverse", result.output)
        self.assertIn("PTR form for reverse entries", result.output)

    def test_dns_context_panel_uses_one_shared_borderless_line(self):
        with patch.object(ib, "active_profile_status", return_value=("prod", "default profile")):
            with patch.object(ib, "current_view_status", return_value=("corp", "configured")):
                with patch.object(ib, "active_zone_status", return_value=("example.com", "shell session")):
                    context_line = ib.dns_context_panel("Current DNS Context")

        self.assertIsInstance(context_line, ib.Text)
        self.assertEqual(
            context_line.plain,
            "Current DNS Context: Profile=prod | View=corp | Zone=example.com",
        )
        self.assertNotIn("\n", context_line.plain)
        self.assertFalse(any(span.style and "on " in str(span.style) for span in context_line.spans))

    def test_dns_context_omits_redundant_missing_active_zone_source(self):
        with patch.object(ib, "active_profile_status", return_value=("default", "default profile")):
            with patch.object(ib, "current_view_status", return_value=("corp", "configured")):
                with patch.object(ib, "active_zone_status", return_value=(None, "not set")):
                    context_line = ib.dns_context_panel("Current DNS Context")

        self.assertEqual(
            context_line.plain,
            "Current DNS Context: Profile=default | View=corp | Zone=not set",
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

    def test_dns_view_list_prints_context_before_view_table(self):
        class FakeClient:
            def close(self):
                pass

        with patch.object(ib, "load_config", return_value={"dns_view": "corp"}):
            with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                with patch.object(ib, "query_dns_view_names", return_value=["corp", "lab"]):
                    with patch.object(ib, "dns_context_panel", return_value="context"):
                        with patch.object(ib, "dns_view_table", return_value="views"):
                            with patch.object(ib.console, "print") as print_mock:
                                ib.run_dns_view_list()

        printed = [call.args[0] for call in print_mock.call_args_list]
        self.assertEqual(printed, ["context", "views"])

    def test_dns_view_list_structured_output_marks_active_view(self):
        class FakeClient:
            def close(self):
                pass

        with patch.object(ib, "load_config", return_value={"dns_view": "lab"}):
            with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                with patch.object(ib, "query_dns_view_names", return_value=["corp", "lab"]):
                    with patch.object(ib, "current_output_format", return_value="jq"):
                        with patch.object(ib, "emit_structured") as emit_structured:
                            ib.run_dns_view_list()

        emit_structured.assert_called_once_with(
            [{"view": "corp", "active": False}, {"view": "lab", "active": True}],
            ["view", "active"],
        )

    def test_dns_zone_list_outputs_csv_with_global_output_option(self):
        class FakeClient:
            view = "corp"

        zones = [
            {
                "_ref": "zone_auth/example",
                "fqdn": "example.com",
                "view": "corp",
                "zone_format": "FORWARD",
                "ns_group": "default",
                "comment": "Production",
            }
        ]
        runner = CliRunner()

        with patch.object(ib, "load_config", return_value={"server": "ignored"}):
            with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                with patch.object(ib, "query_zones", return_value=zones):
                    result = runner.invoke(ib.cli, ["-o", "csv", "dns", "zone", "list"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            result.output.splitlines(),
            [
                "zone,view,format,ns_group,comment",
                "example.com,corp,FORWARD,default,Production",
            ],
        )

    def test_dns_zone_use_outputs_json_with_global_output_option(self):
        runner = CliRunner()

        with patch.object(ib, "write_session_zone") as write_session_zone:
            with patch.object(ib, "session_profile_name", return_value="prod"):
                result = runner.invoke(ib.cli, ["-o", "jq", "dns", "zone", "use", "example.com"])

        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["action"], "use")
        self.assertEqual(data["type"], "ZONE")
        self.assertEqual(data["zone"], "example.com")
        self.assertNotIn("ref", data)
        write_session_zone.assert_called_once_with("example.com", "prod")

    def test_dns_view_use_outputs_json_with_global_output_option(self):
        class FakeClient:
            def close(self):
                pass

        runner = CliRunner()
        with patch.object(ib, "load_config", return_value={"dns_view": "corp"}):
            with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                with patch.object(ib, "query_dns_view_names", return_value=["corp", "Lab View"]):
                    with patch.object(ib, "write_session_view") as write_session_view:
                        result = runner.invoke(
                            ib.cli,
                            ["-o", "jq", "dns", "view", "use", "lab view"],
                        )

        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["action"], "use")
        self.assertEqual(data["type"], "VIEW")
        self.assertEqual(data["view"], "Lab View")
        write_session_view.assert_called_once_with("Lab View")

    def test_dns_create_outputs_json_without_ref(self):
        class FakeClient:
            view = "corp"

            def request(self, method, object_type, payload=None, params=None):
                self.request_seen = (method, object_type, payload)
                return "record:a/app"

        runner = CliRunner()
        client = FakeClient()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    with patch.object(ib, "InfobloxClient", return_value=client):
                        result = runner.invoke(
                            ib.cli,
                            ["dns", "create", "a", "app", "192.0.2.10", "-o", "jq"],
                        )

        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["action"], "create")
        self.assertEqual(data["type"], "A")
        self.assertEqual(data["name"], "app.example.com")
        self.assertEqual(data["zone"], "example.com")
        self.assertEqual(data["view"], "corp")
        self.assertNotIn("ref", data)
        self.assertNotIn("_ref", data)
        self.assertEqual(client.request_seen[0], "POST")
        self.cache_refresh.assert_called_once_with()

    def test_dns_edit_outputs_csv_without_ref(self):
        class FakeClient:
            view = "corp"

            def request(self, method, object_type, payload=None, params=None):
                self.request_seen = (method, object_type, payload)
                return "record:a/app-updated"

        def fake_safe_query(client, object_type, params):
            if object_type == "record:a":
                return [
                    {
                        "_ref": "record:a/app",
                        "name": "app.example.com",
                        "zone": "example.com",
                        "ipv4addr": "192.0.2.10",
                    }
                ]
            return []

        runner = CliRunner()
        client = FakeClient()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    with patch.object(ib, "InfobloxClient", return_value=client):
                        with patch.object(ib, "safe_query", side_effect=fake_safe_query):
                            result = runner.invoke(
                                ib.cli,
                                ["-o", "csv", "dns", "edit", "app", "a", "192.0.2.20"],
                            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            result.output.splitlines(),
            [
                "status,action,type,name,zone,view,message",
                "success,edit,A,app.example.com,example.com,corp,updated A record",
            ],
        )
        self.assertEqual(client.request_seen[0], "PUT")
        self.assertEqual(client.request_seen[1], "record:a/app")
        self.cache_refresh.assert_called_once_with()

    def test_dns_delete_outputs_json_without_ref(self):
        class FakeClient:
            view = "corp"

            def request(self, method, object_type, payload=None, params=None):
                self.request_seen = (method, object_type)
                return None

        def fake_safe_query(client, object_type, params):
            if object_type == "record:a" and params.get("name") == "app.example.com":
                return [
                    {
                        "_ref": "record:a/app",
                        "name": "app.example.com",
                        "zone": "example.com",
                        "ipv4addr": "192.0.2.10",
                    }
                ]
            return []

        runner = CliRunner()
        client = FakeClient()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    with patch.object(ib, "InfobloxClient", return_value=client):
                        with patch.object(ib, "safe_query", side_effect=fake_safe_query):
                            result = runner.invoke(
                                ib.cli,
                                ["dns", "delete", "app", "-o", "jq"],
                            )

        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["action"], "delete")
        self.assertEqual(data["type"], "A")
        self.assertEqual(data["name"], "app.example.com")
        self.assertEqual(data["zone"], "example.com")
        self.assertEqual(data["view"], "corp")
        self.assertNotIn("ref", data)
        self.assertEqual(client.request_seen, ("DELETE", "record:a/app"))
        self.cache_refresh.assert_called_once_with()

    def test_dns_list_outputs_json_without_ref_or_context(self):
        class FakeClient:
            view = "corp"

        records = [
            (
                "a",
                {
                    "_ref": "record:a/app",
                    "name": "app.example.com",
                    "zone": "example.com",
                    "ipv4addr": "192.0.2.10",
                    "ttl": 300,
                    "comment": "Application VIP",
                },
            )
        ]
        runner = CliRunner()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                        with patch.object(ib, "dns_list_zone_info", return_value={"fqdn": "example.com"}):
                            with patch.object(ib, "dns_list_records_for_zone", return_value=records):
                                result = runner.invoke(ib.cli, ["-o", "jq", "dns", "list"])

        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)
        self.assertEqual(data[0]["type"], "A")
        self.assertEqual(data[0]["name"], "app.example.com")
        self.assertEqual(data[0]["value"], "192.0.2.10")
        self.assertNotIn("ref", data[0])
        self.assertNotIn("_ref", data[0])
        self.assertNotIn("Current DNS Context", result.output)

    def test_dns_list_uses_active_zone_and_paged_allrecords(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        zone_matches = [
            {
                "fqdn": "example.com",
                "view": "corp",
                "zone_format": "FORWARD",
                "primary_type": "Grid",
                "soa_serial_number": 10,
            }
        ]
        allrecords = [
            {
                "_ref": "allrecords/app",
                "type": "record:a",
                "name": "app.example.com",
                "zone": "example.com",
                "address": "192.0.2.10",
                "ttl": 300,
                "comment": "Application VIP",
            }
        ]
        table_records = []

        def fake_safe_query(client, object_type, params):
            self.assertEqual(object_type, ib.ZONE_OBJECT)
            self.assertEqual(params["fqdn"], "example.com")
            self.assertIn("soa_serial_number", params["_return_fields"])
            return zone_matches

        def fake_paged_query(client, object_type, params, warn_on_skip=True):
            self.assertEqual(object_type, ib.ALLRECORDS_OBJECT)
            self.assertEqual(params["view"], "corp")
            self.assertEqual(params["zone"], "example.com")
            return allrecords

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
                                with patch.object(ib, "safe_query", side_effect=fake_safe_query):
                                    with patch.object(ib, "paged_query", side_effect=fake_paged_query):
                                        with patch.object(ib, "record_table", side_effect=fake_record_table):
                                            with patch.object(ib, "dns_context_panel", return_value="context"):
                                                with patch.object(ib.console, "print") as print_mock:
                                                    ib.run_dns_list()

        self.assertEqual(table_records, [("a", allrecords[0])])
        printed = [call.args[0] for call in print_mock.call_args_list]
        self.assertEqual(printed, ["context", "records"])

    def test_dns_list_accepts_explicit_zone_argument(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        seen = {}

        def fake_safe_query(client, object_type, params):
            seen["fqdn"] = params["fqdn"]
            return [{"fqdn": "example.net", "view": "corp", "soa_serial_number": 1}]

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                    with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                        with patch.object(ib, "safe_query", side_effect=fake_safe_query):
                            with patch.object(ib, "paged_query", return_value=[]):
                                with patch.object(ib, "dns_context_panel", return_value="context"):
                                    with patch.object(ib, "print_warning") as print_warning:
                                        with patch.object(ib.console, "print"):
                                            ib.run_dns_list("example.net")

        self.assertEqual(seen["fqdn"], "example.net")
        print_warning.assert_called_once_with("No records found in zone example.net.")

    def test_dns_search_outputs_json_with_global_output_option(self):
        class FakeClient:
            view = "corp"

        records = [
            (
                "a",
                {
                    "_ref": "record:a/app",
                    "name": "app.example.com",
                    "zone": "example.com",
                    "ipv4addr": "192.0.2.10",
                    "ttl": 300,
                    "comment": "Application",
                },
            )
        ]
        runner = CliRunner()

        with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
            with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                with patch.object(ib, "search_root_zone", return_value="example.com"):
                    with patch.object(ib, "collect_dns_search_results", return_value=records):
                        result = runner.invoke(ib.cli, ["-o", "jq", "dns", "search", "app"])

        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)
        self.assertEqual(data[0]["type"], "A")
        self.assertEqual(data[0]["name"], "app.example.com")
        self.assertEqual(data[0]["value"], "192.0.2.10")
        self.assertNotIn("ref", data[0])
        self.assertNotIn("Current DNS Context", result.output)

    def test_dns_search_accepts_global_output_option_after_command_arguments(self):
        class FakeClient:
            view = "corp"

        records = [
            (
                "a",
                {
                    "_ref": "record:a/app",
                    "name": "app.example.com",
                    "zone": "example.com",
                    "ipv4addr": "192.0.2.10",
                },
            )
        ]
        runner = CliRunner()

        with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
            with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                with patch.object(ib, "search_root_zone", return_value="example.com"):
                    with patch.object(ib, "collect_dns_search_results", return_value=records):
                        result = runner.invoke(ib.cli, ["dns", "search", "app", "-o", "jq"])

        self.assertEqual(result.exit_code, 0, result.output)
        data = json.loads(result.output)
        self.assertEqual(data[0]["name"], "app.example.com")
        self.assertNotIn("ref", data[0])
        self.assertNotIn("No such option", result.output)

    def test_dns_search_accepts_global_output_option_on_dns_group(self):
        runner = CliRunner()

        with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
            with patch.object(ib, "InfobloxClient"):
                with patch.object(ib, "search_root_zone", return_value="example.com"):
                    with patch.object(ib, "collect_dns_search_results", return_value=[]):
                        result = runner.invoke(ib.cli, ["dns", "-o", "csv", "search", "app"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output.splitlines(), ["type,name,value,zone,ttl,comment"])

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

    def test_dns_search_includes_srv_records_by_target(self):
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
                        "_ref": "allrecords/srv",
                        "type": "record:srv",
                        "name": "_sip._tcp",
                        "zone": "example.com",
                        "record": {
                            "name": "_sip._tcp.example.com",
                            "priority": 10,
                            "weight": 20,
                            "port": 5060,
                            "target": "sip.example.com",
                        },
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
                                                ib.run_dns_search("sip.example.com")

        self.assertEqual(table_records[0][0], "srv")
        self.assertEqual(ib.record_value("srv", table_records[0][1]), "10 20 5060 sip.example.com")
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

    def test_dns_search_formats_srv_records(self):
        direct = {
            "_ref": "record:srv/sip",
            "name": "_sip._tcp.example.com",
            "zone": "example.com",
            "priority": 10,
            "weight": 20,
            "port": 5060,
            "target": "sip.example.com",
        }
        wrapped = {
            "_ref": "allrecords/srv",
            "type": "record:srv",
            "name": "_sip._tcp",
            "zone": "example.com",
            "record": {
                "name": "_sip._tcp.example.com",
                "priority": 10,
                "weight": 20,
                "port": 5060,
                "target": "sip.example.com",
            },
        }

        self.assertEqual(ib.record_value("srv", direct), "10 20 5060 sip.example.com")
        self.assertEqual(ib.record_name(wrapped), "_sip._tcp.example.com")
        self.assertEqual(ib.record_value("srv", wrapped), "10 20 5060 sip.example.com")
        self.assertEqual(ib.record_value("shared-srv", wrapped), "10 20 5060 sip.example.com")
        self.assertTrue(ib.allrecord_matches_keyword(wrapped, "sip.example.com", False))

    def test_dns_search_infers_known_unsupported_allrecords_types_from_ref(self):
        def allrecords_ref(decoded: str) -> str:
            token = (
                ib.base64.urlsafe_b64encode(decoded.encode("utf-8"))
                .decode("ascii")
                .rstrip("=")
            )
            return f"allrecords/{token}:suffix"

        ns_item = {
            "_ref": allrecords_ref("dns.zone_search_index$..fake_bind_ns$.delegation"),
            "type": "UNSUPPORTED",
            "name": "child.example.com",
            "zone": "example.com",
            "record": None,
        }
        soa_item = {
            "_ref": allrecords_ref("dns.zone_search_index$dns.bind_soa$._default.example"),
            "type": "UNSUPPORTED",
            "name": "example.com",
            "zone": "example.com",
            "record": None,
        }
        unknown_item = {
            "_ref": allrecords_ref("dns.zone_search_index$unknown$._default.example"),
            "type": "UNSUPPORTED",
            "name": "other.example.com",
            "zone": "example.com",
            "record": None,
        }

        self.assertEqual(ib.allrecord_type(ns_item), "ns")
        self.assertEqual(ib.allrecord_type(soa_item), "soa")
        self.assertEqual(ib.allrecord_type(unknown_item), "unsupported")
        self.assertEqual(ib.allrecord_search_entry(ns_item)["type"], "ns")
        self.assertEqual(ib.allrecord_search_entry(soa_item)["type"], "soa")
        self.assertEqual(
            ib.allrecord_search_entry_type({"type": "unsupported", "record": ns_item}),
            "ns",
        )

    def test_dns_search_categorizes_unsupported_bind_ns_as_ns_without_ref_value(self):
        def allrecords_ref(decoded: str) -> str:
            token = (
                ib.base64.urlsafe_b64encode(decoded.encode("utf-8"))
                .decode("ascii")
                .rstrip("=")
            )
            return f"allrecords/{token}:suffix"

        ns_item = {
            "_ref": allrecords_ref("dns.zone_search_index$dns.bind_ns$._default.example"),
            "type": "UNSUPPORTED",
            "name": "child.example.com",
            "zone": "example.com",
            "record": "record:ns/ZG5zLmJpbmRfbnMkLmNoaWxkLmV4YW1wbGUuY29t:child/default",
        }

        self.assertEqual(ib.allrecord_type(ns_item), "ns")
        self.assertEqual(ib.allrecord_search_entry(ns_item)["type"], "ns")
        self.assertEqual(ib.record_value("ns", ns_item), "")

        test_console = ib.Console(width=160, force_terminal=False)
        with test_console.capture() as capture:
            test_console.print(ib.record_table([("ns", ns_item)]))

        output = capture.get()
        self.assertIn("NS", output)
        self.assertNotIn("UNSUPPORTED", output)
        self.assertNotIn("record:ns/", output)
        self.assertNotIn(ns_item["_ref"], output)

    def test_dns_search_formats_nested_ns_nameserver_value(self):
        ns_item = {
            "_ref": "allrecords/ns",
            "type": "UNSUPPORTED",
            "name": "example.com",
            "zone": "example.com",
            "record": {
                "_ref": "record:ns/example",
                "name": "example.com",
                "nameserver": "ns1.example.net",
            },
        }

        self.assertEqual(ib.allrecord_type(ns_item), "ns")
        self.assertEqual(ib.record_name(ns_item), "example.com")
        self.assertEqual(ib.record_value("ns", ns_item), "ns1.example.net")

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
                    with patch.object(ib, "start_zone_serial_cache_revalidation") as start_refresh:
                        with patch.object(ib.time, "time", return_value=100):
                            self.assertEqual(ib.query_zone_serials(FakeClient()), zones)
                        with patch.object(ib.time, "time", return_value=129):
                            self.assertEqual(ib.query_zone_serials(FakeClient()), zones)

        paged_query.assert_called_once()
        start_refresh.assert_not_called()

    def test_zone_serial_query_uses_swr_after_30_second_cache_expires(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        first = [{"fqdn": "example.com", "primary_type": "Grid", "soa_serial_number": 1}]
        second = [{"fqdn": "example.com", "primary_type": "Grid", "soa_serial_number": 2}]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                with patch.object(ib, "paged_query", side_effect=[first, second]) as paged_query:
                    with patch.object(ib, "start_zone_serial_cache_revalidation") as start_refresh:
                        with patch.object(ib.time, "time", return_value=100):
                            self.assertEqual(ib.query_zone_serials(FakeClient()), first)
                        with patch.object(ib.time, "time", return_value=131):
                            self.assertEqual(ib.query_zone_serials(FakeClient()), first)

        paged_query.assert_called_once()
        start_refresh.assert_called_once_with()

    def test_zone_serial_query_refreshes_after_swr_window_expires(self):
        class FakeClient:
            server = "https://infoblox.example.com"
            wapi_version = "v2.12.3"
            view = "corp"

        first = [{"fqdn": "example.com", "primary_type": "Grid", "soa_serial_number": 1}]
        second = [{"fqdn": "example.com", "primary_type": "Grid", "soa_serial_number": 2}]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ib, "ALLRECORDS_CACHE_DIR", Path(tmpdir)):
                with patch.object(ib, "paged_query", side_effect=[first, second]) as paged_query:
                    with patch.object(ib, "start_zone_serial_cache_revalidation") as start_refresh:
                        with patch.object(ib.time, "time", return_value=100):
                            self.assertEqual(ib.query_zone_serials(FakeClient()), first)
                        with patch.object(ib.time, "time", return_value=221):
                            self.assertEqual(ib.query_zone_serials(FakeClient()), second)

        self.assertEqual(paged_query.call_count, 2)
        start_refresh.assert_not_called()

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

    def test_dns_list_uses_zone_name_completion(self):
        dns_list = ib.dns.commands["list"]

        self.assertIs(dns_list.params[0]._custom_shell_complete, ib.complete_zone_names)

    def test_dns_view_use_uses_view_name_completion(self):
        view_use = ib.view.commands["use"]

        self.assertIs(view_use.params[0]._custom_shell_complete, ib.complete_dns_view_names)

    def test_configure_commands_use_profile_name_completion(self):
        configure_new = ib.configure.commands["new"]
        configure_edit = ib.configure.commands["edit"]
        configure_delete = ib.configure.commands["delete"]
        configure_use = ib.configure.commands["use"]

        self.assertIs(configure_new.params[0]._custom_shell_complete, ib.complete_new_profile_names)
        self.assertIs(configure_edit.params[0]._custom_shell_complete, ib.complete_existing_profile_names)
        self.assertIs(configure_delete.params[0]._custom_shell_complete, ib.complete_deletable_profile_names)
        self.assertIs(configure_use.params[0]._custom_shell_complete, ib.complete_existing_profile_names)

    def test_dns_edit_uses_record_name_completion(self):
        dns_edit = ib.dns.commands["edit"]

        self.assertIs(dns_edit.params[0]._custom_shell_complete, ib.complete_dns_edit_records)
        self.assertIs(dns_edit.params[1]._custom_shell_complete, ib.complete_dns_edit_record_types)

    def test_dns_edit_type_completion_only_suggests_existing_record_type(self):
        class FakeClient:
            view = "corp"

        class FakeContext:
            params = {"record_name": "app"}

        def fake_safe_query(client, object_type, params):
            if object_type == "record:host":
                return [
                    {
                        "_ref": "record:host/app",
                        "name": "app.example.com",
                        "zone": "example.com",
                        "ipv4addrs": [{"ipv4addr": "192.0.2.10"}],
                    }
                ]
            return []

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ib.DEFAULT_ZONE_ENV, None)
            with patch.object(ib, "load_config", return_value={"default_zone": "example.com"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                        with patch.object(ib, "safe_query", side_effect=fake_safe_query):
                            items = ib.complete_dns_edit_record_types(FakeContext(), None, "")

        self.assertEqual([item.value for item in items], ["host"])


if __name__ == "__main__":
    unittest.main()
