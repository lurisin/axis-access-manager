export const meta = {
  name: 'axis-audit',
  description: 'Audit axis-access-manager (one area group per run): read-only finders, then skeptical verifiers on every finding',
  whenToUse: 'Code and UI-consistency audit of lurisin/axis-access-manager; pass args.group',
  phases: [
    { title: 'Find', detail: 'one read-only finder per area' },
    { title: 'Verify', detail: 'verifiers try to refute each finding, reproducing it when possible' },
  ],
}

const SCRATCH = (args && args.scratch) || '/tmp/claude-0/-home-user-axis-access-manager/a9d7407e-af26-5ce9-9e95-9f7c1abd8eb4/scratchpad/audit'
const GROUP = args && args.group
const PORT_BASE = { 'backend-api': 8800, 'backend-core': 8900, 'backend-mod1': 9000, 'backend-mod2': 9100, frontend: 9200, ui: 9300 }

const CTX = `You are auditing the repository at /home/user/axis-access-manager (add-on version 0.21.7).
What it is: a Home Assistant add-on "Axis Access Manager" that manages persons, access cards (tessere), access rules and the access log of Axis A1210 door controllers. Backend: Python 3.12, FastAPI served through Home Assistant Ingress, SQLite (app/db.py), an httpx VAPIX client to the controller (app/vapix.py), aiomqtt for controller events and for Home Assistant MQTT-discovery entities. Frontend: one file, axis_access_manager/app/static/index.html (vanilla JS, Italian UI, no dependencies). The controller is the source of truth for door openings; the app programs it (users, credentials, access profiles, schedules, doors/readers/inputs) and records events.
Code: axis_access_manager/app/*.py. Tests: tests/ (pytest; FakeController in tests/conftest.py; real controller events in tests/data/eventlogger_a1210.json).
Intended behaviour is documented in axis_access_manager/DOCS.md (user docs) and axis_access_manager/CHANGELOG.md (each release explains its decisions, many driven by field tests on real hardware: AUX input polarity, deduplication of events recovered after a restart, aggregation of repeated presses, etc.). Before calling something a bug, check it is not a deliberate, documented decision (search the CHANGELOG for the feature).
Baseline at HEAD: "ruff check ." and "ruff format --check ." are clean and "python3 -m pytest -q" passes (237 tests). Look for HIDDEN defects that tests and linters do not catch.
The code was written iteratively with an AI assistant over ~60 PRs. Typical hidden defects: stale code paths left by refactors, two code paths implementing the same rule differently, edge cases (empty lists, None, first run, upgrade from older versions, controller offline, partial failure halfway through a multi-step operation), wrong time-zone assumptions, and error handling that hides failures.`

const RUBRIC = 'critical = exploitable by a user who lacks the permission, data loss or corruption, a door that opens when it must not (or stays locked for authorised people), add-on crash or boot failure; high = wrong behaviour in a common flow, or silent failure of a core feature (sync, events, backups, restore, notifications, HA entities); medium = wrong behaviour in a realistic edge case, misleading information shown to the user, resource leak; low = minor edge case with limited impact.'

const UI_RUBRIC = 'high = clearly visible inconsistency or readability problem on a main page, or misleading text; medium = visible inconsistency in secondary places; low = minor.'

function bugRules(label) {
  return `Rules:
- The repository is READ-ONLY for you: never create, edit or delete files under /home/user/axis-access-manager and never run git commands that change state. Write experiments only under ${SCRATCH}/${label}/ and run them from the repo root, e.g.: cd /home/user/axis-access-manager && PYTHONPATH=axis_access_manager:tests python3 ${SCRATCH}/${label}/repro.py . tests/conftest.py provides FakeController; tests/test_*.py show how to build Database, parse_options settings and create_app(...) with fastapi.testclient.TestClient. If you run pytest add -p no:cacheprovider. Reproducing a defect with a small script is the strongest evidence: do it for anything you rate high or critical whenever feasible.
- Read callers and callees (other modules, and index.html for how the UI uses an endpoint) so you judge real paths, not hypothetical ones.
- Report only real defects with a concrete, realistic failure scenario (inputs/state -> wrong result). No style remarks, no refactoring wishes, no "add tests", nothing hypothetical without a path. DO report: wrong results, crashes/unhandled exceptions, data loss or corruption, security/authorisation holes, door-safety problems, silent failures, resource leaks, divergence between the app DB and the controller, misleading information shown to the user because of a logic error, performance problems with real impact.
- One finding per root cause; list other affected lines in the description.
- Severity: ${RUBRIC}
- file = repo-relative path; line = 1-indexed line of the defect in the current file (the most specific line).
- Write title, description, failure_scenario, evidence and suggested_fix IN ITALIAN (the user is Italian); keep identifiers as they are. Title max ~90 characters.
- Be exhaustive within your scope, function by function. Many findings are fine; few are fine if the code is sound. Never pad.
- coverage_notes: what you examined in depth, what you only skimmed, what you could not verify.`
}

function uiRules(label) {
  return `Rules:
- The repository is READ-ONLY for you: never create, edit or delete files under /home/user/axis-access-manager and never run git commands that change state. Scratch files only under ${SCRATCH}/${label}/.
- Report concrete, checkable inconsistencies: file + line, the current text/CSS and the proposed replacement. One finding per distinct string or rule; when the same string appears in several places, list all its line numbers in the description.
- Write title, description and expected IN ITALIAN. Title max ~90 characters.
- Severity: ${UI_RUBRIC}
- Be exhaustive: go through your sources systematically from top to bottom; do not stop after a few examples.
- coverage_notes: what you examined, what you could not check.`
}

