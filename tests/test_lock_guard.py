"""Rete di sicurezza della serratura: porta bloccata quando resta Aperta a Tempo oltre il tempo o si apre."""

import asyncio

from app.events import EventMonitor
from app.lock_guard import (
    CHECK_EVERY,
    FAILED,
    FORCED,
    LOG_GRANT_MAX,
    MARGIN,
    MAX_ACCESS,
    MAX_ATTEMPTS,
    MAX_EXTENDED,
    OPEN_GRACE,
    RETRY_AFTER,
    SLOW_RETRY,
    LockGuard,
)
from app.vapix import VapixError


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Controller:
    """Porte con modalità e tempi; LockDoor blocca, a meno che il controller non lo ignori."""

    def __init__(self, clock, mode="Accessed", times=None):
        self.clock = clock
        self.modes = {"porta": mode}
        self.times = times if times is not None else {"porta": ("PT5S", "PT20S")}
        self.calls = []
        self.ignore_lock = False
        self.fail_read = False
        self.fail_lock = False
        self.fail_after_lock = False
        # Secondi che passano a ogni chiamata (controller lento)
        self.delay = 0.0
        # Chiamata durante la lettura della porta (per esempio un consenso che arriva in quel momento)
        self.on_read = None
        self.slow_list = False

    async def list_doors(self):
        self.calls.append(("list_doors",))
        if self.slow_list:
            await asyncio.sleep(0.01)
        return [{"token": t, "AccessTime": a, "ExtendedAccessTime": e} for t, (a, e) in self.times.items()]

    async def door_mode(self, door):
        self.clock.now += self.delay
        self.calls.append(("door_mode", door))
        if self.on_read:
            callback, self.on_read = self.on_read, None
            callback()
        if self.fail_read:
            raise VapixError("timeout")
        return self.modes.get(door)

    async def lock_door(self, door):
        self.clock.now += self.delay
        self.calls.append(("lock_door", door))
        if self.fail_lock:
            raise VapixError("rifiutato")
        if not self.ignore_lock:
            self.modes[door] = "Locked"
        if self.fail_after_lock:
            self.fail_after_lock = False
            raise VapixError("risposta persa")


class Setup:
    def __init__(self, mode="Accessed", contact=None, times=None):
        self.clock = Clock()
        self.guard = LockGuard(clock=self.clock, cut_on_open=True)
        self.monitor = EventMonitor(prefix="axis")
        self.monitor.door_modes["porta"] = mode
        self.controller = Controller(self.clock, mode, times)
        self.contact = {"porta": contact}
        self.alarms = []
        self.changes = 0
        self.locks = {}
        self.wait = None
        # Registro del controller senza eventi in diretta: (impronta, ora del consenso sull'orologio del test)
        self.log_grant = None
        self.log_reads = 0

    def changed(self):
        self.changes += 1

    def run(self, seconds=0.0, live=True):
        self.clock.now += seconds
        self.wait = asyncio.run(self.tick(live))

    async def tick(self, live=True):
        return await self.guard.tick(
            self.monitor,
            self.controller,
            door_lock=lambda d: self.locks.setdefault(d, asyncio.Lock()),
            contact_open=lambda d: self.contact.get(d),
            log_alarm=lambda d, detail: self.alarms.append((d, detail)),
            changed=self.changed,
            live=live,
            last_grant=self.last_grant,
        )

    async def last_grant(self, door):
        self.log_reads += 1
        if self.log_grant is None:
            return None
        ident, at = self.log_grant
        # None: orologio del controller un secondo avanti, il consenso sembra sempre appena passato. "?": orologio del
        # controller non misurato, i secondi non si sanno
        if at == "?":
            return ident, None
        return ident, -1.0 if at is None else self.clock.now - at

    def lock_calls(self):
        return [c for c in self.controller.calls if c[0] == "lock_door"]

    def accessed(self, mode="Accessed", door="porta"):
        """La porta cambia modalità sul controller e (evento in diretta) in memoria."""
        self.monitor.door_modes[door] = mode
        self.controller.modes[door] = mode


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
    assert s.wait == CHECK_EVERY
    s.run(5 + MARGIN - 0.5)
    # Il prossimo giro arriva quando serve, non al secondo pieno
    assert s.wait == 0.5
    s.run(0.5)
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


