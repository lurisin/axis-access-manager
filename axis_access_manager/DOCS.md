# Axis Access Manager

## Opzioni

| Opzione | Descrizione |
| --- | --- |
| `a1210_host` | IP del controller, ad esempio `192.168.1.50` |
| `a1210_username`, `a1210_password` | Utente del controller con permessi di amministratore. Meglio crearne uno dedicato all'add-on |
| `a1210_verify_ssl` | Lascia disattivato con il certificato autofirmato di fabbrica |
| `door_token` | Token Axis della porta, ad esempio `porta_via_consulta_ingresso_scala` |
| `mqtt_topic_prefix` | Prefisso del client MQTT dell'A1210: gli eventi arrivano su `<prefisso>/eventi/...` e lo stato su `<prefisso>/stato` |
| `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password` | Facoltative: se vuote l'add-on usa il broker Mosquitto di Home Assistant |

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