function finder(key, kind, scope) {
  return { key, kind, prompt: `${CTX}\n\nYOUR SCOPE (${key}):\n${scope}\n\n${kind === 'ui' ? uiRules(key) : bugRules(key)}` }
}

const F = {}
F.apiA = finder('api-a', 'bug', `axis_access_manager/app/main.py lines 1-1000: Pydantic input models, current_user/request_user/need/before_change, create_app setup and the only_ingress middleware, and the routes from GET / up to /api/door-overview (status, summary, notifications, dashboard and its layout, sync, missing cards, persons, cards, unknown cards, events and snapshots, CSV export/import, audit, groups, schedules, holidays, hidden, default group, categories, align, locations, door overview); plus axis_access_manager/app/users.py. Skim the rest of main.py once for shared helpers (_now, _iso, _utc, _week_series, _current_notifications...). For every route check: (a) the permission dependency matches what the route does and what the UI expects; (b) validation in the Pydantic models (missing bounds, empty strings accepted, wrong types, {token:path} params); (c) the route calls the right service/db function with the right arguments - read the callee and check return values and exceptions (VapixError, ValueError, KeyError, sqlite errors) and how they map to HTTP status codes and messages; (d) edge cases: unknown ids (404 vs 500), empty DB, controller not configured or offline, concurrent modification; (e) correctness of computed data (dashboard 'oggi' counters, week series, summaries, door overview, CSV export rows, person import). In users.py check the role/permission model, presets, the administrator, username normalisation, persistence, permission expansion, Home Assistant users not yet in the DB, and whether a user can raise their own permissions.`)
F.apiB = finder('api-b', 'bug', `axis_access_manager/app/main.py lines 1000-2105: routes for door detail, sites/areas, installation, controllers (test, create, update, camera, identity accept, delete), doors, programming (doors, readers, inputs, access points, manage, links, undo), cameras, backups (list, create, policy, verify, compare, lock, delete, preview, restore), users; then the runtime: _start_runtime/_stop_runtime/reload_runtime (what happens when a controller is added/removed/edited while MQTT, health, sync and backup loops run), _wire (MQTT event wiring), _health_loop, the sync, prune and backup loops and the helpers at the end of the file. Review each route: permission (some routes have no dependencies=[need(...)] and must check inline - verify each one), validation, error mapping, correct use of the callee. Runtime: tasks started and cancelled correctly, an exception inside a loop (does it kill the loop forever?), reload races, state left stale after a reload, resources not released.`)
F.security = finder('security', 'bug', `Cross-cutting SECURITY review of the backend and of how the frontend renders data. (1) Authorisation: list every route in main.py with its permission requirement (dependencies=[need(...)] or inline checks for routes without it) and compare with the permission model in users.py and with what each route does - find routes that let a user act beyond their permissions (read-only roles modifying data, non-admins managing users, backups, restores or identity acceptance, a user changing their own role, anyone reading controller passwords). (2) Ingress-only access: the only_ingress middleware and allow_any_client - which client IPs are accepted (INGRESS_PROXY 172.30.32.2), how X-Remote-User-* headers are trusted, whether the API is reachable from the LAN or from other add-ons on the internal network. (3) Injection: SQL built with f-strings or format, path traversal through ids/tokens/filenames (backup ids, snapshot paths, {token:path} routes, CSV import), header injection in Content-Disposition, command execution. (4) XSS in axis_access_manager/app/static/index.html: every place where data from the server (person names, notes, card numbers, door/controller/area/site names, event fields coming from MQTT or the controller, intercom names, error messages, backup diffs, user names) is interpolated into innerHTML/template strings must be escaped with esc() - check attribute contexts and title= tooltips too, and URLs built with data. (5) CSV formula injection in exports (cells starting with = + - @). (6) Secrets: controller and MQTT passwords in API responses, logs, backups, exports, audit log. (7) Door-opening paths (MQTT buttons in ha_mqtt.py, intercom forwarding in intercom.py): who can trigger them, replay, permanent-unlock risk. (8) TLS verification and digest auth towards the controller.`)
F.db = finder('db', 'bug', `axis_access_manager/app/db.py (all), with its callers. Check: schema creation and migrations (idempotent? upgrade from every older schema version, columns added later, defaults for existing rows, migrations inside transactions?); every query: WHERE/JOIN/GROUP/ORDER correctness, NULL handling, LIKE patterns with user input (% and _ not escaped), pagination/cursor logic (ties on the same timestamp -> rows duplicated or skipped), LIMIT/OFFSET; transactions: multi-statement operations that leave partial state on exception, commit placement; foreign keys (PRAGMA foreign_keys on?) and cascades (deleting a person/card/category/controller/door leaves orphans?); concurrency: how the sqlite connection is shared between the asyncio loop, worker threads and the backup_copy hook (check_same_thread, WAL, busy_timeout); timestamps stored as text and compared as strings (mixed formats with/without offset, Z vs +00:00, microseconds) -> wrong ordering or filters; retention pruning (event_retention_days) and the snapshot files; JSON columns; uniqueness constraints vs app logic (card numbers in different case/format, leading zeros).`)
F.service = finder('service', 'bug', `axis_access_manager/app/service.py and axis_access_manager/app/events.py (all), with the vapix/db functions they call and tests/data/eventlogger_a1210.json for real event formats. service.py: synchronisation between app DB and controller (import of existing users/cards, create/update/delete person, cards add/enable/disable/delete, profile/group assignment, validity windows), partial failures (controller call fails after the DB changed or vice versa -> divergence; is there a retry/realign?), idempotency of sync, missing-cards detection with restore/forget, unknown cards, card-number normalisation, align, audit records. events.py: parsing of MQTT and event-logger events into kinds (concesso, negato, apertura_remota, uscita, citofono, allarme, manomissione, lettura...), topic sources -> door/access point/input (0.21.4), deduplication of events recovered at startup (0.21.6), aggregation of repeated presses within 10 s and tamper every 5 minutes, AUX polarity inversion and ignoring the first value (0.21.7), unknown fields, malformed payloads (do they kill the MQTT loop?), time parsing (controller local time vs UTC), snapshot triggering.`)
F.asyncTime = finder('async-time', 'bug', `Cross-cutting, all of axis_access_manager/app/*.py. (1) asyncio correctness: blocking calls on the event loop (sqlite queries, file IO, big json dumps, zip/backup creation, time.sleep, subprocess) inside async routes/loops - estimate the impact (the UI refreshes every 15 s, the health check runs every 30 s); asyncio.create_task without keeping a reference; exceptions in background tasks never retrieved; loops that die at the first exception; cancellation and shutdown (lifespan); missing locks around read-modify-write of shared state (controller programming, sync, backup and restore running concurrently with MQTT events or with each other). (2) Time: naive vs aware datetimes, datetime.now() vs datetime.now(UTC), local time vs UTC in 'oggi' counters, week series, day boundaries, retention, backup schedules ('notte', 'ogni ora se cambiato'), DST transitions (Europe/Rome) for schedules and backup times, ISO strings with different offsets compared as strings, controller time zone vs add-on TZ (apply_timezone in config.py). (3) Resources: httpx.AsyncClient lifecycle, MQTT reconnects (aiomqtt) and resubscription, file handles, unbounded in-memory structures (caches, dedupe sets, queues) that grow forever.`)
F.vapixProg = finder('vapix-prog', 'bug', `axis_access_manager/app/vapix.py and axis_access_manager/app/programming.py (all), with callers in main.py, service.py, rules_service.py and installation.py. vapix.py: HTTPS + digest auth, TLS verification policy, timeouts, retries, error parsing (HTTP errors, JSON errors, faults in the body), pagination of list calls (are all pages fetched?), token handling, encoding of names with special characters, idempotency. programming.py: programming of doors (names, times as ISO 8601 durations), readers, inputs (roles rex/intercom/door_monitor/alarm), access points, reader<->door links and direction, with history and undo: does undo restore exactly the previous state (including fields not shown in the UI), what if the controller changed in between, partial failure during multi-call operations (rollback?), duplicate tokens, deleting things in use, ISO duration parsing/formatting (PT1M30S, PT0S, P1D), validation of user input.`)
F.rules = finder('rules', 'bug', `axis_access_manager/app/rules.py and axis_access_manager/app/rules_service.py (all), with callers. rules.py: iCalendar generation/parsing of schedules (VEVENT with RRULE BYDAY, DTSTART/DTEND, ranges crossing midnight, 00:00-24:00, empty schedules), holidays (how they are excluded; all-day events), the Italian holidays computation (Easter/Pasquetta algorithm, fixed dates, years), validity windows of cards (from/to, inclusive or exclusive end, time zone, 'in scadenza' threshold). rules_service.py: categories (tipologie) -> groups (Axis access profiles) -> schedules on the controller; consistency between DB and controller after create/update/delete; deleting a group or schedule in use; hidden and default group/category; renaming; tokens with special characters (routes use {token:path}); what the controller receives for persons whose category changes; personal overrides vs category access.`)
F.backup = finder('backup', 'bug', `axis_access_manager/app/backup.py (1450 lines) and axis_access_manager/app/backup_copy.py, with callers in main.py and tests/test_backup.py for intent. Check: backup contents (DB data 'Dati Comuni' + per-controller configuration), consistency of the snapshot (DB copied while being written?), atomic writes (temp + rename), file naming and collisions (two backups in the same second), listing/parsing of backup files (a corrupted file -> the whole list fails?), verify, compare/diff correctness, lock/unlock, retention/pruning (locked backups never deleted, count vs age, retention of deleted persons), automatic schedule ('Ogni Ora Se Cambiato', 'Ogni 6 Ore Se Cambiato', 'Solo Notturno'): change detection, missed ticks after restart, time zone/DST. backup_copy.py runs as Home Assistant 'backup_pre' hook in the add-on container: it must never fail or hang (exit codes, locks, DB path, concurrent writer) and its copy must be consistent (sqlite backup API vs file copy).`)
F.restore = finder('restore', 'bug', `axis_access_manager/app/restore.py and axis_access_manager/app/full_restore.py (all), with callers in main.py and tests (tests/test_full_restore.py, tests/test_backup.py). Check: the preview ('Cosa Cambiera') equals what the restore actually does; ordering of operations (profiles/schedules before users/credentials?); partial failure halfway (what is left on the controller and in the DB, can the user retry?); idempotency of a second restore; restoring to a controller with a different identity/serial (tokens 'Axis-<serie>:...' that a controller may refuse); persons deleted after the backup ('ricrea'), modified ('riporta'), added after the backup ('togli') - data-loss risks (removing persons/cards that exist now; events/snapshots lost); mapping between backup ids and current ids; the safety backup before restoring (before_change); selective restore by part/controller; validation of backup file content (older versions, missing keys).`)
F.installHealthNotif = finder('install-health-notif', 'bug', `axis_access_manager/app/installation.py, axis_access_manager/app/health.py and axis_access_manager/app/notifications.py (all), with callers. installation.py: sites/areas/controllers/doors CRUD, validation (duplicate names/hosts, deleting a site with controllers, deleting a controller with doors/persons/events), controller identity (serial/MAC change detection, acceptance by an admin), password handling (kept when the form sends it empty? exposed in responses?), camera/intercom per door. health.py: the 30 s health check: state machine, thresholds and debounce (false alarms on a single timeout?), recovery, pause handling (_apply_pause), MQTT down vs controller down, exceptions. notifications.py: generation rules (door alarm, controller down, MQTT down, unknown card, expiring cards, denied, tamper), severity mapping, dedupe and 'seen' state per user, settings, time windows ('oggi'), counts in the top bar vs the list, notifications that never clear or clear too early.`)
F.integrations = finder('integrations', 'bug', `axis_access_manager/app/ha.py, ha_mqtt.py, intercom.py, config.py, transfer.py, __main__.py, plus axis_access_manager/Dockerfile, build.yaml, config.yaml, requirements.txt, translations/it.yaml, repository.yaml and .github/workflows/ci.yaml. ha.py: Supervisor API calls (token, timeouts, errors), camera snapshots written to /media (paths built from data? overwrite? cleanup?). ha_mqtt.py: discovery payloads (unique_id stability, device info, availability and LWT, retained config, state topics), button commands that open doors (who can publish, payload validation, logging to the access log), entity cleanup when doors/controllers are removed or renamed, topic prefix with special characters. intercom.py: Hikvision doorbell bridge - call states, the 'Apri' forwarding must be a single timed opening and never a permanent unlock, intercom -> door mapping, duplicate events. config.py: options parsing, defaults, MQTT broker from the Supervisor, timezone application. transfer.py: CSV export/import (encoding, BOM for Excel, delimiter, quotes/newlines in fields, formula injection, per-row validation errors, duplicates, partial imports, category/group names that do not exist). Packaging: config.yaml schema vs translation keys, removed options still documented, Dockerfile, version consistency (__init__.py vs config.yaml vs CHANGELOG).`)
F.jsA = finder('js-a', 'bug', `axis_access_manager/app/static/index.html lines 600-1580: utilities (esc, icon, paintIcons, morph/morphChildren/morphNode DOM diffing, relTime, toLocalInput, toast, api, retryBox, run, modal, showImage), shared state loaders, header/top bar (loadSummary, reloadIfUpdated, renderTopStatus, markTopChips, showTopPop), navigation (ROUTES, parseHash, viewNoAccess, render, setNav/initNav, the unsaved-changes guard), refreshView, dashboard (WIDGETS, DEFAULT_DASHES, WIDGET_BODY, kpi, dashData, viewDashboard, bindDashEdit drag/resize/add/remove) and doors pages (doorTile, doorGroups, viewDoorsPage, doorList, viewDoorDetail). Find JavaScript defects: wrong variable or property names (compare with the JSON returned by the matching route in main.py), missing await, unhandled promise rejections, races between the periodic refresh (every 15 s) and user actions (refresh overwriting a form being edited, morph losing input values/focus/selection, stale responses arriving after newer ones), listeners added on every refresh (duplicate handlers), dialogs that can be opened twice, keyboard/focus problems, date handling (toLocalInput/UTC, relTime with future dates), null/undefined access when the API returns empty lists or missing fields, hash routing (unknown routes, back button, pages without permission), timers not cleared when leaving a page, server data interpolated without esc().`)
F.jsB = finder('js-b', 'bug', `axis_access_manager/app/static/index.html lines 1576-2560: persons (loadPersons, viewPersons, renderPersonList with filters/search, renderMissingBanner, restoreMissing, forgetMissing, validityFields/bindValidity/readValidity, newPerson, openPerson - the person card with cards, access, category, validity, notes), unknown cards ('Da registrare': unknownList, refreshUnknown, viewUnknown), the access log ('Registro': LOG_VIEWS, logParams, logRow, eventDetail, showEvent, viewLog, setCursor/loadLog 'Carica Altri' pagination, updateHiddenCount, logRows day headers, refreshLog, doorOptionGroups, bindSnapshots), rules (weekPreview, viewRules, hidden toggle, viewCategories, viewSchedules, scheduleEditor, viewHolidays) and notifications (dayTime, notifCard, notifMatches, renderNotifCards, notifOfEvent, notifRow, loadNotifLog, refreshNotifications, viewNotifications, viewNotificationSettings). Defect classes: wrong field names vs the JSON returned by the main.py routes, missing await, races between the 15 s refresh and user actions, duplicated listeners, pagination duplicates or skips (events with the same timestamp, cursor after a filter change), day headers in the wrong time zone, validity date conversions (datetime-local <-> UTC, end of day inclusive?), search/filter edge cases (accents, case, empty), forms losing data on refresh, double submissions, server data without esc().`)
F.jsC = finder('js-c', 'bug', `axis_access_manager/app/static/index.html lines 2559-3898: installation (viewInstallation, viewControllers, healthState, identityAlert/bindIdentityAlert, areaSelect, controllerFields/bindControllerFields, controllerEditor, controllerHeader, paintControllerState, viewConnection, viewController, linksOf, controllerSchema with input roles, controllerDoors with DOOR_TIME_FIELDS, controllerReaders, controllerStatus, controllerHistory, viewSites, controllerCameras/cameraEditor, controllerIntercoms/intercomEditor, doorBody/doorEditor), settings (viewSettings), backups (aheadTime, retentionText, retentionSpan, backupStatus, backupContents, backupRow, viewBackups, viewBackupSettings, createBackup, capped, diffHtml, compareBackup, restoreItemHtml, restoreResultHtml, restoreBackup), audit (viewAudit, loadAudit), users (viewUsers), importCsv and start(). Defect classes: wrong field names vs the JSON returned by the main.py routes, missing await, races with periodic refresh, duplicated listeners, forms losing data, double submissions (restore clicked twice?), wrong payloads (e.g. password field sent empty and overwriting the stored one), ISO duration conversions for door times, number parsing, server data without esc(), confirmations missing before destructive actions.`)
F.contract = finder('contract', 'bug', `The CONTRACT between axis_access_manager/app/static/index.html and the routes in axis_access_manager/app/main.py. Build the full list of api(...) calls (and other URLs: snapshots, CSV export links, image src) in index.html - method, path, query params, JSON body - and match each to a route: wrong method/path, query parameter names the route ignores, body fields the Pydantic model does not declare (silently dropped) or required fields the UI does not send, type mismatches (string vs int, '' vs null), and response fields used by the JS that the route never returns (or returns with another name/shape). Permissions end to end: ALL_PERMS and ROUTES perms in the UI and buttons/actions shown or hidden by permission checks in JS, versus need(...)/inline checks on the backend - actions the UI offers to a role that the backend rejects (403), and pages that call an endpoint the role cannot read (errors on page load for Operatore/Consultazione/Solo Stato/custom roles). Error handling: how api() turns errors into messages - FastAPI 422 validation errors have detail as a list of objects ('[object Object]' shown?), HTTPException detail strings, network errors, 401/403/404/500, non-JSON responses (HTML error pages from Ingress, 502 while the add-on restarts). Also UI calls to routes that do not exist, and endpoints never used by the UI only if that hides a bug (a feature that cannot be reached).`)
F.visual = finder('visual', 'ui', `RENDERED UI check with a real browser.
1. Harness: write ${SCRATCH}/ui-harness/serve.py that sets os.environ['AAM_SCHEDULED_BACKUPS']='0', removes SUPERVISOR_TOKEN, puts the repo's tests and axis_access_manager directories on sys.path, imports FakeController from conftest, parse_options from app.config and create_app from app.main, builds settings like the 'settings' fixture in tests/conftest.py, creates app = create_app(settings, allow_any_client=True, controller=FakeController(), db_path=<harness dir>/ui.db) and runs uvicorn on 127.0.0.1:<port from argv, default 8765>. Seed realistic data (through the HTTP API once the server is up, and/or the Database class): about 15 persons with Italian names and cards (some disabled, some expiring soon, some expired, some with future validity), several categories (tipologie) with different colours, groups, schedules, holidays, unknown cards, events of every kind (concesso, negato, apertura_remota, uscita, citofono, allarme, manomissione, ...) over the last days including today, a site with areas, the controller with its doors, notifications, at least two backups. Study tests/test_main.py, tests/test_sites.py, tests/test_backup.py, tests/test_controller_page.py, tests/test_health.py and app/db.py to do it. Run the server in the background and stop it at the end.
2. Screenshots: write ${SCRATCH}/ui-harness/shoot.js (Node: require('playwright'); chromium.launch() needs no options). For colorScheme 'light' and 'dark' at 1440x900, visit every page in const ROUTES of index.html (hash routes), including sub-pages and tabs (settings, rules, installation and the controller pages: schema, porte, lettori, stato, storico, telecamere, videocitofoni, connessione; backups) and open the main dialogs (person card, new person, event detail, schedule editor, category editor, backup compare and restore preview, controller editor, user editor). Save full-page PNGs to ${SCRATCH}/ui-shots/<theme>-desktop-<name>.png. Also 390x844 (mobile) in both themes for: dashboard, persone, registro, porte, notifiche, impostazioni, one controller page, and the side menu open. Record console errors, page errors and failed requests per page.
3. Automatic contrast audit: in each page and theme run a script in the page that walks visible elements with direct text, computes the WCAG 2.x contrast between the computed colour and the effective background (first ancestor with a non-transparent background-color, blending alpha) and reports those under 4.5:1 (3:1 for text >= 24px, or >= 18.66px bold) with a selector path, the text, both colours and the ratio. Deduplicate by (class, colours).
4. LOOK at the screenshots (Read the PNG files: all desktop ones and the mobile ones) and note visual problems: misalignment, clipping/overflow, overlapping text, broken mobile layout, inconsistent spacing or sizes between equivalent components, the same meaning shown with different colours on different pages (same event kind, same state), elements unreadable or invisible in dark mode, inconsistent capitalisation visible on screen, inconsistent icons, empty states, raw technical values shown to users (ISO durations, tokens, English strings, undefined, null, NaN, [object Object]).
5. For each problem find the responsible source (file + line in index.html, or the backend string) and report it, mentioning the screenshot file name(s) in the description.
Keep the harness for re-runs: write ${SCRATCH}/ui-harness/README.txt with the exact commands. In coverage_notes list the pages captured and what you could not render.`)
F.capsUi = finder('caps-ui', 'ui', `CAPITALISATION of every user-visible string in axis_access_manager/app/static/index.html: the static HTML and, in the JavaScript, every template literal and string constant that reaches the screen: page titles and subtitles (ROUTES), menu items, tabs, buttons, links, section titles, field labels, column headers, placeholders, title= tooltips, aria-labels, pills/badges/status labels, KINDS labels, dashboard WIDGETS titles and descriptions, constants (SEVERITY, BACKUP_FREQ, DIRECTION_LABELS, COLORS, LOG_VIEWS, NOTIF_TABS, RESTORE_HINTS, RESTORE_MODES, INPUT_ROLE_HINTS, DOOR_TIME_FIELDS, VALIDITY_PILL, DOOR_MODES, DAYS...), toasts, modal titles and confirmation questions, empty states, error messages, hints.
Declared convention: CHANGELOG 0.5.0 says 'Etichette con le iniziali maiuscole, anche nelle entita di Home Assistant'; later entries quote UI labels in Title Case inside sentences ('Pulsante Uscita', 'Apertura Remota', 'Accessi Oggi'). Working hypothesis to VALIDATE against the actual majority practice: Title Case for labels (page titles, menu items, tabs, buttons, field labels, column headers, section titles, widget titles, pills/badges/status labels, option names); small words (articles, prepositions, conjunctions: il lo la i gli le un una, di a da in con su per tra fra, del della dei delle, al alla, nel nella, e o, se) lowercase unless first; acronyms uppercase (MQTT, CSV, REX, AUX, ID...); brand names as written by the brand (Home Assistant, Hik-Connect, Axis); sentence case for descriptions, hints, help text, toasts, confirmation questions, error messages and other full sentences; UI labels quoted inside sentences keep their Title Case.
Steps: (1) inventory the strings by element type and measure the practice (counts per type); write the convention you observe in the 'convention' field, noting where it differs from the hypothesis; (2) report EVERY deviation, one finding per distinct string (all its lines in the description), with current text, expected text and the reason: labels in sentence case where Title Case is the norm and vice versa; small words capitalised ('Se', 'Di', 'E', 'Per', 'Con') or not, inconsistently; the same label capitalised differently in different places; label references inside sentences that do not match the label's actual text; acronyms in the wrong case; full sentences in Title Case.`)
F.textsBackend = finder('texts-backend', 'ui', `User-visible TEXT produced by the Python backend and the add-on configuration: capitalisation, wording and Italian language. Sources: HTTPException(detail=...) and other error/info strings returned in JSON and shown by the UI (toasts, modals) in axis_access_manager/app/*.py; notification titles/texts (notifications.py); health descriptions (health.py); event labels/descriptions built in events.py, intercom.py, service.py; restore/backup summaries and diff labels (backup.py, restore.py, full_restore.py); audit log descriptions; CSV headers and import error messages (transfer.py); Home Assistant entity names, states and attributes (ha_mqtt.py); programming history labels (programming.py); installation/health errors; VapixError or httpx messages that can reach the UI (raw English exception texts shown to users?); plus axis_access_manager/translations/it.yaml and config.yaml (name, description, panel_title). Convention (the same as the UI): Title Case for labels and entity names (small words lowercase), sentence case for messages; correct Italian (typos, accents: perche/poiche/ne/se/piu/puo/gia/e must carry the right accents, apostrophes: un'altra, qual e without apostrophe, po'); terminology identical to the UI labels (find the label in index.html and check that the backend uses the same word and casing). Report each deviation with file + line, current text, expected text. Also report English strings or raw technical messages that reach the user.`)
F.langUi = finder('lang-ui', 'ui', `WORDING, TERMINOLOGY, ITALIAN LANGUAGE, PUNCTUATION and FORMATS of the user-visible text in axis_access_manager/app/static/index.html (capitalisation is checked by another agent: skip it unless it is part of a terminology mismatch). Find: (1) the same concept named with different words - build a glossary (tessera/badge/card/credenziale; tipologia/categoria; persona/utente/anagrafica; struttura/sito/impianto/area; controller/centralina; porta/varco; lettore; registro/log/storico; istantanea/foto/snapshot; pulsante/REX/uscita; citofono/videocitofono; negato/rifiutato; gruppo/profilo; fascia oraria; festivita; notifica/avviso/allarme ...) with occurrences, choose the dominant term and report deviations; (2) Italian errors: typos, wrong accents (perche', po', E' instead of the accented capital E, qual e), missing or wrong apostrophes, gender/number agreement, wrong prepositions, unnatural phrasing; (3) plurals with counts: every count interpolation that can read '1 persone', '1 tessere', '1 eventi'; (4) formats: dates/times (dd/mm vs d/m, 24h, oggi/ieri, relative times, weekday names), durations (raw ISO like PT7S shown?), numbers, file sizes - inconsistent across pages; (5) punctuation: final periods in hints/descriptions/toasts used inconsistently, '...' vs the ellipsis character, quote styles, dashes, middle-dot separators, spacing; (6) messages that are unclear, misleading or contradict what the code does (read the code around the message); (7) English words or technical tokens shown where an Italian label exists; (8) missing empty states or error messages (a list that silently shows nothing). One finding per distinct issue/string (all lines in the description).`)
F.colors = finder('colors', 'ui', `Visual consistency of the CSS and colour usage in axis_access_manager/app/static/index.html (CSS lines 8-574 and every class or inline style used in the JS templates). (1) Theme tokens: compare the light (:root) and dark (@media prefers-color-scheme: dark) palettes - every token defined in both? Hard-coded colours (#fff, #1b1f24, #5f6b7a, rgba(...), color-mix(...)) that bypass tokens: how do they look in the other theme? Some rules reference a data-theme attribute (:root[data-theme=...]): is it ever set by the JS? Are the token blocks consistent with it? (2) Contrast: write a Python script in your scratch dir computing WCAG 2.x ratios for every foreground/background pair actually used together (text and muted on bg/surface/surface-2; accent on accent-soft; accent-text on accent; ok/warn/err/info on their -soft and on surface; #fff on err/ok/accent in both themes; chips, pills, badges, nav .count, toast, save-bar, primary/danger buttons, disabled states, placeholders; borders of UI components vs background, 3:1). Report pairs under 4.5:1 for normal text (3:1 for large text and UI components) per theme, with the CSS line. (3) Semantic consistency: collect every mapping meaning -> colour (KINDS event types -> k-ok/k-warn/k-err/k-info/k-accent/k-muted, KIND_DOT, VALIDITY_PILL, healthState, notification severities crit/warn/low and NOTIF_ICONS, RESTORE_ACTIONS, DOOR_MODES, backup status, top chips, dashboard KPI colours, category COLORS) and find contradictions: the same meaning with different colours in different places, different meanings sharing a colour confusingly (a warning shown in info purple, success in accent blue), legends that do not match. (4) Component consistency: buttons (sizes, radii, variants, icon buttons), cards, pills, badges, dots, chips, lists/tables, form fields, dialogs: font-size scale, weights, spacing scale, border radii (count distinct values and where), icon sizes, focus-visible styles (keyboard focus visible on every interactive element?), hover and disabled states. (5) Dead or broken CSS: classes defined but never used in HTML/JS, classes used in JS templates but never defined, duplicated or contradicting rules, rules overridden later in the file. (6) Responsive: breakpoints used (list them) and inconsistencies, components that overflow at 360-390 px, tap targets under 32 px on mobile.`)
F.docs = finder('docs', 'ui', `Consistency between the documentation shown to users and the actual app. Home Assistant shows axis_access_manager/DOCS.md in the add-on Documentation tab and CHANGELOG.md in its Changelog tab; README.md is the GitHub page. Compare them with the real UI (index.html: ROUTES page names, menu labels, tabs, buttons, field labels, role names and permissions - ALL_PERMS and the role presets in users.py - dashboard widgets), the options (config.yaml + translations/it.yaml), the Home Assistant entities (names in ha_mqtt.py) and the behaviour in the code. Report: pages/buttons/options/labels named differently in docs and UI (casing included), features documented but removed or changed, features in the app missing from DOCS.md, wrong default values, wrong structure lists (README 'Struttura' vs the actual files in app/), outdated version or state text (README says 'Stato: 0.3.0' while the add-on is 0.21.7), broken links or anchors, instructions that no longer work, Italian language errors in the docs, terminology different from the UI. In the CHANGELOG only check entries from 0.15 onwards, and only for label names that no longer match the UI (older entries are history). One finding per issue with doc file + line; mention the code location in the description.`)