def test_times_beyond_the_app_maximum_are_capped():
    """Un tempo messo sul controller fuori dall'app non tiene la serratura alimentata oltre i massimi."""
    s = Setup(times={"porta": ("PT300S", "PT600S")})
    s.run()
    s.run(MAX_ACCESS + MARGIN + 0.1)
    assert s.lock_calls() == [("lock_door", "porta")] and s.alarms == [("porta", FORCED)]
    s = Setup(times={"porta": ("PT300S", "PT600S")})
    s.guard.granted("porta", extended=True)
    s.run()
    s.run(MAX_EXTENDED + MARGIN + 0.1)
    assert len(s.lock_calls()) == 1


def test_unknown_times_use_the_maximum():
    s = Setup(times={})
    s.run()
    s.run(MAX_ACCESS + MARGIN - 0.1)
    assert s.lock_calls() == []
    s.run(0.2)
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
    """Aperta a Tempo letta con le API (consenso perso): il tempo parte da quando l'app la vede."""
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
    s.guard.granted("porta", door_open=False)
    s.run()
    s.run(2)
    s.contact["porta"] = True
    s.run()
    assert s.lock_calls() == [] and s.wait == OPEN_GRACE
    s.run(OPEN_GRACE)
    assert s.lock_calls() == [("lock_door", "porta")]
    assert s.alarms == []
    assert s.monitor.door_modes["porta"] == "Locked"


def test_cut_on_open_is_off_by_default():
    """Finché il blocco all'apertura non è provato in loco: la porta che si apre resta a tempo, vale solo il tempo."""
    s = Setup(contact=False)
    s.guard.cut_on_open = False
    s.guard.granted("porta", door_open=False)
    s.run()
    s.run(2)
    s.contact["porta"] = True
    s.run(OPEN_GRACE)
    s.run(1)
    assert s.lock_calls() == [] and s.alarms == []
    s.run(2 + MARGIN)
    assert s.lock_calls() == [("lock_door", "porta")] and s.alarms == [("porta", FORCED)]
    assert LockGuard().cut_on_open is False


def test_door_opened_right_after_the_grant():
    """Pulsante e porta spinta insieme: il consenso ha visto la porta chiusa, il primo giro la vede già aperta."""
    s = Setup(contact=True)
    s.guard.granted("porta", door_open=False)
    s.run()
    s.run(OPEN_GRACE)
    assert s.lock_calls() == [("lock_door", "porta")]


def test_door_already_open_at_grant_is_not_cut():
    """Porta già aperta (materiale, porta che si sta chiudendo) al consenso: chi ha passato la tessera deve poterla
    spingere. La serratura si riblocca a fine tempo, come senza contatto."""
    s = Setup(contact=True)
    s.guard.granted("porta", door_open=True)
    s.run()
    s.run(2)
    assert s.lock_calls() == []
    s.accessed("Locked")
    s.run(5)
    assert s.lock_calls() == [] and s.alarms == []


def test_door_closing_at_grant_then_opened_again_is_cut():
    s = Setup(contact=True)
    s.guard.granted("porta", door_open=True)
    s.run()
    s.contact["porta"] = False
    s.run(1)
    s.contact["porta"] = True
    s.run(0.5)
    s.run(OPEN_GRACE)
    assert s.lock_calls() == [("lock_door", "porta")]


def test_contact_stuck_open_does_not_cut_every_access():
    """Contatto che segna sempre aperta (cavo tagliato, non collegato): le aperture durano il loro tempo."""
    s = Setup(contact=True)
    for _ in range(3):
        s.guard.granted("porta", door_open=True)
        s.accessed()
        s.run()
        s.run(4)
        s.accessed("Locked")
        s.run(1)
    assert s.lock_calls() == []


def test_contact_open_only_for_an_instant():
    s = Setup(contact=False)
    s.guard.granted("porta", door_open=False)
    s.run()
    s.contact["porta"] = True
    s.run(0.1)
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


def test_lock_applied_but_answer_lost_still_logs_the_alarm():
    s = Setup()
    s.controller.fail_after_lock = True
    s.run()
    s.run(5 + MARGIN + 0.1)
    assert len(s.lock_calls()) == 1 and s.alarms == []
    # Il controller l'ha bloccata: l'evento "Bloccata" arriva, e l'allarme resta
    s.monitor.door_modes["porta"] = "Locked"
    s.run(1)
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


