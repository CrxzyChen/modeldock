from __future__ import annotations
import unittest
from pathlib import Path
from mediacenter.redis_transport import render_acl
from mediacenter.transport import Identity

ROOT=Path(__file__).parents[1]

class RedisAclMatrixTests(unittest.TestCase):
    def test_each_epoch_gets_disjoint_worker_and_server_users_without_global_keys(self):
        template=(ROOT/"deploy/redis-acl.template").read_text(encoding="utf-8")
        first=render_acl(template,Identity("server","instance-a","epoch-a"),server_user="server-a",worker_user="worker-a",
                         server_secret_sha256="1"*64,worker_secret_sha256="2"*64)
        second=render_acl(template,Identity("server","instance-b","epoch-b"),server_user="server-b",worker_user="worker-b",
                          server_secret_sha256="3"*64,worker_secret_sha256="4"*64)
        self.assertNotEqual(first,second)
        for body in (first,second):
            self.assertNotIn("~*",body);self.assertNotIn("+@all",body);self.assertNotIn("nopass",body)

    def test_acl_template_has_no_global_union_or_administrative_commands(self):
        source=(ROOT/"deploy/redis-acl.template").read_text(encoding="utf-8")
        for forbidden in ("~*","+@all","+config|set","+acl","+shutdown","+flushall"):
            self.assertNotIn(forbidden,source.lower())
