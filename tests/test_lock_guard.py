"""Rete di sicurezza della serratura: porta bloccata quando resta Aperta a Tempo oltre il tempo o si apre."""

import asyncio

from app.events import EventMonitor
from app.lock_guard import FAILED, FORCED, MARGIN, MAX_ATTEMPTS, OPEN_GRACE, RETRY_AFTER, UNKNOWN_LIMIT, LockGuard
from app.vapix import VapixError


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Controller:
    """Porte con modalità e tempi; LockDoor blocca, a meno che il controller non lo ignori."""

    def __init__(self, mode="Accessed", times=None):
        self.modes = {"porta": mode}
        self.times = times if times is not None else {"porta": ("PT5S", "PT20S")}
        self.calls = []
        self.ignore_lock = False
        self.fail_read = False
        self.fail_lock = False

    async def list_doors(self):
        self.calls.append(("list_doors",))
        return [{"token": t, "AccessTime": a, "ExtendedAccessTime": e} for t, (a, e) in self.times.items()]

    async def door_mode(self, door):
        if self.fail_read:
            raise VapixError("timeout")
        return self.modes.get(door)

    async def lock_door(self, door):
        self.calls.append(("lock_door", door))
        if self.fail_lock:
            raise VapixError("rifiutato")
        if not self.ignore_lock:
            self.modes[door] = "Locked"


class Setup:
    def __init__(self, mode="Accessed", contact=None, times=None):
        self.clock = Clock()
        self.guard = LockGuard(clock=self.clock)
        self.monitor = EventMonitor(prefix="axis")
        self.monitor.door_modes["porta"] = mode
        self.controller = Controller(mode, times)
        self.contact = {"porta": contact}
        self.alarms = []
        self.changes = 0
        self.locks = {}

    def changed(self):
        self.changes += 1

    def run(self, seconds=0.0):
        self.clock.now += seconds
        asyncio.run(
            self.guard.tick(
                self.monitor,
                self.controller,
                door_lock=lambda d: self.locks.setdefault(d, asyncio.Lock()),
                contact_open=lambda d: self.contact.get(d),
                log_alarm=lambda d, detail: self.alarms.append((d, detail)),
                changed=self.changed,
            )
        )

    def lock_calls(self):
        return [c for c in self.controller.calls if c[0] == "lock_door"]

    def accessed(self, mode="Accessed"):
        """La porta cambia modalità (evento del controller)."""
        self.monitor.door_modes["porta"] = mode
        self.controller.modes["porta"] = mode


def test_normal_opening_is_left_alone():
    s = Setup()
    s.guard.granted("porta")
    s.run()
    s.run(4.9 + MARGIN)
    assert s.lock_calls() == []
    # Il controller riblocca a fine tempo: nessun comando
    s.accessed("Locked")
    s.run(5)
    assert s.lock_calls() == [] and s.alarms == [] and s.guard.accesses == {}


def test_overtime_locks_and_logs_alarm():
    """Come in loco il 3/11: la porta resta Aperta a Tempo (contatto che segna aperta senza contatto collegato)."""
    s = Setup()
    s.guard.granted("porta")
    s.run()
    s.run(5 + MARGIN + 0.1)
    assert s.lock_calls() == [("lock_door", "porta")]
    assert s.alarms == [("porta", FORCED)]
    assert s.monitor.door_modes["porta"] == "Locked" and s.changes == 1
    s.run(10)
    assert len(s.lock_calls()) == 1


def test_extended_opening_uses_extended_time():
    s = Setup()
    s.guard.granted("porta", extended=True)
    s.run()
    s.run(19 + MARGIN)
    assert s.lock_calls() == []
    s.run(1.1)
    assert s.lock_calls() == [("lock_door", "porta")]


def test_new_grant_restarts_the_time():
    s = Setup()
    s.guard.granted("porta")
    s.run()
    s.run(4)
    # Seconda tessera mentre è ancora aperta: il controller fa ripartire il tempo
    s.guard.granted("porta")
    s.run(MARGIN + 2)
    assert s.lock_calls() == []
    s.run(3.1)
    assert s.lock_calls() == [("lock_door", "porta")]


def test_accessed_seen_without_grant_counts_from_now():
    """Aperta a Tempo letta con le API (eventi persi): il tempo parte da quando l'app la vede."""
    s = Setup()
    s.run()
    s.run(5 + MARGIN - 0.1)
    assert s.lock_calls() == []
    s.run(0.2)
    assert s.lock_calls() == [("lock_door", "porta")]