const GROUPS = {
  'backend-api': [F.security, F.apiB, F.apiA],
  'backend-core': [F.asyncTime, F.db, F.service],
  'backend-mod1': [F.backup, F.vapixProg, F.rules],
  'backend-mod2': [F.restore, F.installHealthNotif, F.integrations],
  frontend: [F.contract, F.jsA, F.jsB, F.jsC],
  ui: [F.visual, F.capsUi, F.langUi, F.colors, F.textsBackend, F.docs],
}

const BUG_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          title: { type: 'string' },
          file: { type: 'string' },
          line: { type: 'integer' },
          category: { type: 'string', enum: ['bug', 'security', 'concurrency', 'time', 'data-loss', 'error-handling', 'performance', 'resource-leak', 'contract', 'door-safety', 'migration', 'other'] },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          description: { type: 'string' },
          failure_scenario: { type: 'string' },
          evidence: { type: 'string' },
          suggested_fix: { type: 'string' },
        },
        required: ['title', 'file', 'line', 'category', 'severity', 'description', 'failure_scenario', 'suggested_fix'],
      },
    },
    coverage_notes: { type: 'string' },
  },
  required: ['findings', 'coverage_notes'],
}

const UI_SCHEMA = {
  type: 'object',
  properties: {
    convention: { type: 'string' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          title: { type: 'string' },
          file: { type: 'string' },
          line: { type: 'integer' },
          category: { type: 'string', enum: ['capitalization', 'terminology', 'language', 'punctuation', 'format', 'color', 'contrast', 'layout', 'typography', 'spacing', 'iconography', 'accessibility', 'docs-mismatch', 'dead-css', 'other'] },
          severity: { type: 'string', enum: ['high', 'medium', 'low'] },
          current: { type: 'string' },
          expected: { type: 'string' },
          description: { type: 'string' },
        },
        required: ['title', 'file', 'line', 'category', 'severity', 'current', 'expected', 'description'],
      },
    },
    coverage_notes: { type: 'string' },
  },
  required: ['convention', 'findings', 'coverage_notes'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          verdict: { type: 'string', enum: ['confirmed', 'plausible', 'refuted'] },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          reasoning: { type: 'string' },
          reproduced: { type: 'boolean' },
          corrected_file: { type: 'string' },
          corrected_line: { type: 'integer' },
          corrected_title: { type: 'string' },
          corrected_description: { type: 'string' },
          corrected_fix: { type: 'string' },
        },
        required: ['id', 'verdict', 'severity', 'reasoning', 'reproduced'],
      },
    },
  },
  required: ['verdicts'],
}