def test_without_live_events_two_accesses_are_not_merged():
    """Revisione: solo letture ogni 10 s, il "Bloccata" tra due tessere non si vede. Si rilegge il controller a ogni
    giro: la seconda apertura ha il suo tempo."""
    s = Setup()
    s.run(live=False)
    s.run(2, live=False)
    # Fine della prima apertura: in memoria resta Aperta a Tempo (lettura ogni 10 s)
    s.controller.modes["porta"] = "Locked"
    s.run(1, live=False)
    assert s.guard.accesses == {} and s.monitor.door_modes["porta"] == "Locked"
    # La lettura dei 10 s vede la seconda tessera
    s.accessed()
    s.run(4, live=False)
    s.run(5 + MARGIN - 0.5, live=False)
    assert s.lock_calls() == [] and s.alarms == []
    s.run(0.6, live=False)
    assert s.lock_calls() == [("lock_door", "porta")]


def test_without_live_events_the_contact_is_not_used():
    s = Setup(contact=False)
    s.run(live=False)
    s.contact["porta"] = True
    s.run(1, live=False)
    s.run(1, live=False)
    assert s.lock_calls() == []


def test_open_from_the_app_while_waiting_is_not_locked():
    """Revisione: il blocco aspettava la porta occupata da un'apertura dall'app, e poi chiudeva quella nuova."""
    s = Setup()
    s.run()

    async def scenario():
        lock = s.locks.setdefault("porta", asyncio.Lock())
        await lock.acquire()
        s.clock.now += 5 + MARGIN + 0.1
        task = asyncio.create_task(s.tick())
        await asyncio.sleep(0)
        # Apertura dall'app: il controller la apre, e il consenso arriva alla rete di sicurezza
        s.controller.modes["porta"] = "Accessed"
        s.guard.granted("porta", door_open=False)
        lock.release()
        return await task

    asyncio.run(scenario())
    assert s.lock_calls() == [] and s.alarms == []


def test_grant_during_the_read_before_locking():
    s = Setup()
    s.run()
    s.controller.on_read = lambda: s.guard.granted("porta", door_open=False)
    s.run(5 + MARGIN + 0.1)
    assert s.lock_calls() == [] and s.alarms == []


def test_second_door_keeps_its_grace_with_a_slow_controller():
    """Revisione: l'ora del giro restava quella di prima delle chiamate al controller per la prima porta."""
    s = Setup()
    s.run()
    s.clock.now += 5 + MARGIN + 0.1
    # La porta principale è da bloccare (tempo scaduto); intanto la seconda si apre subito dopo il consenso
    s.controller.times["porta2"] = ("PT5S", "PT20S")
    s.guard.granted("porta2", door_open=False)
    s.accessed(door="porta2")
    s.contact["porta2"] = True
    s.controller.delay = 0.4
    s.run()
    # Bloccata la principale; la seconda aspetta il suo mezzo secondo da quando l'app la vede aperta
    assert s.lock_calls() == [("lock_door", "porta")]
    s.controller.delay = 0
    s.run(OPEN_GRACE)
    assert s.lock_calls() == [("lock_door", "porta"), ("lock_door", "porta2")]


def test_after_the_failure_alarm_it_keeps_trying_slowly():
    """Revisione: dopo tre blocchi non riusciti la rete di sicurezza si fermava, con la serratura alimentata."""
    s = Setup()
    s.controller.ignore_lock = True
    s.run()
    s.run(5 + MARGIN + 0.1)
    for _ in range(MAX_ATTEMPTS - 1):
        s.run(RETRY_AFTER)
    assert len(s.lock_calls()) == MAX_ATTEMPTS and s.alarms == [("porta", FAILED)]
    s.run(SLOW_RETRY - 1)
    assert len(s.lock_calls()) == MAX_ATTEMPTS
    s.controller.ignore_lock = False
    s.run(1)
    assert len(s.lock_calls()) == MAX_ATTEMPTS + 1 and s.monitor.door_modes["porta"] == "Locked"
    # Un allarme solo
    assert s.alarms == [("porta", FAILED)]


