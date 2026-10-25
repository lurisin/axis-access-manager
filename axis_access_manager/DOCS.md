# Axis Access Manager

Gestione delle tessere e degli accessi per il controller Axis A1210, dentro Home Assistant.

## Primo avvio e aggiornamento dalla 0.3

Dalla 0.5 controller, porte, citofono e telecamera si configurano **nell'app**, nella pagina **Impianti**, non più nella scheda Configurazione dell'add-on.
Al primo avvio della 0.5 l'add-on importa da solo la configurazione che c'era nelle opzioni (indirizzo, utente e password del controller, porta, citofono, telecamera, gruppo predefinito): dopo l'aggiornamento non devi rifare niente.
Le vecchie opzioni restano visibili come "Obsoleta" e non servono più.

Il primo utente di Home Assistant che apre l'app diventa **amministratore**. Gli altri entrano con il ruolo "Solo Stato" finché un amministratore non dà loro altri permessi (Impostazioni → Utenti).
Il pannello ora è visibile anche agli utenti di Home Assistant che non sono amministratori di Home Assistant: cosa possono fare lo decidono i permessi dell'app.

## Opzioni dell'add-on

Restano solo quelle tecniche:

| Opzione | Descrizione |
| --- | --- |
| `event_retention_days` | Giorni di conservazione del registro e delle istantanee (predefinito 365) |
| `ha_integration` | Pubblica le entità in Home Assistant via MQTT Discovery (predefinito attivo) |
| `ha_topic_prefix` | Topic MQTT delle entità dell'add-on (predefinito `axis_access_manager`) |
| `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password` | Facoltative: se vuote l'add-on usa il broker Mosquitto di Home Assistant |
| `log_level` | Livello di dettaglio dei log |

## Sezioni

- **Cruscotto**: accessi e rifiuti di oggi, persone e tessere, grafico degli ultimi 7 giorni, ultimi eventi e avvisi (impianto da configurare, tessere da registrare, tessere in scadenza, allarmi, MQTT scollegato).
- **Persone**: elenco con ricerca per nome, ditta o tessera e filtri per tipologia, gruppo e stato. Un clic apre la scheda della persona: tipologia, gruppo e validità, tessere (aggiungi, sospendi, elimina), dati e note, ultimi passaggi.
- **Da Registrare**: tessere lette dal lettore ma non registrate, da assegnare a una persona nuova o esistente senza digitare il numero.
- **Registro**: tutti gli eventi con ricerca, tipo, intervallo di date, istantanee ed esportazione CSV.
- **Regole**: tipologie, gruppi, fasce orarie e festività (vedi sotto).
- **Impianti**: controller, porte con citofono e telecamera, programmazione del controller (vedi sotto).
- **Impostazioni**: stato di controller, porte, MQTT, citofoni e Home Assistant, esportazione e importazione CSV; per gli amministratori anche **Utenti** e **Attività**.

In alto c'è lo stato della porta (o delle porte). **L'app non apre la porta**: l'apertura è nei pulsanti di Home Assistant, da mettere nelle dashboard, e passa comunque dall'add-on con i suoi controlli di sicurezza.

Il controller resta la fonte di verità: all'avvio e ogni 15 minuti l'add-on rilegge utenti e tessere, quindi anche quelle create in altro modo compaiono in elenco.

## Impianti

- **Controller**: nome, indirizzo, utente, password (non viene mai mostrata, lasciala vuota per non cambiarla), prefisso MQTT e verifica del certificato. Con **Prova Collegamento** controlli i dati prima di salvarli. Salvando, l'add-on si ricollega subito senza riavvio.
- **Porte**: per ogni porta del controller scegli il nome, se avere i pulsanti Apri e Apri a Lungo in Home Assistant, la **telecamera** per le istantanee e il **videocitofono** associato.
- Per ora l'add-on gestisce un controller. Porte, citofoni, telecamere e pulsanti sono già organizzati per porta, così si potrà passare a più controller e più porte senza rifare la configurazione.

### Programmazione del controller

La scheda **Programmazione** mostra come è programmato il controller e permette di cambiarlo:

- **Porte**: nome, descrizione e tempi (apertura, apertura estesa, allarme di porta aperta troppo a lungo, preallarme);
- **Lettori**: nome e descrizione;
- **Collegamenti lettore-porta** (punti di accesso Axis): quale lettore comanda quale porta, direzione (ingresso o uscita), attivo o no.

Ogni modifica legge prima l'oggetto dal controller, cambia solo i campi scelti e salva la versione precedente nello **Storico Modifiche**: con **Annulla** la riporti com'era. Da qui non passano comandi di apertura.
La creazione da zero di porte e lettori (tipo di serratura, protocollo OSDP del lettore) resta per ora nella pagina web del controller; i dettagli tecnici letti sono visibili sotto ogni porta.

## Regole di accesso

- Una **tipologia** dice che tipo di tessera ha una persona: ad esempio Manutentore, Pulizie, Dipendente, Ospite. Ha un colore, un gruppo e una durata facoltativa. Scegliendo la tipologia, la persona riceve in automatico il gruppo (cioè gli orari in cui apre) e la durata. Se cambi il gruppo di una tipologia puoi applicarlo subito a tutte le sue persone.
- Una **fascia oraria** è un insieme di intervalli settimanali (ad esempio lun-ven 8-18 e sab 9-12). Se è attiva l'opzione "Non Valida nei Giorni Festivi", nei giorni della scheda Festività la fascia non vale.
- Un **gruppo** (profilo di accesso Axis) collega la porta a una fascia oraria. Ogni persona appartiene a un gruppo; tutte le sue tessere lo seguono.
- Tutti i gruppi e tutte le fasce si possono modificare, anche quelli predefiniti del controller. Quelli che non servono si **nascondono**: spariscono dagli elenchi e si ritrovano con "Mostra Nascosti".
- Il **gruppo predefinito** (per le nuove tessere senza tipologia) si sceglie in Regole → Gruppi con "Rendi Predefinito". I nuovi gruppi ne copiano la configurazione cambiando solo la fascia oraria, così valgono sulla stessa porta.
- La **validità** (dal/al) limita una persona a un periodo: utile per ospiti e imprese. Le date sono scritte sul controller, che rifiuta la tessera fuori periodo.

