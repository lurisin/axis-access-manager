# Axis Access Manager

Add-on di Home Assistant per gestire tessere, anagrafica e log di accesso di un controller **Axis A1210**.
L'interfaccia compare nella barra laterale di Home Assistant tramite Ingress; il controller resta la fonte
di verità per le aperture, quindi la porta funziona anche se Home Assistant è spento.

Cosa fa: anagrafica e tipologie sincronizzate con l'A1210, tessere da registrare, fasce orarie e festività,
validità temporanea, cruscotto personalizzabile, notifiche, registro con foto ed esportazione CSV, impianti con schema
lettori ↔ porte e ingressi (pulsanti, relè del citofono, contatto porta), videocitofoni Hikvision, entità ed eventi in
Home Assistant, importazione CSV, utenti con ruoli e permessi, backup con confronto, ripristino e ripristino completo
del controller. Istruzioni in [DOCS.md](axis_access_manager/DOCS.md), novità di ogni versione nel
[CHANGELOG](axis_access_manager/CHANGELOG.md).

## Struttura

```
repository.yaml              repository di add-on per Home Assistant
pyproject.toml               impostazioni di pytest e ruff
requirements-dev.txt         dipendenze per i test e ruff
.github/workflows/ci.yaml    test, ruff e controllo dell'add-on a ogni push su main e a ogni pull request
axis_access_manager/         l'add-on
  config.yaml                opzioni, Ingress, servizio MQTT, copia del database prima dei backup di Home Assistant
  translations/it.yaml       nomi e descrizioni delle opzioni nella scheda Configurazione
  DOCS.md                    documentazione (scheda Documentazione dell'add-on)
  CHANGELOG.md               novità di ogni versione
  Dockerfile, build.yaml     immagine (Python 3.12 su base Home Assistant)
  requirements.txt           dipendenze Python dell'add-on
  app/
    __main__.py              avvio del server (uvicorn)
    config.py                lettura di /data/options.json e broker MQTT dal Supervisor
    vapix.py                 client VAPIX per l'A1210 (HTTPS + digest)
    events.py                ascolto degli eventi pubblicati via MQTT dall'A1210
    db.py                    database SQLite in /data (persone, tessere, regole, eventi, audit)
    backup_copy.py           copie coerenti del database (prima dei backup di Home Assistant e degli aggiornamenti)
    service.py               logica: sincronizzazione, anagrafica, tipologia e validità, eventi, apertura porta
    rules.py                 fasce orarie e festività in iCalendar, validità delle tessere
    rules_service.py         tipologie, profili di accesso Axis, fasce orarie e festività sul controller
    installation.py          impianti: controller e porte con videocitofono e telecamera, configurati nell'app
    programming.py           programmazione del controller (porte, lettori, collegamenti, ingressi) con annullamento
    health.py                stato di ogni controller: raggiungibile, identità, orologio
    notifications.py         notifiche, guasti e allarmi
    backup.py                backup dell'app: dati comuni e configurazione dei controller, confronto, ritenzione, verifica
    restore.py               ripristino selettivo (tipologie, persone e tessere, porte e lettori)
    full_restore.py          ripristino completo di un controller resettato o sostituito
    users.py                 utenti dell'app e permessi
    intercom.py              videocitofono Hikvision tramite l'add-on Hikvision Doorbell
    ha.py                    API di Home Assistant: foto dalle telecamere e chi ha premuto i pulsanti
    ha_mqtt.py               entità ed eventi per Home Assistant via MQTT Discovery
    transfer.py              esportazione e importazione CSV
    main.py                  API e pagina servite tramite Ingress
    static/index.html        interfaccia (HTML e JavaScript senza dipendenze)
tests/                       test (pytest)
```

## Installazione in Home Assistant

1. *Impostazioni → Componenti aggiuntivi → Raccolta componenti aggiuntivi*, menu in alto a destra, *Repository*.
2. Aggiungi `https://github.com/lurisin/axis-access-manager`.
3. Installa **Axis Access Manager**, attiva *Mostra nella barra laterale* e avvialo.
4. Apri il pannello **Controllo Accessi**. In **Impianti** premi **Aggiungi Controller** e inserisci indirizzo, utente,
   password e **Prefisso MQTT** dell'A1210 (**Prova Collegamento** li verifica). Poi, nella pagina del controller, apri la
   scheda **Porte** e premi **Gestisci nell'App** sulla porta da usare.
5. Nella pagina web dell'A1210 collega il client MQTT al broker di Home Assistant con i topic del prefisso scelto:
   i passi sono nella sezione *Prima installazione* della documentazione.

Gli aggiornamenti compaiono nella stessa pagina dopo *Controlla aggiornamenti*.

## Sviluppo

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
ruff check . && ruff format --check .
```