def test_without_live_events_a_card_in_the_log_restarts_the_time():
    """Revisione: senza eventi in diretta la tessera passata durante l'apertura non arrivava, e la rete di sicurezza
    tagliava l'apertura nuova con un falso allarme."""
    s = Setup()
    s.guard.granted("porta")
    s.run(live=False)
    s.log_grant = ("r1", s.clock.now - 0.1)
    s.run(3, live=False)
    # Tessera a +3 nel registro
    s.log_grant = ("r2", s.clock.now)
    s.run(5 + MARGIN - 3 + 0.1, live=False)
    assert s.lock_calls() == [] and s.alarms == []
    s.run(3 - 0.2, live=False)
    assert s.lock_calls() == []
    s.run(0.2, live=False)
    assert s.lock_calls() == [("lock_door", "porta")] and s.alarms == [("porta", FORCED)]


def test_without_live_events_the_access_starts_from_the_log():
    """Revisione: con la lettura ogni 10 s l'apertura si vedeva tardi e il tempo partiva da lì."""
    s = Setup()
    s.log_grant = ("r1", s.clock.now - 4)
    s.run(live=False)
    s.run(1 + MARGIN - 0.1, live=False)
    assert s.lock_calls() == []
    s.run(0.2, live=False)
    assert s.lock_calls() == [("lock_door", "porta")]


def test_without_live_events_old_or_future_log_grants_are_not_used():
    s = Setup()
    # Troppo vecchio per l'apertura appena vista: può venire da altro
    s.log_grant = ("r1", s.clock.now - LOG_GRANT_MAX - 1)
    s.run(live=False)
    started = s.guard.accesses["porta"].started
    assert started == s.clock.now
    # Orologio del controller avanti: un consenso nel futuro non fa ripartire il tempo
    s.log_grant = ("r2", s.clock.now + 20)
    s.run(5 + MARGIN, live=False)
    assert s.lock_calls() == [("lock_door", "porta")]


def test_the_same_log_grant_restarts_the_time_once():
    """Orologio del controller un po' avanti: lo stesso consenso non fa ripartire il tempo a ogni giro."""
    s = Setup()
    s.run(live=False)
    s.run(4, live=False)
    s.log_grant = ("r1", None)
    s.run(1 + MARGIN, live=False)
    assert s.lock_calls() == [] and s.guard.accesses["porta"].started == s.clock.now
    s.run(5 + MARGIN, live=False)
    assert s.lock_calls() == [("lock_door", "porta")]


def test_with_live_events_the_log_is_not_read():
    s = Setup()
    s.guard.granted("porta")
    s.run()
    s.run(5 + MARGIN + 0.1)
    assert s.log_reads == 0 and s.lock_calls() == [("lock_door", "porta")]


def test_alarm_kept_when_a_new_grant_replaces_an_unconfirmed_forced_lock():
    """Revisione: blocco per il tempo scaduto accettato, ma una tessera subito dopo: l'allarme si perdeva."""
    s = Setup()
    s.run()
    s.controller.fail_after_lock = True
    s.run(5 + MARGIN + 0.1)
    assert len(s.lock_calls()) == 1 and s.alarms == []
    s.guard.granted("porta")
    s.run(0.5)
    assert s.alarms == [("porta", FORCED)]


def test_times_read_during_an_invalidation_are_read_again():
    s = Setup()

    async def scenario():
        reading = asyncio.create_task(s.guard._read_times(s.controller))
        await asyncio.sleep(0)
        s.guard.invalidate_times()
        await reading

    s.controller.slow_list = True
    asyncio.run(scenario())
    assert s.guard.times_at is None


def test_without_a_measured_controller_clock_a_new_log_grant_counts_from_now():
    """Revisione: con l'orologio del controller indietro e non misurato l'età del consenso era sbagliata, e la rete di
    sicurezza tagliava in anticipo. Senza misura un consenso nuovo vale da quando si vede, mai da prima."""
    s = Setup()
    s.log_grant = ("r1", "?")
    s.run(live=False)
    assert s.guard.accesses["porta"].started == s.clock.now
    s.run(4, live=False)
    s.log_grant = ("r2", "?")
    s.run(1 + MARGIN, live=False)
    assert s.lock_calls() == [] and s.guard.accesses["porta"].started == s.clock.now
    s.run(5 + MARGIN, live=False)
    assert s.lock_calls() == [("lock_door", "porta")]