function chunk(arr, n) {
  const out = []
  for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n))
  return out
}

function bugVerifyPrompt(batch, label, port) {
  const items = batch.map((x) => ({ id: x.id, title: x.title, file: x.file, line: x.line, category: x.category, severity: x.severity, description: x.description, failure_scenario: x.failure_scenario, evidence: x.evidence, suggested_fix: x.suggested_fix }))
  return `${CTX}

You are an adversarial VERIFIER. Another agent reported the finding(s) below. For EACH one, try hard to REFUTE it:
1. Open the cited file and line; read the whole function plus its callers and callees (and, for API behaviour, how index.html calls the endpoint).
2. Decide whether the failure scenario can really happen in this codebase: upstream validation, DB constraints, how the UI drives the endpoint, documented intended behaviour (search CHANGELOG.md and DOCS.md for the feature), real controller behaviour described there.
3. Whenever feasible, reproduce it by running code. Scratch files only under ${SCRATCH}/${label}/; run from the repo root with PYTHONPATH=axis_access_manager:tests (FakeController is in tests/conftest.py; TestClient + create_app as in tests/test_main.py; add -p no:cacheprovider if you run pytest). For browser behaviour you may start the app with uvicorn on 127.0.0.1:${port} (a FakeController-based harness may exist in ${SCRATCH}/ui-harness/, see its README.txt) and drive it with Playwright from Node (require('playwright'); chromium.launch() needs no options). Never modify the repository; stop any server you start.
Verdicts: "confirmed" = verified on the real code path (reproduced=true only if you actually ran code that shows it); "plausible" = probably real but depends on conditions you could not check (real hardware, timing); "refuted" = not a defect (misreading, impossible path, handled elsewhere, documented intended behaviour, or too speculative to matter). Default to "refuted" when the claim rests on a misreading.
Re-assess severity with this rubric: ${RUBRIC}
If the finding is real but a detail is wrong, fill corrected_file / corrected_line / corrected_title / corrected_description / corrected_fix (in Italian). reasoning: 1-4 sentences IN ITALIAN with the decisive evidence (what you read or ran, and what happened).
Return exactly one verdict per finding id.

Findings (JSON):
${JSON.stringify(items, null, 1)}`
}

