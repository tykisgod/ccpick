from __future__ import annotations
from ccpick_app import runtime as _runtime, backend as _backend
import contextlib
import http.client
import importlib.util
import json
import math
import os
import stat
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

class CoordinationError(RuntimeError):
    pass
POLICY_FILE = _runtime.data_dir() / 'quota-policy.json'

def _manual_actions_allowed():
    path = POLICY_FILE.with_name('manual-actions.json')

    def stamp(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def private(info):
        return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and (stat.S_IMODE(info.st_mode) == 384) and (0 < info.st_size <= 4096) and (not hasattr(os, 'getuid') or info.st_uid == os.getuid())

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        expected = path.parent.resolve(strict=True) / path.name
        before = path.lstat()
        if not private(before) or path.resolve(strict=True) != expected:
            return False
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
        flags |= getattr(os, 'O_NONBLOCK', 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if not private(opened) or stamp(opened) != stamp(before):
                return False
            with os.fdopen(fd, 'rb', closefd=False) as handle:
                raw = handle.read(4097)
            after = os.fstat(fd)
            current = path.lstat()
            if len(raw) != opened.st_size or not private(after) or stamp(after) != stamp(opened) or (stamp(current) != stamp(after)) or (path.resolve(strict=True) != expected):
                return False
        finally:
            os.close(fd)
        document = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_keys)
        return type(document) is dict and set(document) == {'version', 'allowWhileWatchOnly'} and (type(document['version']) is int) and (document['version'] == 1) and (document['allowWhileWatchOnly'] is True)
    except (OSError, ValueError, TypeError, UnicodeError, RuntimeError):
        return False

def mutation_allowed(purpose):
    if type(purpose) is not str or purpose not in {'automatic', 'manual-switch', 'manual-login', 'manual-verify'}:
        return False
    import ccpick_usage as usage
    if not usage.watch_only():
        return True
    if purpose == 'automatic':
        return False
    try:
        before = POLICY_FILE.lstat()
        if not stat.S_ISREG(before.st_mode) or policy_settings()['policy'] not in ('quota-only', 'native-proxy') or child_environment() is None:
            return False
        after = POLICY_FILE.lstat()
        fields = ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if any((getattr(before, field) != getattr(after, field) for field in fields)):
            return False
    except (OSError, CoordinationError):
        return False
    return _manual_actions_allowed()

def _loopback_url(value):
    try:
        if not isinstance(value, str):
            raise ValueError()
        p = urlsplit(value)
        if p.scheme != 'http' or p.hostname not in ('127.0.0.1', '::1') or p.username or p.password or (p.path not in ('', '/')) or p.query or p.fragment or (not p.port):
            raise ValueError()
        return (p.hostname, p.port)
    except (TypeError, ValueError):
        raise CoordinationError('explicit_loopback_url_required') from None

def policy_settings():
    value = os.environ.get('CCSWITCH_POLICY', '').strip()
    if value not in ('', 'legacy', 'quota-only', 'native-proxy'):
        raise CoordinationError('invalid_switch_policy')
    result = {'policy': value or 'legacy', 'gateUrl': os.environ.get('CCSWITCH_GATE_URL', ''), 'proxyUrl': os.environ.get('CCSWITCH_PROXY_URL', '')}
    try:
        info = POLICY_FILE.lstat()
    except FileNotFoundError:
        return result
    except OSError:
        raise CoordinationError('device_policy_unavailable') from None
    if not stat.S_ISREG(info.st_mode):
        raise CoordinationError('invalid_device_policy')
    try:
        with POLICY_FILE.open('rb') as handle:
            raw = handle.read(4097)
    except OSError:
        raise CoordinationError('device_policy_unavailable') from None
    try:
        document = json.loads(raw.decode('utf-8-sig'))
        if isinstance(document, dict) and document.get('version') == 2:
            if len(raw) > 4096 or type(document['version']) is not int or set(document) != {'version', 'policy', 'proxyUrl'} or (document['policy'] != 'native-proxy'):
                raise ValueError()
            _loopback_url(document['proxyUrl'])
            if result['proxyUrl'] and result['proxyUrl'].rstrip('/') != document['proxyUrl'].rstrip('/'):
                raise CoordinationError('device_policy_environment_conflict')
            return {**document, 'gateUrl': ''}
        if len(raw) > 4096 or not isinstance(document, dict) or set(document) != {'version', 'policy', 'gateUrl', 'proxyUrl'} or (type(document['version']) is not int) or (document['version'] != 1) or (document['policy'] != 'quota-only'):
            raise ValueError()
        _loopback_url(document['gateUrl'])
        _loopback_url(document['proxyUrl'])
    except (ValueError, TypeError, UnicodeError, CoordinationError):
        raise CoordinationError('invalid_device_policy') from None
    if value and value != 'quota-only':
        raise CoordinationError('device_policy_environment_conflict')
    for key in ('gateUrl', 'proxyUrl'):
        if result[key] and result[key].rstrip('/') != document[key].rstrip('/'):
            raise CoordinationError('device_policy_environment_conflict')
    return document

def enabled() -> bool:
    return policy_settings()['policy'] == 'quota-only'

def child_environment():
    settings = policy_settings()
    if settings['policy'] not in ('quota-only', 'native-proxy'):
        return None
    value = settings['proxyUrl']
    try:
        if not value:
            raise CoordinationError('explicit_loopback_proxy_required')
        _loopback_url(value)
    except CoordinationError:
        raise CoordinationError('explicit_loopback_proxy_required') from None
    env = dict(os.environ)
    env['CCSWITCH_POLICY'] = settings['policy']
    env['CCSWITCH_GATE_URL'] = settings['gateUrl']
    env['CCSWITCH_PROXY_URL'] = value
    proxy_names = {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'WS_PROXY', 'WSS_PROXY', 'NO_PROXY'}
    for key in list(env):
        if key.upper() in proxy_names or (settings['policy'] == 'native-proxy' and key.upper() == 'ANTHROPIC_BASE_URL'):
            del env[key]
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'WS_PROXY', 'WSS_PROXY'):
        env[name] = value
        env[name.lower()] = value
    env['NO_PROXY'] = env['no_proxy'] = 'localhost,127.0.0.1,::1'
    return env

