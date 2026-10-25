# Axis Access Manager

Gestione delle tessere e degli accessi per il controller Axis A1210, dentro Home Assistant.

## Opzioni

| Opzione | Descrizione |
| --- | --- |
| `a1210_host` | IP del controller, ad esempio `192.168.1.50` |
| `a1210_username`, `a1210_password` | Utente del controller con permessi di amministratore. Meglio crearne uno dedicato all'add-on |
| `a1210_verify_ssl` | Lascia disattivato con il certificato autofirmato di fabbrica |
| `door_token` | Token Axis della porta, ad esempio `porta_via_consulta_ingresso_scala` |
| `access_profile` | Gruppo (profilo Axis) predefinito per le nuove tessere, ad esempio `profilo_via_consulta_ingresso_scala_sempre` |
| `event_retention_days` | Giorni di conservazione del registro e delle istantanee (predefinito 365) |
| `mqtt_topic_prefix` | Prefisso del client MQTT dell'A1210: gli eventi arrivano su `<prefisso>/eventi/...` e lo stato su `<prefisso>/stato` |
| `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password` | Facoltative: se vuote l'add-on usa il broker Mosquitto di Home Assistant |
| `ha_integration` | Pubblica le entità in Home Assistant via MQTT Discovery (predefinito attivo) |
| `ha_door_buttons` | Aggiunge i pulsanti "Apri porta" e "Apri porta a lungo" (predefinito attivo) |
| `ha_topic_prefix` | Topic MQTT delle entità dell'add-on (predefinito `axis_access_manager`) |
| `snapshot_camera` | Facoltativa: entità `camera.` da cui salvare le istantanee |
| `snapshot_on_granted` | Salva l'istantanea anche sugli accessi concessi |
| `readonly_users` | Nomi utente di Home Assistant che possono solo consultare |
| `intercom_name` | Nome del citofono Hikvision come scritto nelle opzioni dell'add-on Hikvision Doorbell. Vuoto = citofono non collegato |
| `intercom_open_door` | Se attivo, "Apri" dall'app Hik-Connect o dal monitor apre la porta Axis (predefinito: disattivato) |
| `intercom_relay` | Relè del citofono che corrisponde al tasto Apri (1 o 2, di solito 1) |
| `intercom_mqtt_prefix` | Prefisso dei topic dell'add-on Hikvision Doorbell (predefinito `hmd`) |

## Sezioni

- **Cruscotto**: accessi e rifiuti di oggi, persone e tessere, grafico degli ultimi 7 giorni, ultimi eventi e avvisi (tessere da registrare, tessere in scadenza, allarmi, MQTT scollegato).
- **Persone**: elenco con ricerca per nome, ditta o tessera e filtri per gruppo e stato. Un clic apre la scheda della persona: gruppo e validità, tessere (aggiungi, sospendi, elimina), dati e note, ultimi passaggi.
- **Da registrare**: tessere lette dal lettore ma non registrate, da assegnare a una persona nuova o esistente senza digitare il numero.
- **Registro**: tutti gli eventi con ricerca, tipo, intervallo di date, istantanee ed esportazione CSV.
- **Regole**: gruppi, fasce orarie e festività (vedi sotto).
- **Impostazioni**: stato di controller, MQTT e Home Assistant, opzioni, esportazione e importazione CSV, registro delle attività.

In alto ci sono lo stato della porta e i pulsanti **Apri** e **Apri a lungo**. Funzionano solo a serratura bloccata, perché la serratura non regge l'alimentazione continua.

Il controller resta la fonte di verità: all'avvio e ogni 15 minuti l'add-on rilegge utenti e tessere, quindi anche quelle create in altro modo compaiono in elenco.

## Regole di accesso

- Una **fascia oraria** è un insieme di intervalli settimanali (ad esempio lun-ven 8-18 e sab 9-12). Se è attiva l'opzione "Non valida nei giorni festivi", nei giorni della scheda Festività la fascia non vale.
- Un **gruppo** (profilo di accesso Axis) collega la porta a una fascia oraria. Ogni persona appartiene a un gruppo; tutte le sue tessere lo seguono.
- I nuovi gruppi copiano la configurazione del gruppo predefinito (`access_profile`) cambiando solo la fascia oraria, così valgono sulla stessa porta.
- Il gruppo predefinito e le fasce predefinite del controller (`standard_...`) non si modificano dall'add-on.
- La **validità** (dal/al) limita una persona a un periodo: utile per ospiti e imprese. Le date sono scritte sul controller, che rifiuta la tessera fuori periodo.