function uiVerifyPrompt(batch, convention, label) {
  const items = batch.map((x) => ({ id: x.id, title: x.title, file: x.file, line: x.line, category: x.category, severity: x.severity, current: x.current, expected: x.expected, description: x.description }))
  return `${CTX}

You are a skeptical REVIEWER of UI-consistency findings (capitalisation, wording, language, colours, layout, docs) reported by another agent. That agent observed this convention: ${convention || '(none stated)'}
For EACH finding: open the cited file/line and check that (1) the quoted current text/CSS really exists there; (2) it really deviates from the convention as actually practised in the codebase - look for counterexamples yourself: if the 'deviation' is the majority usage, or the element type is legitimately written differently (a sentence, not a label), refute it; (3) the proposed replacement is correct Italian and consistent with the convention; (4) for contrast findings recompute the ratio yourself (WCAG 2.x relative luminance; a tiny Python script) and check the colours are really used together; (5) for visual findings that cite a screenshot, open it (Read the PNG in ${SCRATCH}/ui-shots/) and confirm.
Verdicts: "confirmed" / "plausible" / "refuted" (refuted when it is not real, not visible to users, intended, or a matter of taste with no inconsistency). Re-assess severity: ${UI_RUBRIC} (never use critical). If something is wrong in the finding, fill corrected_line / corrected_title / corrected_description / corrected_fix (corrected_fix = the corrected expected text, in Italian). reasoning in Italian, 1-3 sentences. reproduced = true only if you checked it by running code or looking at a screenshot.
Never modify the repository; scratch files only under ${SCRATCH}/${label}/.
Return exactly one verdict per finding id.

Findings (JSON):
${JSON.stringify(items, null, 1)}`
}

