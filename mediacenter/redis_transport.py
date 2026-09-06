"""Redis 7.2 / redis-py 5.3 transport. SQLite/journal remain the authority.

There is deliberately no reliable-stream deletion API: application receipts
alone cannot prove that every consumer group is done, nor exclude a new group
racing a trim. Retain reliable records until a separately proven retention
protocol exists. Telemetry alone is coalesced and expires.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from string import Template

import redis
from redis import exceptions as errors
from redis.backoff import NoBackoff
from redis.retry import Retry

from .protocol import MAX_MESSAGE_BYTES, ProtocolError, parse_envelope, validate_envelope
from .transport import Delivery, Identity, TransportError, lane_for, token

LANES = ("commands", "control", "events")
GROUPS = {"commands": "worker-execution-v1", "control": "worker-control-v1", "events": "server-events-v1"}
MAX_INGRESS_BYTES = 1024 * 1024  # Redis 7.2 proto-max-bulk-len minimum

# Workers cannot use XADD: MAXLEN/MINID would bypass a ban on XTRIM/XDEL.
# Only the trusted Server promotes an append-only RPUSH ingress to Streams.
# Never mistake Lua atomic execution for rollback after a runtime error.
_PEEK = """
local t=redis.call('TYPE',KEYS[1]).ok
if t~='none' and t~='list' then return redis.error_reply('WRONGTYPE ingress') end
local v=redis.call('LINDEX',KEYS[1],0)
if not v then return {} end
if string.len(v)>tonumber(ARGV[2]) then return {'unsafe',tostring(string.len(v)),''} end
if string.len(v)>tonumber(ARGV[3]) then return {'budget',tostring(string.len(v)),''} end
if string.len(v)>tonumber(ARGV[1]) then return {'oversize',tostring(string.len(v)),redis.sha1hex(v)} end
return {'raw',tostring(string.len(v)),v}
"""
_MOVE = """
local it=redis.call('TYPE',KEYS[1]).ok
if it~='none' and it~='list' then return redis.error_reply('WRONGTYPE ingress') end
local v=redis.call('LINDEX',KEYS[1],0)
if not v then return 0 end
if string.len(v)>tonumber(ARGV[5]) then return redis.error_reply('ERR ingress limit') end
local fingerprint=nil
if ARGV[1]=='raw' then
  if v~=ARGV[2] then return 0 end
else
  fingerprint=redis.sha1hex(v)
  if string.len(v)~=tonumber(ARGV[3]) or fingerprint~=ARGV[2] then return 0 end
end
if ARGV[4]=='' then
  local et=redis.call('TYPE',KEYS[2]).ok
  if et~='none' and et~='stream' then return redis.error_reply('WRONGTYPE events') end
  redis.call('XADD',KEYS[2],'*','envelope',v)
else
  local dt=redis.call('TYPE',KEYS[3]).ok
  if dt~='none' and dt~='hash' then return redis.error_reply('WRONGTYPE diagnostics') end
  if not fingerprint then fingerprint=redis.sha1hex(v) end
  redis.call('HSET',KEYS[3],'ingress:'..fingerprint,
    cjson.encode({lane='ingress',code=ARGV[4],bytes=string.len(v),sha1=fingerprint}))
