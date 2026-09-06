from __future__ import annotations

import http.client
import json
import threading
import unittest

from mediacenter.server import Handler, MediaCenterHTTPServer


class Center:
    def __init__(self):
        self.calls = []

    def stop_deployment_worker(self):
        return True

    def plan_user_deployment(self, body):
        self.calls.append(("plan", body))
        return {"operation": body, "capacity": {"schedulable": True}}

    def create_user_deployment(self, body, *, idempotency_key, identity_scope):
        self.calls.append(("create", body, idempotency_key, identity_scope))
        return {"id": "dop_fixture", "state": "accepted"}

    def uninstall_user_deployment(self, identifier, body, *, idempotency_key, identity_scope):
        self.calls.append(('uninstall', identifier, body, idempotency_key, identity_scope))
        return {'id': 'dop_remove', 'state': 'accepted', 'milestone': 'removing_containers'}

    def get_deployment_operation(self, identifier):
        self.calls.append(("get", identifier))
        return {"id": identifier, "state": "ready"}

    def list_deployment_operations(self, limit):
        self.calls.append(("list", limit))
        return [{"id": "dop_fixture", "state": "ready"}]

    def cancel_deployment_operation(self, identifier):
        self.calls.append(("cancel", identifier))
        return {"id": identifier, "state": "canceled"}

    def list_runtime_profiles(self):
        self.calls.append(("profiles",))
        return [{"profile_id": "sdxl-single-file", "revision": 1}]

    def list_asset_compatibility(self, **query):
        self.calls.append(('compatibility-list', query))
        return [{'verdict':'exact'}]

    def assess_asset_compatibility(self, body):
        self.calls.append(('compatibility-assess', body))
        return {'verdict':'incompatible'}


class PH8APITests(unittest.TestCase):
    def setUp(self):
        self.server = MediaCenterHTTPServer(("127.0.0.1", 0), Handler)
        self.server.api_key = "fixture-key"
        self.server.center = Center()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, *, key="fixture-key", headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=2)
        raw = None if body is None else json.dumps(body).encode("utf-8")
        values = dict(headers or {})
        if key is not None:
            values["X-API-Key"] = key
        if raw is not None:
            values["Content-Type"] = "application/json"
            values["Content-Length"] = str(len(raw))
        connection.request(method, path, body=raw, headers=values)
        response = connection.getresponse()
        value = json.loads(response.read())
        connection.close()
        return response.status, value

    def test_plan_create_read_and_cancel_routes_keep_command_semantics_distinct(self):
        status, plan = self.request("POST", "/api/v1/deployment-plans", {"fixture": 1})
        self.assertEqual((status, plan["capacity"]["schedulable"]), (200, True))
        status, created = self.request(
            "POST", "/api/v1/deployment-operations", {"fixture": 1},
            headers={"Idempotency-Key": "install-fixture"})
        self.assertEqual((status, created["state"]), (202, "accepted"))
        status, current = self.request("GET", "/api/v1/deployment-operations/dop_fixture")
        self.assertEqual((status, current["id"]), (200, "dop_fixture"))
        status, canceled = self.request(
            "POST", "/api/v1/deployment-operations/dop_fixture/cancel", {})
        self.assertEqual((status, canceled["state"]), (200, "canceled"))
        self.assertEqual(self.server.center.calls, [
            ("plan", {"fixture": 1}),
            ("create", {"fixture": 1}, "install-fixture", "server-admin"),
            ("get", "dop_fixture"),
            ("cancel", "dop_fixture"),
        ])

    def test_deployment_commands_require_authentication(self):
        status, value = self.request(
            "POST", "/api/v1/deployment-plans", {"fixture": 1}, key=None)
        self.assertEqual(status, 401)
        self.assertEqual(value["error"]["code"], "unauthorized")
        self.assertEqual(self.server.center.calls, [])

    def test_user_uninstall_has_instance_scoped_authenticated_202_route(self):
        route = '/api/v1/deployments/wai-user/uninstall'
        self.assertEqual(self.request('POST', route, {}, key=None)[0], 401)
        self.assertEqual(self.server.center.calls, [])
        status, result = self.request('POST', route, {'retry_of': None}, headers={'Idempotency-Key':'remove-one'})
        self.assertEqual((status, result['state']), (202, 'accepted'))
        self.assertEqual(self.server.center.calls, [('uninstall', 'wai-user', {'retry_of':None}, 'remove-one', 'server-admin')])
        self.assertEqual(self.request('POST', '/api/v1/deployments/a/b/uninstall', {})[0], 404)

    def test_compatibility_commands_and_queries_require_auth_and_preserve_filters(self):
        self.assertEqual(self.request('GET','/api/v1/asset-compatibility',key=None)[0],401)
        self.assertEqual(self.request('POST','/api/v1/asset-compatibility',{},key=None)[0],401)
        status, result=self.request('GET','/api/v1/asset-compatibility?subject_asset_id=lora&base_asset_id=wai')
        self.assertEqual((status,result['items'][0]['verdict']),(200,'exact'))
        self.assertEqual(self.server.center.calls[-1],('compatibility-list',dict(subject_asset_id='lora',base_asset_id='wai')))
        status, result=self.request('POST','/api/v1/asset-compatibility',dict(subject_asset_id='lora',base_asset_id='pony'))
        self.assertEqual((status,result['verdict']),(200,'incompatible'))

    def test_runtime_profiles_are_discoverable_through_an_authenticated_read_contract(self):
        status, value = self.request("GET", "/api/v1/runtime-profiles")
        self.assertEqual(status, 200)
        self.assertEqual(value["items"], [
            {"profile_id": "sdxl-single-file", "revision": 1}])
        self.assertEqual(self.server.center.calls, [("profiles",)])

    def test_deployment_operations_can_be_recovered_after_desktop_restart(self):
        status, value = self.request("GET", "/api/v1/deployment-operations?limit=25")
        self.assertEqual(status, 200)
        self.assertEqual(value["items"][0]["id"], "dop_fixture")
        self.assertEqual(self.server.center.calls, [("list", 25)])


if __name__ == "__main__":
    unittest.main()