## Home Assistant

Con `ha_integration` attivo compare il dispositivo **Controllo accessi** con queste entità:

- sensori: stato porta, ultimo accesso (con persona, tessera ed esito negli attributi), accessi oggi, accessi negati oggi, tessere da registrare, persone registrate;
- pulsanti: apri porta, apri porta a lungo (con `ha_door_buttons`), con gli stessi controlli di sicurezza dell'interfaccia;
- evento **Eventi accessi**, con tipi `accesso_concesso`, `accesso_negato`, `tessera_letta`, `tessera_sconosciuta`, `apertura_remota`, `allarme_porta`, `manomissione`, `citofono` e attributi `persona`, `tessera`, `dettaglio`, `origine`, `ora`.

Esempio di automazione: notifica quando una tessera sconosciuta viene passata sul lettore.

```yaml
triggers:
  - trigger: state
    entity_id: event.controllo_accessi_eventi_accessi
conditions:
  - condition: state
    entity_id: event.controllo_accessi_eventi_accessi
    attribute: event_type
    state: tessera_sconosciuta
actions:
  - action: notify.notify
    data:
      message: "Tessera sconosciuta {{ state_attr(trigger.entity_id, 'tessera') }}"
```

Il nome esatto dell'entità evento è visibile in Impostazioni → Dispositivi → Controllo accessi.

## Istantanee

Con `snapshot_camera` impostato, l'add-on chiede un'immagine alla telecamera tramite Home Assistant per accessi negati, tessere sconosciute, chiamate del citofono e allarmi (e per gli accessi concessi con `snapshot_on_granted`). Le immagini compaiono nel registro e vengono cancellate insieme agli eventi più vecchi di `event_retention_days`.

## Importazione CSV

File con separatore `;` o `,` e intestazione facoltativa: `nome; tessera; gruppo; valida_dal; valida_al; ditta; note`. Obbligatori solo nome e tessera; il gruppo si indica per nome; le date nel formato `gg/mm/aaaa` (con ora facoltativa `hh:mm`). Prima viene mostrata un'anteprima riga per riga; le tessere già registrate vengono saltate, e se il nome corrisponde a una persona esistente la tessera le viene aggiunta.

## Eventi MQTT dell'A1210

Per il registro servono gli eventi **AccessControl** (accesso concesso, negato, tessera sconosciuta). Se in Impostazioni la voce "Eventi AccessControl" resta gialla dopo il passaggio di una tessera, aggiungili al filtro eventi del client MQTT dell'A1210.

## Citofono Hikvision

L'add-on legge gli eventi del citofono dall'add-on **Hikvision Doorbell**, che li pubblica sullo stesso broker MQTT.
Non parla direttamente con il citofono.

- **Chiamate nel log**: "ha suonato", "risposta dopo N s", "nessuna risposta", "chiamata chiusa"
  (con l'istantanea, se `snapshot_camera` è impostata).
- **Apri dal citofono**: quando si preme Apri sull'app Hik-Connect o sul monitor (in futuro anche dal centralino VoIP),
  il citofono attiva il suo relè e lo segnala. L'add-on manda allora all'A1210 **una sola apertura a tempo**,
  come il pulsante Apri: solo a serratura bloccata e al massimo una ogni 5 secondi.
  Nel log compare come apertura remota con fonte "citofono".
- Le aperture fatte con tessere, PIN o volti salvati sul citofono **non** vengono inoltrate: chi può entrare lo decide solo l'A1210.
- Il relè del citofono non va collegato alla serratura.

Topic letti (con `intercom_name` = `Citofono` e relè 1):
`hmd/sensor/Citofono/Call-state/state`, `hmd/switch/Citofono/Door-1-relay/state` e `.../attributes`.
La pagina Impostazioni mostra se il citofono è collegato e l'ora dell'ultimo messaggio ricevuto.
Se il citofono usa un firmware recente senza evento di chiamata, attiva `call_state_poll` nell'add-on Hikvision Doorbell.

## Sicurezza

L'add-on non espone porte: risponde solo alle richieste che arrivano da Home Assistant tramite Ingress. Gli utenti in `readonly_users` possono consultare ma non modificare né aprire la porta. Ogni modifica è registrata con il nome dell'utente di Home Assistant. L'add-on non invia mai comandi di sblocco prolungato alla porta.