end
-- A failed append/diagnostic above MUST NOT consume the ingress element.
-- A failure here may duplicate the append on retry, which inbox deduplicates.
redis.call('LPOP',KEYS[1])
return 1
"""


def render_acl(template: str, identity: Identity, *, server_user: str, worker_user: str,
               server_secret_sha256: str, worker_secret_sha256: str) -> str:
    """Render a trusted template; Redis ACL files do not accept comment lines."""
    token(server_user)
    token(worker_user)
    if server_user == worker_user or "default" in {server_user, worker_user}:
        raise TransportError("invalid_acl_identity")
    for value in (server_secret_sha256, worker_secret_sha256):
        if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise TransportError("invalid_secret_digest")
    values = dict(SERVER=identity.server_id, INSTANCE=identity.instance_id, EPOCH=identity.worker_epoch,
                  SERVER_USER=server_user, WORKER_USER=worker_user,
                  SERVER_SECRET_SHA256=server_secret_sha256, WORKER_SECRET_SHA256=worker_secret_sha256)
    body = "\n".join(line for line in template.splitlines() if line.strip() and not line.lstrip().startswith("#"))
    try:
        return Template(body).substitute(values) + "\n"
    except (ValueError, KeyError):
        raise TransportError("invalid_acl_template") from None


def classify(error, *, write=False):
    if isinstance(error, errors.AuthenticationError):
        return TransportError("redis_authentication_failed")
    if isinstance(error, (errors.NoPermissionError, errors.AuthorizationError)):
        return TransportError("redis_permission_denied")
    if isinstance(error, errors.OutOfMemoryError):
        return TransportError("redis_backpressure", retryable=True)
    if isinstance(error, errors.TimeoutError):
        return TransportError("redis_timeout", retryable=True, outcome_unknown=write)
    if isinstance(error, errors.BusyLoadingError):
        return TransportError("redis_loading", retryable=True)
    if isinstance(error, errors.ConnectionError):
        return TransportError("redis_disconnected", retryable=True, outcome_unknown=write)
    if isinstance(error, errors.ResponseError):
        return TransportError("redis_command_rejected")
    return TransportError("redis_transport_failed")


def _secret(path):
    try:
        path = Path(path)
        if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or os.name == "posix" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
                raise ValueError
            raw = source.read(257)
        if not re.fullmatch(rb"[A-Za-z0-9_-]{32,256}", raw):
            raise ValueError
        return raw.decode("ascii")
    except (OSError, ValueError, TypeError):
        raise TransportError("invalid_secret_file") from None


@dataclass(frozen=True)
class RedisEndpoint:
    username: str
    secret_file: Path
    unix_socket: str | None = None
    host: str = "127.0.0.1"
    port: int = 6379
    tls: bool = True
    ca_file: str | None = None
    socket_timeout: float = 2.0
    connect_timeout: float = 1.0

    def options(self):
        token(self.username)
        if (type(self.port) is not int or not 1 <= self.port <= 65535 or type(self.tls) is not bool
                or type(self.socket_timeout) not in (int, float) or type(self.connect_timeout) not in (int, float)
                or not 0.1 <= self.socket_timeout <= 30 or not 0.1 <= self.connect_timeout <= 10):
            raise TransportError("invalid_connection_limits")
        if not self.unix_socket and not self.tls and self.host not in {"127.0.0.1", "::1", "localhost"}:
            raise TransportError("plaintext_remote_forbidden")
        options = dict(username=self.username, password=_secret(self.secret_file), socket_timeout=self.socket_timeout,
                       socket_connect_timeout=self.connect_timeout, max_connections=2, decode_responses=False,
                       retry=Retry(NoBackoff(), 0), retry_on_timeout=False, lib_name=None, lib_version=None)
        if self.unix_socket:
            if not Path(self.unix_socket).is_absolute():
                raise TransportError("invalid_socket_path")
            options["unix_socket_path"] = self.unix_socket
        else:
            options.update(host=self.host, port=self.port, ssl=self.tls)
            if self.tls:
                options.update(ssl_cert_reqs="required", ssl_check_hostname=True, ssl_ca_certs=self.ca_file)
        return options


class RedisTransport:
    def __init__(self, identity: Identity, endpoint: RedisEndpoint, *, role: str, consumer: str,
                 capabilities=None, claim_idle_ms=30000, client_factory=redis.Redis):
        if role not in {"server", "worker"}:
            raise TransportError("invalid_role")
        self.identity, self.role, self.consumer = identity, role, token(consumer)
        if type(claim_idle_ms) is not int or not 0 <= claim_idle_ms <= 86400000:
            raise TransportError("invalid_claim_limits")
        self.claim_idle_ms = claim_idle_ms
        self.capabilities, self.endpoint = capabilities, endpoint
        # Distinct pools: a blocking execute read cannot occupy control/events.
        options = endpoint.options()
        self.clients = {lane: client_factory(**(dict(options, socket_timeout=0.2, socket_connect_timeout=0.2)
                                              if lane == "telemetry" else options))
                        for lane in (*LANES, "telemetry", "diagnostics")}
        self.backpressured = False
        # Telemetry is lossy and never a Redis key. Two slots per epoch bound
        # both publishing and observing, independently of reliable lanes.
        self._telemetry_lock = threading.Lock()
        self._telemetry_stop = threading.Event()
        self._telemetry_wake = threading.Event()
        self._telemetry_thread = None
        self._telemetry_latest = {}
        self._telemetry_seen = {}
        self._telemetry_pending = {}
        self._telemetry_ready = threading.Event()
        self._telemetry_generation = 0

    def close(self, timeout=3):
        self._telemetry_stop.set()
        self._telemetry_wake.set()
        if self._telemetry_thread is not None:
            self._telemetry_thread.join(timeout=timeout)
            if self._telemetry_thread.is_alive():
                return False
        with self._telemetry_lock:
            self._telemetry_latest.clear()
            self._telemetry_pending.clear()
        for client in self.clients.values():
            client.close()
        return True

    def key(self, lane):
        if lane not in (*LANES, "telemetry:heartbeat", "telemetry:progress", "diagnostics", "ingress"):
            raise TransportError("invalid_lane")
        return self.identity.prefix + ":" + lane

    def _call(self, function, *args, write=False, **kwargs):
        try:
            return function(*args, **kwargs)
        except errors.RedisError as error:
            classified = classify(error, write=write)
            if classified.code == "redis_backpressure":
                self.backpressured = True
            raise classified from None

    def provision(self):
        if self.role != "server":
            raise TransportError("role_forbidden")
        self.verify_runtime()
        self._start_telemetry()
        for lane in LANES:
            try:
                self.clients[lane].xgroup_create(self.key(lane), GROUPS[lane], id="0-0", mkstream=True)
            except errors.ResponseError as error:
                if str(error).split(" ", 1)[0] != "BUSYGROUP":
                    raise classify(error, write=True) from None
            except errors.RedisError as error:
                raise classify(error, write=True) from None

    def verify_runtime(self):
        """Trusted Server checks live limits; a template is not attestation.

        This read-only CONFIG subcommand is never granted to the Worker.
        Return only pass/fail, not configuration paths or credentials.
        """
        if self.role != "server":
            raise TransportError("role_forbidden")
        names = ("proto-max-bulk-len", "client-query-buffer-limit", "maxmemory", "maxmemory-policy", "appendonly", "appendfsync", "client-output-buffer-limit")
        values = self._call(self.clients["events"].config_get, *names)
        values = {(key.decode() if isinstance(key, bytes) else key):
                  (value.decode() if isinstance(value, bytes) else value) for key, value in values.items()}
        try:
            output = values["client-output-buffer-limit"].split()
            offset = output.index("pubsub")
            hard, soft, seconds = map(int, output[offset + 1:offset + 4])
            safe = (0 < int(values["proto-max-bulk-len"]) <= MAX_INGRESS_BYTES
                    and 0 < int(values["client-query-buffer-limit"]) <= 2 * MAX_INGRESS_BYTES
                    and int(values["maxmemory"]) > 0 and values["maxmemory-policy"] == "noeviction"
                    and values["appendonly"] == "yes" and values["appendfsync"] in {"always", "everysec"}
                    and 0 < hard <= 1048576 and 0 < soft <= 262144 and 0 < seconds <= 1)
        except (KeyError, ValueError, TypeError):
            safe = False
        if not safe:
            raise TransportError("unsafe_redis_configuration")

    def _validate(self, envelope, lane=None):
        message = validate_envelope(envelope, capabilities=self.capabilities)
        if not self.identity.matches(message):
            raise TransportError("wrong_transport_identity")
        if lane is not None and lane_for(message["type"]) != lane:
            raise TransportError("wrong_message_lane")
        return message

    def publish(self, envelope):
        message = self._validate(envelope)
        lane = lane_for(message["type"])
        if (self.role == "server" and lane not in {"commands", "control"}
                or self.role == "worker" and lane not in {"events", "telemetry"}):
            raise TransportError("role_forbidden")
        if lane == "telemetry":
            return self._telemetry(message)
        if lane == "commands":
            self.verify_runtime()  # refuse new execution delivery on unsafe configuration
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        if lane == "events":
            result = self._call(self.clients[lane].rpush, self.key("ingress"), raw, write=True)
            return "ingress:" + str(result)  # receipt/journal identity is still message_id
        result = self._call(self.clients[lane].xadd, self.key(lane), {"envelope": raw}, write=True)
        # A successful bounded write proves current admission, not future space.
        self.backpressured = False
        return result.decode("ascii") if isinstance(result, bytes) else result

    def _consume_lane(self, lane):
        if lane not in LANES:
            raise TransportError("invalid_lane")
        if (self.role == "server" and lane != "events" or self.role == "worker" and lane == "events"):
            raise TransportError("role_forbidden")

    def _decode(self, lane, rows):
        deliveries = []
        for entry_id, fields in rows:
            entry_id = entry_id.decode("ascii") if isinstance(entry_id, bytes) else entry_id
            raw = fields.get(b"envelope", fields.get("envelope", b""))
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            fingerprint = hashlib.sha256(raw).hexdigest()
            try:
                if set(fields) not in ({b"envelope"}, {"envelope"}):
                    raise TransportError("invalid_transport_fields")
                message = parse_envelope(raw, capabilities=self.capabilities)
                self._validate(message, lane)
                deliveries.append(Delivery(lane, entry_id, message, fingerprint=fingerprint))
            except (ProtocolError, TransportError) as error:
                deliveries.append(Delivery(lane, entry_id, error_code=error.code, fingerprint=fingerprint))
        return deliveries

    def read(self, lane, *, count=20, block_ms=100):
        self._consume_lane(lane)
        if (type(count) is not int or not 1 <= count <= 1000 or type(block_ms) is not int
                or not 0 <= block_ms <= min(1000, int(self.endpoint.socket_timeout * 500))):
            raise TransportError("invalid_read_limits")
        client, key = self.clients[lane], self.key(lane)
        # Redis BLOCK 0 is infinite: omit BLOCK for nonblocking calls.
        prior = self._call(client.xreadgroup, GROUPS[lane], self.consumer, {key: "0-0"}, count=count)
        if prior and prior[0][1]:
            return self._decode(lane, prior[0][1])
        recovered = self.claim(lane, min_idle_ms=self.claim_idle_ms, count=count)
        if recovered:
            return recovered
        if lane == "events":
            self.promote_events(count=count)
        rows = self._call(client.xreadgroup, GROUPS[lane], self.consumer, {key: ">"}, count=count,
                          **({"block": block_ms} if block_ms else {}))
        return self._decode(lane, rows[0][1]) if rows else []

    def promote_events(self, *, count=20, max_bytes=MAX_INGRESS_BYTES):
        """Bounded trusted append promotion, no Worker authority or new attempt."""
        if self.role != "server":
            raise TransportError("role_forbidden")
        if (type(count) is not int or not 1 <= count <= 1000 or type(max_bytes) is not int
                or not MAX_INGRESS_BYTES <= max_bytes <= MAX_INGRESS_BYTES * 4):
            raise TransportError("invalid_promotion_limits")
        self.verify_runtime()
        client, moved, used = self.clients["events"], 0, 0
        for _ in range(count):
            if used >= max_bytes:
                break
            head = self._call(client.eval, _PEEK, 1, self.key("ingress"), MAX_MESSAGE_BYTES, MAX_INGRESS_BYTES, max_bytes - used)
            if not head:
                break
            mode, size, value = head[0].decode("ascii"), int(head[1]), head[2]
            if mode == "unsafe":
                raise TransportError("redis_ingress_limit_violation")
            if mode == "budget" or used + size > max_bytes:
                break
            code = "message_too_large" if mode == "oversize" else ""
            if mode == "raw":
                try:
                    message = parse_envelope(value, capabilities=self.capabilities)
                    self._validate(message, "events")
                except (ProtocolError, TransportError) as error:
                    code = error.code
            moved += self._call(client.eval, _MOVE, 3, self.key("ingress"), self.key("events"),
                                self.key("diagnostics"), mode, value, size, code, MAX_INGRESS_BYTES, write=True)
            used += size
        return {"moved": moved, "bytes_examined": used}

    def claim(self, lane, *, min_idle_ms, count=20):
        """Transfer PEL delivery ownership only; never authorize execution."""
        self._consume_lane(lane)
        if type(min_idle_ms) is not int or min_idle_ms < 0 or type(count) is not int or not 1 <= count <= 1000:
            raise TransportError("invalid_claim_limits")
        client, key = self.clients[lane], self.key(lane)
        pending = self._call(client.xpending_range, key, GROUPS[lane], "-", "+", count, idle=min_idle_ms)
        if not pending:
            return []
        rows = self._call(client.xclaim, key, GROUPS[lane], self.consumer, min_idle_ms,
                          [item["message_id"] for item in pending], write=True)
        return self._decode(lane, rows)

    def ack(self, delivery):
        self._consume_lane(delivery.lane)
        if not re.fullmatch(r"[0-9]+-[0-9]+", delivery.entry_id):
            raise TransportError("invalid_delivery_id")
        self._call(self.clients[delivery.lane].xack, self.key(delivery.lane), GROUPS[delivery.lane], delivery.entry_id, write=True)

    def quarantine(self, delivery, code):
        self._consume_lane(delivery.lane)
        if type(code) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code):
            raise TransportError("invalid_diagnostic_code")
        # Keep original immutable stream record. Diagnostics are metadata, never
        # a second task truth source or a fabricated business acknowledgement.
        value = json.dumps({"lane": delivery.lane, "entry_id": delivery.entry_id,
                            "code": code, "sha256": delivery.fingerprint}, separators=(",", ":"))
        self._call(self.clients["diagnostics"].hset, self.key("diagnostics"),
                   delivery.lane + ":" + delivery.entry_id, value, write=True)
        self.ack(delivery)

    def diagnostics(self, *, cursor=0, count=50):
        if type(cursor) is not int or cursor < 0 or type(count) is not int or not 1 <= count <= 1000:
            raise TransportError("invalid_diagnostic_cursor")
        following, values = self._call(self.clients["diagnostics"].hscan, self.key("diagnostics"), cursor=cursor, count=count)
        return {"next_cursor": following, "items": [json.loads(value) for value in values.values()]}

    def trim_reliable(self, lane, **_proof):
        if lane not in LANES:
            raise TransportError("invalid_lane")
        raise TransportError("reliable_retention_unproven")

    def _start_telemetry(self):
        with self._telemetry_lock:
            if self._telemetry_thread is None and not self._telemetry_stop.is_set():
                self._telemetry_thread = threading.Thread(
                    target=self._telemetry_loop, name="mc-telemetry-" + self.role, daemon=True)
                self._telemetry_thread.start()

    @staticmethod
    def _newer_telemetry(message, previous):
        sequence = "telemetry_seq" if message["type"] == "telemetry.heartbeat" else "progress_seq"
        # WorkerRuntime sequences increase across tasks within one epoch, so
        # an old attempt cannot displace a newer task's progress observation.
        return previous is None or message[sequence] > previous[sequence]

    def _telemetry(self, message):
        self._start_telemetry()
        kind = message["type"].split(".")[1]
        with self._telemetry_lock:
            if self._telemetry_stop.is_set():
                return "dropped"
            previous = self._telemetry_pending.get(kind)
            if not self._newer_telemetry(message, previous):
                return "coalesced"
            # Detach from callers; queueing never waits for Redis/network I/O.
            self._telemetry_pending[kind] = json.loads(json.dumps(message))
        self._telemetry_wake.set()
        return "queued"

    def _telemetry_loop(self):
        client = self.clients["telemetry"]
        while not self._telemetry_stop.is_set():
            subscriber = None
            try:
                if self.role == "worker":
                    self._telemetry_wake.wait(0.25)
                    self._telemetry_wake.clear()
                    with self._telemetry_lock:
                        pending, self._telemetry_pending = self._telemetry_pending, {}
                    for kind, message in pending.items():
                        if self._telemetry_stop.is_set():
                            break
                        client.publish(self.key("telemetry:" + kind),
                                       json.dumps(message, ensure_ascii=False, separators=(",", ":")))
                    continue
                subscriber = client.pubsub(ignore_subscribe_messages=False)
                channels = {self.key("telemetry:" + kind).encode(): kind for kind in ("heartbeat", "progress")}
                subscriber.subscribe(*channels)
                subscriber.connection.register_connect_callback(self._telemetry_reconnected)
                generation = self._telemetry_generation
                subscribed = set()
                while not self._telemetry_stop.is_set():
                    item = subscriber.get_message(timeout=0.1)
                    if generation != self._telemetry_generation:
                        generation = self._telemetry_generation
                        subscribed.clear()
                    if not item:
                        self._telemetry_stop.wait(0.01)
                        continue
                    channel = item.get("channel")
                    if channel not in channels:
                        continue
                    if item.get("type") == "subscribe":
                        subscribed.add(channel)
                        if len(subscribed) == 2:
                            self._telemetry_ready.set()
                        continue
                    if item.get("type") != "message":
                        continue
                    raw = item.get("data")
                    if not isinstance(raw, bytes) or len(raw) > MAX_MESSAGE_BYTES:
                        continue
                    try:
                        message = self._validate(parse_envelope(raw, capabilities=self.capabilities), "telemetry")
                        kind = channels[channel]
                        if message["type"] != "telemetry." + kind:
                            continue
                        # Containers and controller share the server clock.
                        # Old socket backlog must not become newly fresh just
                        # because the observer was paused before parsing it.
                        age = (datetime.now(timezone.utc) - datetime.fromisoformat(
                            message["created_at"].replace("Z", "+00:00"))).total_seconds()
                        if not -5 <= age < 30:
                            continue
                    except (ProtocolError, TransportError):
                        continue
                    with self._telemetry_lock:
                        sequence = "telemetry_seq" if kind == "heartbeat" else "progress_seq"
                        if message[sequence] > self._telemetry_seen.get(kind, -1):
                            self._telemetry_seen[kind] = message[sequence]
                            self._telemetry_latest[kind] = (time.monotonic() - max(0, age), message)
            except errors.RedisError:
                # Loss is intentional. Never replay telemetry from a Redis key
                # or write connection diagnostics per heartbeat to disk.
                pass
            finally:
                if subscriber is not None:
                    subscriber.close()
                self._telemetry_ready.clear()
                with self._telemetry_lock:
                    self._telemetry_latest.clear()
            self._telemetry_stop.wait(0.25)

    def _telemetry_reconnected(self, _connection):
        self._telemetry_ready.clear()
        with self._telemetry_lock:
            self._telemetry_generation += 1
            self._telemetry_latest.clear()

    def telemetry(self, kind):
        if self.role != "server" or kind not in {"heartbeat", "progress"}:
            raise TransportError("role_forbidden")
        self._start_telemetry()
        with self._telemetry_lock:
            value = self._telemetry_latest.get(kind)
            if value is None or time.monotonic() - value[0] >= 30 or not self._telemetry_ready.is_set():
                return None
            return json.loads(json.dumps(value[1]))