def strict_roster(usage, seq=None) -> dict:
    roster = usage.managed_roster(seq)
    if not isinstance(roster, dict) or not roster or any((not isinstance(v, str) or not v.strip() for v in roster.values())):
        raise CoordinationError('device_pool_unavailable')
    return roster

def quota_state(row, now=None, max_age=90.0) -> str:
    now = time.time() if now is None else now
    if not isinstance(row, dict) or row.get('error') or row.get('denied'):
        return 'unknown'
    stamp = row.get('fetchedAt')
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or (not math.isfinite(stamp)):
        return 'unknown'
    if not 0 <= now - stamp <= max_age:
        return 'unknown'
    windows = row.get('counted')
    resets = row.get('resets')
    if not isinstance(windows, dict) or not {'5h', '7d'}.issubset(windows) or (not isinstance(resets, dict)):
        return 'unknown'
    from datetime import datetime
    for name, pct in windows.items():
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or (not math.isfinite(pct)) or (pct < 0):
            return 'unknown'
        if pct == 0 and resets.get(name) in (None, ''):
            continue
        try:
            reset = datetime.fromisoformat(str(resets[name]).replace('Z', '+00:00'))
            if reset.tzinfo is None or reset.timestamp() <= now:
                return 'unknown'
        except (KeyError, TypeError, ValueError, OverflowError):
            return 'unknown'
    return 'exhausted' if any((pct >= 100 for pct in windows.values())) else 'available'