async function verifyFinder(res, f, fi) {
  if (!res) {
    log(`${f.key}: ricerca fallita`)
    return { finder: f.key, failed: true, convention: '', coverage_notes: 'finder failed', findings: [] }
  }
  const findings = (res.findings || []).map((x, i) => ({ ...x, id: `${f.key}-${i + 1}`, finder: f.key, kind: f.kind }))
  log(`${f.key}: ${findings.length} segnalazioni da verificare`)
  if (!findings.length) return { finder: f.key, convention: res.convention || '', coverage_notes: res.coverage_notes, findings: [] }
  let batches
  if (f.kind === 'ui') {
    batches = chunk(findings, 15)
  } else {
    const isSevere = (x) => x.severity === 'critical' || x.severity === 'high'
    batches = [...findings.filter(isSevere).map((x) => [x]), ...chunk(findings.filter((x) => !isSevere(x)), 4)]
  }
  const base = (PORT_BASE[GROUP] || 9500) + fi * 15
  const sets = await parallel(batches.map((b, bi) => () => {
    const label = `verify-${f.key}-${bi + 1}`
    const prompt = f.kind === 'ui' ? uiVerifyPrompt(b, res.convention, label) : bugVerifyPrompt(b, label, base + bi)
    return agent(prompt, { label, phase: 'Verify', schema: VERDICT_SCHEMA })
  }))
  const byId = {}
  sets.forEach((s) => { if (s && s.verdicts) s.verdicts.forEach((v) => { byId[v.id] = v }) })
  const merged = findings.map((x) => {
    const v = byId[x.id]
    if (!v) return { ...x, verdict: 'unverified' }
    const out = { ...x, verdict: v.verdict, final_severity: v.severity, verify_reasoning: v.reasoning, reproduced: v.reproduced }
    if (v.corrected_file) out.file = v.corrected_file
    if (v.corrected_line) out.line = v.corrected_line
    if (v.corrected_title) out.title = v.corrected_title
    if (v.corrected_description) out.description = v.corrected_description
    if (v.corrected_fix) { if (f.kind === 'ui') out.expected = v.corrected_fix; else out.suggested_fix = v.corrected_fix }
    return out
  })
  const n = (k) => merged.filter((x) => x.verdict === k).length
  log(`${f.key}: ${n('confirmed')} confermate, ${n('plausible')} plausibili, ${n('refuted')} smentite, ${n('unverified')} non verificate`)
  return { finder: f.key, convention: res.convention || '', coverage_notes: res.coverage_notes, findings: merged }
}

const finders = GROUPS[GROUP]
if (!finders) throw new Error(`unknown group: ${GROUP}`)
log(`Gruppo ${GROUP}: ${finders.length} aree`)

const results = await pipeline(
  finders,
  (f) => agent(f.prompt, { label: `find-${f.key}`, phase: 'Find', schema: f.kind === 'ui' ? UI_SCHEMA : BUG_SCHEMA }),
  (res, f, fi) => verifyFinder(res, f, fi),
)

const done = results.filter(Boolean)
const all = done.flatMap((r) => r.findings)
return {
  group: GROUP,
  finders: done.map((r) => ({ finder: r.finder, failed: !!r.failed, convention: r.convention, coverage_notes: r.coverage_notes, total: r.findings.length })),
  findings: all.filter((x) => x.verdict !== 'refuted'),
  refuted: all.filter((x) => x.verdict === 'refuted').map((x) => ({ id: x.id, title: x.title, file: x.file, line: x.line, reason: x.verify_reasoning })),
}
