# Axis Access Manager

Add-on di Home Assistant per gestire tessere, anagrafica e log di accesso di un controller **Axis A1210**.
L'interfaccia compare nella barra laterale di Home Assistant tramite Ingress; il controller resta la fonte
di verità per le aperture, quindi la porta funziona anche se Home Assistant è spento.

Stato: **0.3.0**: anagrafica sincronizzata con l'A1210, tessere da registrare, gruppi, fasce orarie e festività,
validità temporanea, registro con istantanee ed esportazione CSV, entità ed eventi in Home Assistant, importazione CSV,
registro delle attività e utenti in sola lettura. Dettagli in [DOCS.md](axis_access_manager/DOCS.md).

## Struttura

```
repository.yaml              repository di add-on per Home Assistant
axis_access_manager/         l'add-on
  config.yaml                opzioni, Ingress, servizio MQTT
  Dockerfile, build.yaml     immagine (Python 3.12 su base Home Assistant)
  app/
    config.py                lettura di /data/options.json e broker MQTT dal Supervisor
    vapix.py                 client VAPIX per l'A1210 (HTTPS + digest)
    events.py                ascolto degli eventi pubblicati via MQTT dall'A1210
    db.py                    database SQLite in /data (persone, tessere, regole, eventi, audit)
    service.py               logica: sincronizzazione, anagrafica, gruppo e validità, eventi, apertura porta
    rules.py                 fasce orarie e festività in iCalendar, validità delle tessere
    rules_service.py         gruppi (profili Axis), fasce orarie e festività sul controller
    ha.py                    API di Home Assistant: istantanee dalle telecamere
    ha_mqtt.py               entità ed eventi per Home Assistant via MQTT Discovery
    transfer.py              esportazione e importazione CSV
    main.py                  API e pagina servite tramite Ingress
    static/index.html        interfaccia (HTML e JavaScript senza dipendenze)
tests/                       test (pytest)
```

## Installazione in Home Assistant

1. *Impostazioni → Componenti aggiuntivi → Raccolta componenti aggiuntivi*, menu in alto a destra, *Repository*.
2. Aggiungi `https://github.com/lurisin/axis-access-manager`.
3. Installa **Axis Access Manager**, compila le opzioni (indirizzo, utente e password dell'A1210) e avvia.
4. Attiva *Mostra nella barra laterale*.

Gli aggiornamenti compaiono nella stessa pagina dopo *Controlla aggiornamenti*.

## Sviluppo

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
ruff check . && ruff format --check .
```