def test_old_grant_does_not_count():
    s = Setup(mode="Locked")
    s.guard.granted("porta")
    s.run(10)
    s.accessed()
    s.run()
    s.run(5 + MARGIN - 0.1)
    assert s.lock_calls() == []


def test_door_opened_during_access_locks_without_alarm():
    """Il controller con il contatto porta tiene la serratura alimentata finché la porta è aperta: si blocca subito."""
    s = Setup(contact=False)
    s.guard.granted("porta")
    s.run()
    s.run(2)
    s.contact["porta"] = True
    s.run()
    assert s.lock_calls() == []
    s.run(OPEN_GRACE)
    assert s.lock_calls() == [("lock_door", "porta")]
    assert s.alarms == []
    assert s.monitor.door_modes["porta"] == "Locked"


def test_door_already_open_at_grant_locks_at_once():
    s = Setup(contact=True)
    s.guard.granted("porta")
    s.run()
    s.run(OPEN_GRACE)
    assert s.lock_calls() == [("lock_door", "porta")]
    assert s.alarms == []


def test_contact_open_only_for_an_instant():
    s = Setup(contact=True)
    s.guard.granted("porta")
    s.run()
    s.contact["porta"] = False
    s.run(OPEN_GRACE)
    assert s.lock_calls() == []


def test_mode_already_locked_on_controller_is_not_touched():
    """Evento "Bloccata" perso o in ritardo: prima di bloccare si rilegge, e la porta già bloccata resta com'è."""
    s = Setup()
    s.run()
    s.controller.modes["porta"] = "Locked"
    s.run(5 + MARGIN + 1)
    assert s.lock_calls() == [] and s.alarms == []
    assert s.monitor.door_modes["porta"] == "Locked" and s.changes == 1


def test_lock_ignored_retries_then_reports_failure():
    s = Setup()
    s.controller.ignore_lock = True
    s.run()
    s.run(5 + MARGIN + 0.1)
    assert len(s.lock_calls()) == 1 and s.alarms == []
    s.run(RETRY_AFTER - 0.5)
    assert len(s.lock_calls()) == 1
    for _ in range(MAX_ATTEMPTS):
        s.run(RETRY_AFTER)
    assert len(s.lock_calls()) == MAX_ATTEMPTS
    assert s.alarms == [("porta", FAILED)]


def test_lock_error_retries():
    s = Setup()
    s.controller.fail_lock = True
    s.run()
    s.run(5 + MARGIN + 0.1)
    assert len(s.lock_calls()) == 1
    s.controller.fail_lock = False
    s.run(RETRY_AFTER)
    assert len(s.lock_calls()) == 2
    assert s.alarms == [("porta", FORCED)]


def test_read_error_waits():
    s = Setup()
    s.controller.fail_read = True
    s.run()
    s.run(5 + MARGIN + 0.1)
    assert s.lock_calls() == []
    s.controller.fail_read = False
    s.run(1)
    assert s.lock_calls() == []
    s.run(RETRY_AFTER)
    assert s.lock_calls() == [("lock_door", "porta")]


def test_unknown_times_use_the_maximum():
    s = Setup(times={})
    s.run()
    s.run(UNKNOWN_LIMIT + MARGIN - 0.1)
    assert s.lock_calls() == []
    s.run(0.2)
    assert s.lock_calls() == [("lock_door", "porta")]


def test_times_are_cached_and_reread_after_invalidate():
    s = Setup()
    s.run()
    s.run(1)
    reads = [c for c in s.controller.calls if c[0] == "list_doors"]
    assert len(reads) == 1
    s.guard.invalidate_times()
    s.run(1)
    assert len([c for c in s.controller.calls if c[0] == "list_doors"]) == 2


def test_no_reads_when_no_door_is_open():
    s = Setup(mode="Locked")
    s.run()
    s.run(100)
    assert s.controller.calls == []


def test_new_access_after_lock_is_tracked_again():
    s = Setup()
    s.run()
    s.run(5 + MARGIN + 0.1)
    assert len(s.lock_calls()) == 1
    s.run(1)
    s.guard.granted("porta")
    s.accessed()
    s.run(1)
    s.run(5 + MARGIN)
    assert len(s.lock_calls()) == 2
