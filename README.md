# Axis Access Manager

Add-on di Home Assistant per gestire tessere, anagrafica e log di accesso di un controller **Axis A1210**.
L'interfaccia compare nella barra laterale di Home Assistant tramite Ingress; il controller resta la fonte
di verità per le aperture, quindi la porta funziona anche se Home Assistant è spento.

Stato: **Fase 0**, scheletro dell'add-on con pagina di stato (controller, MQTT, eventi ricevuti).

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
    main.py                  API e pagina servite tramite Ingress
    static/index.html        pagina di stato
tests/                       test (pytest)
```

## Installazione in Home Assistant

Il repository è privato, quindi il modo più semplice è installarlo come **add-on locale**:

1. Installa l'add-on *Samba share* o *Advanced SSH & Web Terminal*.
2. Copia la cartella `axis_access_manager` dentro la cartella `addons` di Home Assistant
   (`/addons/axis_access_manager`).
3. Vai in *Impostazioni → Componenti aggiuntivi → Raccolta componenti aggiuntivi*, menu in alto a destra,
   *Controlla aggiornamenti*: l'add-on compare sotto *Componenti aggiuntivi locali*.
4. Installa, compila le opzioni (indirizzo, utente e password dell'A1210) e avvia.
5. Attiva *Mostra nella barra laterale*.

Se in futuro il repository diventa pubblico basterà aggiungere `https://github.com/lurisin/axis-access-manager`
come repository nella raccolta componenti aggiuntivi.

## Sviluppo

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
ruff check . && ruff format --check .
```