class GateClient:

    def __init__(self, url=None):
        self.url = url or policy_settings()['gateUrl']
        try:
            self.host, self.port = _loopback_url(self.url)
        except CoordinationError:
            raise CoordinationError('explicit_loopback_gate_required') from None

    def request(self, route, lease=None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=3)
        try:
            body = None if lease is None else json.dumps({'leaseId': lease})
            headers = {} if lease is None else {'content-type': 'application/json', 'x-request-gate-action': 'switch_' + route.rsplit('/', 1)[-1]}
            connection.request('GET' if lease is None else 'POST', route, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(16385)
            if response.status != 200 or len(raw) > 16384:
                raise CoordinationError('gate_control_rejected')
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except CoordinationError:
            raise
        except Exception:
            raise CoordinationError('gate_unavailable') from None
        finally:
            connection.close()

    def status(self):
        result = self.request('/status')
        persistence = result.get('persistence') or {}
        admission = result.get('admission') or {}
        if not isinstance(persistence, dict) or not isinstance(admission, dict) or result.get('service') != 'tyk-claude-request-gate' or (result.get('version') != 1) or result.get('closing') or (persistence.get('enabled') is not True) or (persistence.get('healthy') is not True) or (admission.get('kind') != 'tyk-fixed-egress'):
            raise CoordinationError('strict_gate_required')
        self.validate_switch(result.get('switch'))
        return result

    @staticmethod
    def validate_switch(value):
        if not isinstance(value, dict) or not isinstance(value.get('paused'), bool) or (not isinstance(value.get('drained'), bool)) or isinstance(value.get('inFlight'), bool) or (not isinstance(value.get('inFlight'), int)) or (value['inFlight'] < 0):
            raise CoordinationError('invalid_gate_state')
        lease = value.get('leaseId')
        try:
            if value['paused']:
                if not isinstance(lease, str) or str(uuid.UUID(lease)) != lease:
                    raise ValueError()
            elif lease is not None or value['drained']:
                raise ValueError()
            if value['drained'] and value['inFlight'] != 0:
                raise ValueError()
        except ValueError:
            raise CoordinationError('invalid_gate_state') from None
        return value

class Coordinator:

    def __init__(self, directory=None, gate=None, drain_timeout=120.0, can_mutate=None):
        if directory is None:
            import ccpick_usage
            directory = ccpick_usage.WATCH_ONLY_FLAG.parent / 'request-gate-switch'
        self.directory = Path(directory)
        self.gate = gate or GateClient()
        self.drain_timeout = drain_timeout
        self.can_mutate = can_mutate or (lambda: True)
        self.journal = None
        self.handle = None

    def __enter__(self):
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.handle = (self.directory / 'switch.lock').open('a+b')
        except OSError:
            raise CoordinationError('switch_journal_unavailable') from None
        try:
            self.handle.seek(0, os.SEEK_END)
            if self.handle.tell() == 0:
                self.handle.write(b'0')
                self.handle.flush()
            self.handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise CoordinationError('switch_coordinator_busy') from None
        try:
            filename = self.directory / 'switch.json'
            if filename.exists():
                if filename.stat().st_size > 2048:
                    raise ValueError()
                value = json.loads(filename.read_text(encoding='utf-8'))
                if not isinstance(value, dict) or set(value) != {'version', 'leaseId', 'phase'} or value['version'] != 1 or (value['phase'] not in ('idle', 'waiting', 'switching', 'verified', 'failed')):
                    raise ValueError()
                if value['phase'] == 'idle':
                    if value['leaseId'] is not None:
                        raise ValueError()
                elif not isinstance(value['leaseId'], str) or str(uuid.UUID(value['leaseId'])) != value['leaseId']:
                    raise ValueError()
                self.journal = value
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise CoordinationError('switch_journal_requires_review') from None

    def __exit__(self, *_):
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    @property
    def pending(self):
        return bool(self.journal and self.journal['phase'] != 'idle')

    def _save(self, lease, phase):
        value = {'version': 1, 'leaseId': lease, 'phase': phase}
        temporary = self.directory / ('switch.' + str(uuid.uuid4()) + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as handle:
                json.dump(value, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.directory / 'switch.json')
            if os.name != 'nt':
                fd = os.open(str(self.directory), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            self.journal = value
        except Exception:
            raise CoordinationError('switch_journal_unavailable') from None
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()

    def _require_mutation(self):
        from ccpick_app.account_context import manager
        if manager() is not None:
            raise CoordinationError('legacy_credential_mutation_disabled')
        if not self.can_mutate():
            raise CoordinationError('watch_only_enabled')

    def hold(self, *, rollback=False):
        if not rollback:
            self._require_mutation()
        status = self.gate.status()['switch']
        if not self.pending:
            if status['paused']:
                raise CoordinationError('foreign_switch_lease_requires_review')
            self._save(str(uuid.uuid4()), 'waiting')
        lease = self.journal['leaseId']
        if status['paused'] and status['leaseId'] != lease:
            raise CoordinationError('foreign_switch_lease_requires_review')
        if not rollback:
            self._require_mutation()
        result = self.gate.validate_switch(self.gate.request('/control/switch/pause', lease))
        if not result['paused'] or result['leaseId'] != lease:
            raise CoordinationError('switch_pause_not_confirmed')
        return result

    def drained(self):
        self.hold()
        deadline = time.monotonic() + self.drain_timeout
        while True:
            result = self.gate.status()['switch']
            if not result['paused'] or result['leaseId'] != self.journal['leaseId']:
                raise CoordinationError('switch_lease_lost')
            if result['drained']:
                return
            if time.monotonic() >= deadline:
                raise CoordinationError('switch_waiting_for_inflight')
            time.sleep(0.2)

    def resume_current(self, guard=None):
        if not self.pending:
            return
        if self.journal['phase'] in ('switching', 'failed'):
            raise CoordinationError('manual_verified_switch_required')
        self.drained()
        if self.gate.status()['admission'].get('ready') is not True:
            raise CoordinationError('fixed_egress_not_ready')
        if guard is not None:
            guard()
        self._require_mutation()
        try:
            result = self.gate.validate_switch(self.gate.request('/control/switch/resume', self.journal['leaseId']))
            if result['paused']:
                raise CoordinationError('switch_resume_not_confirmed')
            self._save(None, 'idle')
        except Exception:
            with contextlib.suppress(Exception):
                self.hold(rollback=True)
            raise CoordinationError('switch_resume_uncertain_manual_review_required') from None

    def switch(self, operation, *, manual=False, guard=None):
        if self.pending and self.journal['phase'] in ('switching', 'failed') and (not manual):
            raise CoordinationError('manual_verified_switch_required')
        self.drained()
        if self.gate.status()['admission'].get('ready') is not True:
            raise CoordinationError('fixed_egress_not_ready')
        if guard is not None:
            guard()
        self._require_mutation()
        self._save(self.journal['leaseId'], 'switching')
        try:
            if not operation():
                raise CoordinationError('switch_or_identity_verification_failed')
            self._save(self.journal['leaseId'], 'verified')
            self.resume_current()
        except Exception:
            with contextlib.suppress(Exception):
                self.hold(rollback=True)
            self._save(self.journal['leaseId'], 'failed')
            raise CoordinationError('switch_failed_gate_paused_manual_verification_required') from None

def manual_switch(cswap, target, operation, *, target_guard=None):
    import ccpick_usage as usage
    from ccpick_enroll import auth_status
    try:
        if not mutation_allowed('manual-switch'):
            return (False, 'watch_only_enabled')
        child_environment()
        with Coordinator(can_mutate=lambda: mutation_allowed('manual-switch')) as coordinator:

            def guard():
                if not mutation_allowed('manual-switch'):
                    raise CoordinationError('watch_only_enabled')
                if target not in strict_roster(usage).values():
                    raise CoordinationError('target_not_in_device_pool')
                if not auth_status().get('email'):
                    raise CoordinationError('active_identity_unavailable')
                if target_guard is not None:
                    target_guard()
            guard()
            coordinator.switch(operation, manual=True, guard=guard)
        return (True, 'switch_verified')
    except CoordinationError as error:
        return (False, str(error))
    except Exception:
        return (False, 'switch_coordination_unavailable')

def run_auto(*, dry_run=False, refresh_usage=True):
    here = Path(__file__).resolve().parent
    source = here / 'autoswitch' / 'claude-autoswitch-decide.py'
    if not source.is_file():
        source = here / 'claude-autoswitch-decide.py'
    spec = importlib.util.spec_from_file_location('ccpick_quota_decide', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.decide_quota_only(dry_run, allow_switch=not module.watch_only(), refresh_usage=refresh_usage)

def cmd_switch(args):
    import argparse
    from ccpick_auto import switch_and_verify
    from ccpick_enroll import cswap_bin
    parser = argparse.ArgumentParser(prog='ccpick switch', description='验证本机账号切换；严格模式会先排空网关')
    parser.add_argument('target')
    ns = parser.parse_args(args)
    cswap = cswap_bin()
    if not cswap:
        print('switch_program_unavailable')
        return 1
    ok, _ = switch_and_verify(cswap, ns.target)
    print('switch_verified' if ok else 'switch_failed_or_paused; check the local switch status')
    return 0 if ok else 1