## Utenti e permessi

Gli utenti sono quelli di Home Assistant, riconosciuti dall'app. In Impostazioni → Utenti l'amministratore assegna un ruolo o sceglie i permessi a spunta:

| Ruolo | Cosa può fare |
| --- | --- |
| Amministratore | Tutto, compresi impianti, utenti e registro delle attività |
| Operatore | Persone e tessere (anche modificarle), registro, vedere le regole, esportare e importare |
| Consultazione | Vedere persone, registro e regole senza modificare |
| Solo Stato | Cruscotto e stato del controller |

Permessi singoli: vedere persone, modificare persone e tessere, registro, vedere regole, modificare regole, esportare e importare, impianti, utenti. Deve restare sempre almeno un amministratore.

## Home Assistant

Con `ha_integration` attivo compare il dispositivo **Controllo Accessi** con queste entità:

- sensori: Stato Porta, Ultimo Accesso (con persona, tessera ed esito negli attributi), Accessi Oggi, Accessi Negati Oggi, Tessere da Registrare, Persone Registrate;
- pulsanti **Apri Porta** e **Apri Porta a Lungo** per le porte che li hanno attivi in Impianti: aprono solo a serratura bloccata e solo con un'apertura a tempo;
- per ogni porta oltre la prima: Stato, Apri e Apri a Lungo con il nome della porta (ad esempio "Apri Cantina");
- evento **Eventi Accessi**, con tipi `accesso_concesso`, `accesso_negato`, `tessera_letta`, `tessera_sconosciuta`, `apertura_remota`, `allarme_porta`, `manomissione`, `citofono` e attributi `persona`, `tessera`, `dettaglio`, `origine`, `ora`.

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

Il nome esatto dell'entità evento è visibile in Impostazioni → Dispositivi → Controllo Accessi. Gli identificativi delle entità restano quelli delle versioni precedenti: cambia solo il nome mostrato.

## Istantanee

Con una telecamera impostata sulla porta (Impianti), l'add-on chiede un'immagine alla telecamera tramite Home Assistant per accessi negati, tessere sconosciute, chiamate del citofono e allarmi di quella porta (e per gli accessi concessi, se attivato). Le immagini compaiono nel registro e vengono cancellate insieme agli eventi più vecchi di `event_retention_days`.

## Importazione CSV

File con separatore `;` o `,` e intestazione facoltativa: `nome; tessera; gruppo; valida_dal; valida_al; ditta; note; tipologia`. Obbligatori solo nome e tessera; gruppo e tipologia si indicano per nome (la tipologia assegna il suo gruppo se il gruppo è vuoto); le date nel formato `gg/mm/aaaa` (con ora facoltativa `hh:mm`). Prima viene mostrata un'anteprima riga per riga; le tessere già registrate vengono saltate, e se il nome corrisponde a una persona esistente la tessera le viene aggiunta.

## Eventi MQTT dell'A1210

Per il registro servono gli eventi **AccessControl** (accesso concesso, negato, tessera sconosciuta). Se in Impostazioni la voce "Eventi AccessControl" resta gialla dopo il passaggio di una tessera, aggiungili al filtro eventi del client MQTT dell'A1210.

## Citofono Hikvision

Il citofono si associa a una porta in **Impianti**. L'add-on legge i suoi eventi dall'add-on **Hikvision Doorbell**, che li pubblica sullo stesso broker MQTT.
Non parla direttamente con il citofono.

- **Chiamate nel log**: "ha suonato", "risposta dopo N s", "nessuna risposta", "chiamata chiusa"
  (con l'istantanea, se la porta ha una telecamera).
- **Apri dal citofono**: quando si preme Apri sull'app Hik-Connect o sul monitor (in futuro anche dal centralino VoIP),
  il citofono attiva il suo relè e lo segnala. L'add-on manda allora all'A1210 **una sola apertura a tempo**,
  sulla porta associata: solo a serratura bloccata e al massimo una ogni 5 secondi.
  Nel log compare come apertura remota con fonte "citofono".
- Le aperture fatte con tessere, PIN o volti salvati sul citofono **non** vengono inoltrate: chi può entrare lo decide solo l'A1210.
- Il relè del citofono non va collegato alla serratura.

Topic letti (con nome del citofono `Citofono` e relè 1):
`hmd/sensor/Citofono/Call-state/state`, `hmd/switch/Citofono/Door-1-relay/state` e `.../attributes`.
La pagina Impostazioni mostra lo stato della chiamata e l'ora dell'ultimo messaggio ricevuto da ogni citofono.
Al primo collegamento il citofono può scaricare lo storico degli eventi e bloccarsi fino a un riavvio: tienine conto nella prima prova.
Se il citofono usa un firmware recente senza evento di chiamata, attiva `call_state_poll` nell'add-on Hikvision Doorbell.

## Sicurezza

L'add-on non espone porte: risponde solo alle richieste che arrivano da Home Assistant tramite Ingress, e ogni richiesta è controllata con i permessi dell'utente. La password del controller resta nel database dell'add-on e non viene mai rimandata alla pagina. Ogni modifica è registrata con il nome dell'utente di Home Assistant. L'add-on non invia mai comandi di sblocco prolungato alla porta.
