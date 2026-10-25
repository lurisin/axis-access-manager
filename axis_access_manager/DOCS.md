# Axis Access Manager

## Opzioni

| Opzione | Descrizione |
| --- | --- |
| `a1210_host` | IP del controller, ad esempio `192.168.1.50` |
| `a1210_username`, `a1210_password` | Utente del controller con permessi di amministratore. Meglio crearne uno dedicato all'add-on |
| `a1210_verify_ssl` | Lascia disattivato con il certificato autofirmato di fabbrica |
| `door_token` | Token Axis della porta, ad esempio `porta_via_consulta_ingresso_scala` |
| `access_profile` | Profilo Axis assegnato alle nuove tessere, ad esempio `profilo_via_consulta_ingresso_scala_sempre` |
| `event_retention_days` | Giorni di conservazione del log (predefinito 365) |
| `mqtt_topic_prefix` | Prefisso del client MQTT dell'A1210: gli eventi arrivano su `<prefisso>/eventi/...` e lo stato su `<prefisso>/stato` |
| `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password` | Facoltative: se vuote l'add-on usa il broker Mosquitto di Home Assistant |

## Schede

- **Persone**: elenco con le tessere di ciascuno. Da qui si registra una persona nuova, si aggiunge una tessera, si rinomina, si sospende o riattiva una tessera e si elimina. Ogni modifica viene scritta subito sul controller.
- **Da registrare**: tessere lette dal lettore ma non registrate. Si assegnano a una persona nuova o esistente senza digitare il numero.
- **Log**: accessi concessi e negati (con il motivo), letture, aperture dall'app con il nome dell'utente di Home Assistant, allarmi e manomissioni.
- **Stato**: collegamento al controller e a MQTT.

In alto ci sono lo stato della porta e i pulsanti **Apri** e **Apri a lungo**. Funzionano solo a serratura bloccata, perché la serratura non regge l'alimentazione continua.

Il controller resta la fonte di verità: all'avvio e ogni 15 minuti l'add-on rilegge utenti e tessere, quindi anche quelle create in altro modo (ad esempio dagli script di Home Assistant) compaiono in elenco.

## Pagina di stato

La pagina nella barra laterale mostra:

- **Controller**: modello, firmware, porte e numero di tessere registrate, letti via VAPIX.
- **MQTT**: collegamento al broker, stato online del controller, ultima tessera letta.
- **Eventi ricevuti**: per ogni categoria (AccessControl, Door, IdPoint, Device) quanti eventi sono arrivati dall'avvio.

Per il log degli accessi servono gli eventi **AccessControl** (accesso concesso, negato, tessera sconosciuta).
Se dopo il passaggio di una tessera la categoria non compare, aggiungili al filtro eventi del client MQTT
dell'A1210.

## Sicurezza

L'add-on non espone porte: risponde solo alle richieste che arrivano da Home Assistant tramite Ingress.
Non invia mai comandi di sblocco prolungato alla porta.
