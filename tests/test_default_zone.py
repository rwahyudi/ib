import importlib.machinery
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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

    def test_usage_help_includes_current_view_and_active_zone(self):
        with patch.dict(os.environ, {ib.DEFAULT_ZONE_ENV: "example.com"}, clear=False):
            with patch.object(ib, "default_config_values", return_value={"dns_view": "corp"}):
                with patch.object(ib, "read_session_zone", return_value=None):
                    with ib.cli.make_context("ib", [], resilient_parsing=True) as ctx:
                        help_text = ib.cli.get_help(ctx)

        self.assertIn("DNS Context", help_text)
        self.assertIn("corp", help_text)
        self.assertIn("example.com", help_text)
        self.assertIn("IB_ZONE", help_text)

    def test_dns_context_panel_is_one_line_with_background(self):
        with patch.object(ib, "current_view_status", return_value=("corp", "configured")):
            with patch.object(ib, "active_zone_status", return_value=("example.com", "shell session")):
                panel = ib.dns_context_panel("Current DNS Context")

        self.assertIsInstance(panel.renderable, ib.Text)
        self.assertEqual(
            panel.renderable.plain,
            "View: corp (configured)  |  Active zone: example.com (shell session)",
        )
        self.assertNotIn("\n", panel.renderable.plain)
        self.assertIn("on #0f172a", str(panel.style))

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

        zone_info = {"fqdn": "example.com", "view": "corp", "zone_format": "FORWARD"}
        with patch.object(ib, "load_config", return_value={"dns_view": "corp"}):
            with patch.object(ib, "InfobloxClient", return_value=FakeClient()):
                with patch.object(ib, "safe_query", return_value=[zone_info]) as safe_query:
                    with patch.object(ib, "zone_detail_table", return_value="details"):
                        with patch.object(ib.console, "print") as print_mock:
                            ib.run_dns_zone_view("example.com.")

        params = safe_query.call_args.args[2]
        self.assertEqual(params["fqdn"], "example.com")
        self.assertEqual(params["view"], "corp")
        self.assertEqual(params["_return_fields"], ib.ZONE_DETAIL_RETURN_FIELDS)
        self.assertIn("soa_serial_number", params["_return_fields"])
        self.assertIn("member_soa_mnames", params["_return_fields"])
        self.assertIn("soa_negative_ttl", params["_return_fields"])
        print_mock.assert_called_once_with("details")

    def test_zone_serial_query_includes_primary_type(self):
        class FakeClient:
            view = "corp"

        with patch.object(ib, "paged_query", return_value=[]) as paged_query:
            ib.query_zone_serials(FakeClient())

        params = paged_query.call_args.args[2]
        self.assertIn("primary_type", params["_return_fields"])

    def test_zone_detail_table_includes_soa_settings(self):
        table = ib.zone_detail_table(
            {
                "fqdn": "example.com",
                "view": "corp",
                "zone_format": "FORWARD",
                "member_soa_mnames": [{"mname": "ns1.example.com"}],
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

    def test_dns_zone_view_uses_zone_name_completion(self):
        zone_view = ib.zone.commands["view"]

        self.assertIs(zone_view.params[0]._custom_shell_complete, ib.complete_zone_names)


if __name__ == "__main__":
    unittest.main()
