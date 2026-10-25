# Axis Access Manager

Gestione delle tessere e degli accessi per il controller Axis A1210, dentro Home Assistant.

## Primo avvio e aggiornamento dalla 0.3

Dalla 0.5 controller, porte, citofono e telecamera si configurano **nell'app**, nella pagina **Impianti**, non più nella scheda Configurazione dell'add-on.
Al primo avvio della 0.5 l'add-on importa da solo la configurazione che c'era nelle opzioni (indirizzo, utente e password del controller, porta, citofono, telecamera, gruppo predefinito): dopo l'aggiornamento non devi rifare niente.
Dalla 0.8 le vecchie opzioni "Obsoleta" non ci sono più: chi aggiorna da una versione precedente alla 0.5 passi prima dalla 0.7.

Il primo utente di Home Assistant che apre l'app diventa **amministratore**. Gli altri entrano con il ruolo "Solo Stato" finché un amministratore non dà loro altri permessi (Impostazioni → Utenti).
Il pannello ora è visibile anche agli utenti di Home Assistant che non sono amministratori di Home Assistant: cosa possono fare lo decidono i permessi dell'app.

### Novità della 0.10

- **Gruppi e tipologie diventano una cosa sola: la tipologia**, in Persone → Tipologie. Ogni tipologia dice chi è la persona e, con le righe **Dove e Quando**, quali porte, aree o strutture apre e in quale fascia oraria. In Regole restano solo Fasce Orarie e Festività.
- Al primo avvio i gruppi passano da soli alle tipologie, **senza cambiare chi entra dove**:
  - una tipologia prende il gruppo che usava già;
  - chi non aveva una tipologia entra in una tipologia con il nome del suo gruppo;
  - il gruppo predefinito diventa la tipologia predefinita.
- Una persona con un accesso diverso da quello della sua tipologia è segnalata come **Accesso Personalizzato**, con il tasto per allinearla.

### Novità della 0.8

- In Home Assistant arrivano anche lo stato del controller (**Controller Online**, **Coperchio Controller Aperto**, **Ultimo Avvio Controller**), gli **ingressi I/O** del controller e, per ogni porta, **Allarme**, **Preallarme** e **Manomissione** come sensori acceso/spento: tutto quello che serviva il vecchio package YAML.
- Le vecchie opzioni "Obsoleta" (indirizzo, utente e password dell'A1210, porta, citofono...) sono sparite dalla scheda Configurazione dell'add-on.

### Novità della 0.7

- **Notifiche**: in fondo al menu c'è la campanella con il numero delle notifiche da gestire. Lo stato della porta non è più in alto: si vede nel dettaglio di ogni porta.
- **Impianti** mostra i controller come schede per struttura e area. Toccando la scheda si apre la pagina del controller con lo **schema lettori ↔ porte**; l'ingranaggio apre la pagina **Connessione** (indirizzo, credenziali, area, descrizione della posizione).
- L'area ora è del **controller**: porte e lettori la ereditano. Al primo avvio della 0.7 l'area che avevi dato alla porta passa al suo controller.
- **Porte** è un elenco essenziale (nome, area, stato): il clic apre il dettaglio, anche con una porta sola.

### Novità della 0.6

- Il menu è una **barra laterale** sempre presente con le sole icone; il pulsante ☰ la allarga con i nomi delle pagine. Sul telefono si apre sopra la pagina e si richiude da sola dopo la scelta; sul computer resta come l'hai lasciata.
- Le porte stanno in **strutture e aree** (ad esempio Teatro › Accesso Scala, Hotel › Ingresso, Villa 3 › Cancello). Al primo avvio la porta già configurata finisce in "Struttura Principale › Generale": rinominale in Impianti → Strutture e Aree.
- Con una sola porta l'app resta semplice: colonne e filtri per porta compaiono solo quando le porte sono più di una.

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

- **Barra in alto**: in ogni pagina compaiono **Guasti** (controller, MQTT, telecamere) e **Allarmi** (porte e manomissioni non ancora viste) solo quando ci sono, altrimenti **Sistema OK**. Un clic apre l'elenco. Se l'add-on non risponde compare **Add-on Non Raggiungibile**. Nel cruscotto, accanto al titolo, ci sono la data, l'ora e la matita per modificarlo.
- **Cruscotto**: riquadri che ogni utente sceglie e dispone come vuole.
  - Quelli disponibili sono: Da Gestire, Accessi, Negati e Allarmi di oggi, Persone, Porte, Ultimi 7 Giorni, Ultimi Eventi, Tessere in Scadenza, Tessere da Registrare e Stato Impianto.
  - Con la matita (**Modifica Cruscotto**) li sposti trascinandoli, li togli o li aggiungi. La maniglia nell'angolo in basso a destra cambia larghezza (a quarti di riga) e altezza (a righe da 64 px). I riquadri piccoli riempiono gli spazi vuoti e, se il contenuto non ci sta, scorre dentro il riquadro.
  - **Ripristina Predefinito** torna al cruscotto predefinito, che dipende dai permessi: amministratore (tessere da sistemare, numeri del giorno, andamento, registro e cose da gestire), operatore che modifica le tessere (tessere da registrare e da gestire in alto), consultazione con il registro (numeri del giorno e registro in grande), solo stato (porte e stato dell'impianto).
  - I riquadri che richiedono un permesso (registro, persone) compaiono solo a chi ce l'ha.
- **Porte**: elenco essenziale per struttura e area, con nome, accessi di oggi e stato. Il clic apre il dettaglio: stato, numeri di oggi, dispositivi (lettori con direzione, citofono, telecamera, controller), le tipologie che la aprono e i suoi ultimi eventi.
- **Persone**: elenco essenziale in ordine alfabetico, con il colore della tipologia e un'etichetta solo se c'è qualcosa da sistemare (tessera sospesa, scaduta, in scadenza, accesso personalizzato). Filtri rapidi per tipologia, ricerca per nome, ditta o tessera, filtro per stato. Un clic apre la scheda della persona: tipologia con le sue porte e fasce, validità, tessere (aggiungi, sospendi, elimina), dati e note, ultimi passaggi. La scheda **Tipologie** gestisce le tipologie (vedi Regole di accesso).
- **Da Registrare**: tessere lette dal lettore ma non registrate, da assegnare a una persona nuova o esistente senza digitare il numero.
- **Registro**: una riga per evento (ora, esito, chi, porta) e i dettagli al clic, a lato sul computer e in una finestra sul telefono. Le viste **Da Controllare** (rifiuti, tessere sconosciute, allarmi), Solo Negati, Solo Accessi, Allarmi e Tutto servono quando il passaggio è tanto: nelle viste filtrate un contatore dice quanti accessi riusciti non sono mostrati. Poi ricerca, porta, intervallo di date, istantanee ed esportazione CSV. La vista scelta viene ricordata.
- **Notifiche**: in alto **Da Gestire**, ordinate per gravità.
  - Critica: allarmi, manomissioni, controller o MQTT non raggiungibili, tessera sconosciuta ripetuta.
  - Media: tentativi ripetuti di una persona, tessera sconosciuta passata una volta.
  - Bassa: singolo accesso negato, tessera in scadenza.
  - Gli eventi uguali (stessa persona o tessera, stessa porta, stesso motivo, a meno di 5 minuti l'uno dall'altro) diventano una sola notifica con il numero dei tentativi.
  - Ogni notifica ha la foto della porta, Dettagli e Presa Visione. Un nuovo tentativo la rimette da gestire.
  - Sotto c'è il **Registro Eventi** delle ultime 24 ore, con le schede per categoria e lo stato Da Gestire o Visto di ogni evento.
  - Allarmi ed eventi restano per 24 ore. I problemi di collegamento restano finché non si risolvono, e se tornano si ripresentano.
  - Quali tipi sono attivi lo sceglie l'amministratore in Impostazioni → Notifiche.
- **Regole**: fasce orarie e festività (vedi sotto).
- **Impianti**: controller, schema lettori ↔ porte, porte con citofono e telecamera, strutture e aree, programmazione del controller e backup (vedi sotto).
- **Impostazioni**: solo l'applicazione, in fondo al menu insieme a Notifiche e all'utente collegato (che per l'amministratore apre la gestione degli utenti). Generale (Home Assistant, opzioni dell'add-on, esportazione e importazione CSV) e, per gli amministratori, **Notifiche**, **Utenti** e **Attività**. Lo stato di controller, porte, eventi MQTT e citofoni è nella scheda Stato del controller.

**L'app non apre la porta**: l'apertura è nei pulsanti di Home Assistant, da mettere nelle dashboard, e passa comunque dall'add-on con i suoi controlli di sicurezza.

Il controller resta la fonte di verità: all'avvio e ogni 15 minuti l'add-on rilegge utenti e tessere, quindi anche quelle create in altro modo compaiono in elenco.

## Impianti

La pagina mostra i **controller** come schede, raggruppati per struttura e area, con modello, numero di porte e lettori e stato del collegamento.

- **Ingranaggio → Connessione**: nome, struttura e area, descrizione della posizione (es. "quadro sotto la scala"), indirizzo, utente, password (non viene mai mostrata, lasciala vuota per non cambiarla), prefisso MQTT e verifica del certificato. Con **Prova Collegamento** controlli i dati prima di salvarli. Salvando, l'add-on si ricollega subito senza riavvio.
- **Scheda del controller**: la configurazione di lettori e porte, nelle schede Schema, Porte, Lettori, Telecamere, Videocitofoni, Stato, Storico Modifiche e Backup (vedi sotto). **Stato** mostra se il controller risponde (modello, firmware, tessere, ultima sincronizzazione), lo stato delle sue porte, gli eventi MQTT ricevuti e i citofoni; funziona anche quando il controller non risponde.
- **Strutture e Aree**: dove si trovano i controller. Crei strutture (Teatro Eliseo, Hotel Giulia, Villa 1…) e al loro interno aree (Via Consulta, Primo Piano…); poi nella Connessione del controller scegli la sua area. Porte e lettori del controller la ereditano.
- **Backup**: salvataggio, confronto e ripristino dei dati dell'app e della configurazione dei controller (vedi Backup dell'app). Ogni controller ha anche la sua scheda Backup con lo storico della sola configurazione.
- Per ora l'add-on gestisce un controller. Pagine e dati sono già organizzati per averne di più.

### Schema lettori ↔ porte

A sinistra i lettori, a destra le porte del controller, uniti da un filo per ogni collegamento. **Tocca un lettore e poi una porta** per collegarli. L'**ingranaggio sul filo** apre direzione (ingresso o uscita), Collegamento Attivo e Scollega.

Le modifiche restano in sospeso, con il filo arancione tratteggiato, finché non premi **Salva**; Scarta le annulla. Se lasci la pagina con modifiche non salvate l'app chiede se salvarle o scartarle.

Sull'Axis un collegamento è un lettore dentro un **punto di accesso** della porta, che ha la sua direzione: lo schema sposta i lettori tra i punti di accesso già presenti sul controller, senza crearne di nuovi. Nel menu Direzione compaiono solo le direzioni che la porta ha già sul controller (sull'A1210 di prova c'è solo l'ingresso). La creazione di nuovi punti di accesso dall'app arriverà in una versione successiva: la pagina web dell'A1210 non la permette. Spegnere un collegamento spegne il suo punto di accesso, quindi vale per tutti i lettori che lo usano (l'app lo dice).

### Ingressi del controller

Sotto i lettori, nello stesso schema, ci sono gli **ingressi fisici** del controller:
- il connettore Door ha due ingressi: pin 1-2 contatto porta, pin 3-4 REX;
- i due AUX (Aux pin 3 e 4);
- gli ingressi Manomissione e Allarme del connettore External.

Il pallino accanto al nome mostra lo stato in tempo reale degli ingressi che lo mandano (AUX, Manomissione e Allarme).

**Tocca un ingresso e poi una porta** per collegarlo, oppure tocca due volte l'ingresso per aprirne la scheda:
- **Nome**: è anche il nome dell'entità in Home Assistant, che mantiene il suo identificativo.
- **Ruolo**:
  - **Pulsante Uscita**: chiede l'apertura della porta e sblocca la serratura, ad esempio dal pulsante della portineria. Più pulsanti in parallelo sulla stessa linea vanno bene.
  - **Relè Citofono**: il contatto del relè del citofono. Apre la porta anche con Home Assistant spento.
  - **Ingresso di Allarme**: solo stato in Home Assistant.
  - **Non Usato**.
- **Porta**: per Pulsante Uscita e Relè Citofono.
- **Contatto**: Normalmente Aperto o Normalmente Chiuso.
- **Linea Supervisionata**: con le resistenze di fine linea il controller riconosce un cavo tagliato o in corto e non lo scambia per una pressione. È consigliata per il citofono esterno.

Con **Scrivi sul Controller**, per Pulsante Uscita e Relè Citofono l'app crea sul controller:
- un punto di identificazione senza lettore, con il tipo di contatto scelto;
- l'assegnazione dell'ingresso fisico a quel punto;
- un punto di accesso in uscita sulla porta, con il profilo REXOnly di fabbrica.

Togliendo il ruolo, l'app li rimuove. Ogni modifica compare nello Storico Modifiche e si può annullare. Lo schema dei lettori non mostra questi punti e non li tocca.

Nel registro l'apertura compare con il nome dato all'ingresso, ad esempio "Pulsante Portineria", e nel dettaglio il ruolo e il morsetto, ad esempio "Pulsante Uscita · Aux 1". Il nome resta quello che aveva al momento dell'apertura, anche se poi lo cambi.

Da sapere:
- Sull'A1210 un ingresso, una volta assegnato, non torna mai "mai usato": togliendo il ruolo resta un ingresso semplice libero.
- Dopo aver tolto la linea supervisionata, l'ingresso può risultare attivo finché non riavvii il controller.
- Il ruolo **Sensore Porta** sul contatto porta arriverà dopo una prova al banco.

### Porte, lettori e storico

- **Porte**: per ogni porta del controller le impostazioni dell'app (nome, pulsanti Apri e Apri a Lungo in Home Assistant) e, con **Nome e Tempi**, nome, descrizione e tempi sul controller (apertura, apertura estesa, allarme di porta aperta troppo a lungo, preallarme). L'apertura arriva al massimo a 30 secondi e quella estesa a 60, per non tenere alimentata a lungo la serratura. Una porta del controller non ancora usata dall'app si aggiunge con **Gestisci nell'App**.
- **Telecamere** e **Videocitofoni**: schede del controller con le telecamere di Home Assistant per le istantanee e i videocitofoni Hikvision. Per ognuno scegli la porta; da qui li aggiungi, li sposti su un'altra porta o li togli. Una telecamera può inquadrare anche il coperchio del controller, se il controller è in un altro locale: la sua apertura viene fotografata da lì.
- **Istantanee**: con una telecamera sulla porta l'app salva una foto su accessi negati, aperture a distanza (Home Assistant e citofono), chiamate al citofono e allarmi; con "Istantanea Anche sugli Accessi Concessi" anche a ogni tessera accettata. Una foto per passaggio, scattata sull'esito (accesso negato o concesso), non sulla lettura della tessera. Le foto si vedono nel registro e in Home Assistant in **Media → axis_access_manager** (cartella `/media/axis_access_manager`, raggiungibile anche con File editor o Samba); il nome del file è numero evento, data e ora dell'evento. Si cancellano insieme al registro dopo `event_retention_days` giorni e non entrano nel backup dell'add-on.
- **Lettori**: nome e descrizione sul controller, e quali porte comandano.
- **Storico Modifiche**: ogni scrittura legge prima l'oggetto dal controller, cambia solo i campi scelti e salva la versione precedente; con **Annulla** la riporti com'era. Da qui non passano comandi di apertura.

La creazione da zero di porte e lettori (tipo di serratura, protocollo OSDP del lettore) resta per ora nella pagina web del controller; i dettagli tecnici letti sono visibili sotto ogni porta.

## Regole di accesso

- Una **tipologia** dice chi è la persona (Manutentore, Pulizie, Dipendente, Ospite...) e decide dove e quando apre la sua tessera. Si gestisce in **Persone → Tipologie** e ha:
  - un colore e una descrizione;
  - le righe **Dove e Quando**, ognuna con una porta, un'area o un'intera struttura e la sua fascia oraria, ad esempio Pulizie → Via Consulta › Camere → 07–15;
  - una durata facoltativa della tessera, in giorni dalla registrazione (ad esempio 1 per gli ospiti).
- Un'area o una struttura vale per **tutte le sue porte, anche quelle aggiunte dopo**: quando una porta entra in un'area, le tipologie che la comprendono si aggiornano da sole. Una struttura comprende tutte le sue aree: per tenere fuori i locali tecnici mettili in un'area a parte e scegli le singole aree.
- Per ogni tipologia l'app scrive e tiene aggiornato un **profilo di accesso** sul controller, con una regola per ogni lettore delle porte coperte. Pulsanti di uscita e relè del citofono aprono senza tessera e non ne hanno bisogno. Con fasce diverse per porta, la fascia del profilo è "Sempre", perché l'A1210 controlla sia quella del profilo sia quella della regola.
- Scegliendo la tipologia di una persona, tutte le sue tessere ricevono l'accesso e la durata della tipologia. Modificando una tipologia, l'accesso cambia subito per tutte le sue persone.
- **Accesso Personalizzato**: una persona le cui tessere usano un profilo diverso da quello della sua tipologia (rimasto dalle versioni precedenti, o scelto dall'importazione CSV). Ha un'etichetta nell'elenco e il tasto **Allinea alla Tipologia** nella scheda; salvando l'accesso della persona torna quello della tipologia.
- La **tipologia predefinita** è proposta per le nuove tessere; si cambia con "Rendi Predefinita". Una tipologia con persone, o che è la predefinita, non si elimina. Una porta, un'area o una struttura usata in una riga non si elimina: toglila prima dalle righe.
- Una **fascia oraria** è un insieme di intervalli settimanali (ad esempio lun-ven 8-18 e sab 9-12). Se è attiva l'opzione "Non Valida nei Giorni Festivi", nei giorni della scheda Festività la fascia non vale. Tutte le fasce si possono modificare, anche quelle predefinite del controller; quelle che non servono si **nascondono**.
- La **validità** (dal/al) limita una persona a un periodo: utile per ospiti e imprese. Le date sono scritte sul controller, che rifiuta la tessera fuori periodo.

## Utenti e permessi

Gli utenti sono quelli di Home Assistant, riconosciuti dall'app. In Impostazioni → Utenti l'amministratore assegna un ruolo o sceglie i permessi a spunta:

| Ruolo | Cosa può fare |
| --- | --- |
| Amministratore | Tutto, compresi impianti, utenti e registro delle attività |
| Operatore | Stato, persone e tessere (anche modificarle), registro, vedere le fasce orarie, esportare e importare |
| Tessere | Solo anagrafica, tipologie e fasce orarie: registrare e modificare persone e tessere, senza registro né stato |
| Consultazione | Vedere stato, persone, registro e fasce orarie senza modificare |
| Solo Registro | Solo il registro accessi |
| Solo Stato | Cruscotto, porte e notifiche |
| Nessun Accesso | Nessuna sezione: l'utente vede solo un avviso |

Permessi singoli: stato (cruscotto, porte e notifiche), vedere persone e tipologie, modificare persone e tessere, registro, vedere fasce orarie e festività, modificare tipologie, fasce e festività, esportare e importare, impianti, backup (vedere, creare e confrontare; ripristinare ed eliminare restano agli amministratori), utenti.

Ogni permesso mostra le sue sezioni: togliendo la spunta la sezione sparisce dal menu e il server rifiuta i suoi dati. Impostazioni compare a chi può esportare e importare, configurare gli impianti o gestire gli utenti. Dopo l'accesso l'utente arriva sulla prima sezione che gli è permessa: con il solo registro entra direttamente nel Registro. Deve restare sempre almeno un amministratore.

## Home Assistant

Con `ha_integration` attivo compare il dispositivo **Controllo Accessi** con queste entità:

- sensori: Stato Porta, Ultimo Accesso (con persona, tessera ed esito negli attributi), Accessi Oggi, Accessi Negati Oggi, Tessere da Registrare, Persone Registrate;
- per ogni porta, sensori acceso/spento **Allarme Porta** (porta forzata o aperta troppo a lungo), **Preallarme Porta** e **Manomissione Porta**, con negli attributi il tipo e l'ora dell'ultimo cambio;
- controller: **Controller Online**, **Coperchio Controller Aperto**, **Ultimo Avvio Controller** e gli ingressi **Ingresso AUX 1**, **Ingresso AUX 2**, **Ingresso Manomissione**, **Ingresso Allarme** (porte I/O 0-3);
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

Nel registro le aperture dai pulsanti mostrano chi ha premuto, ad esempio "Luca (Home Assistant)": l'add-on legge l'utente dallo stato del pulsante e il nome dalla **persona** di Home Assistant collegata a quell'utente (Impostazioni → Persone). Se l'utente non ha una persona compare solo "Home Assistant"; le aperture da automazioni compaiono come "Automazione (Home Assistant)".

## Istantanee

Con una telecamera impostata sulla porta (Impianti), l'add-on chiede un'immagine alla telecamera tramite Home Assistant per accessi negati, tessere sconosciute, chiamate del citofono e allarmi di quella porta (e per gli accessi concessi, se attivato). Le immagini compaiono nel registro e vengono cancellate insieme agli eventi più vecchi di `event_retention_days`.

## Importazione CSV

File con separatore `;` o `,` e intestazione facoltativa: `nome; tessera; gruppo; valida_dal; valida_al; ditta; note; tipologia`. Obbligatori solo nome e tessera; gruppo e tipologia si indicano per nome (la tipologia assegna il suo gruppo se il gruppo è vuoto); le date nel formato `gg/mm/aaaa` (con ora facoltativa `hh:mm`). Prima viene mostrata un'anteprima riga per riga; se il nome corrisponde a una persona esistente la tessera nuova le viene aggiunta.

Per le tessere **già registrate** il file aggiorna la persona: gruppo, validità, ditta, note e tipologia, se diversi da quelli attuali. Nell'anteprima queste righe compaiono come "da aggiornare", con cosa cambia (ad esempio "valida al 31/10/2026 → 31/12/2026"), e si applicano solo dopo la conferma. Si confrontano solo le colonne presenti nel file. Gruppo e tipologia vuoti lasciano quelli attuali; date, ditta e note vuote li svuotano, come nel file esportato. Il modo più semplice per una modifica in blocco: esporta l'anagrafica, cambia le colonne nel foglio di calcolo e reimporta. Una persona con più tessere ha una riga per tessera, e le righe devono avere gli stessi valori. Il nome non si cambia dal file.

## Eventi MQTT dell'A1210

Per il registro servono gli eventi **AccessControl** (accesso concesso, negato, tessera sconosciuta). Se nella scheda Stato del controller la voce "Eventi AccessControl" resta gialla dopo il passaggio di una tessera, aggiungili al filtro eventi del client MQTT dell'A1210.

I sensori di Home Assistant usano anche **Door** (stato, allarmi, manomissione) e **Device** (coperchio, avvio, porte I/O). Un sensore resta "sconosciuto" finché il controller non manda il primo evento: se gli ingressi I/O restano sconosciuti anche cambiandoli, aggiungi `Device/IO/Port` al filtro eventi MQTT dell'A1210.

## Citofono Hikvision

Il citofono si associa a una porta in **Impianti**. L'add-on legge i suoi eventi dall'add-on **Hikvision Doorbell**, che li pubblica sullo stesso broker MQTT.
Non parla direttamente con il citofono.

- **Chiamate nel log**: "ha suonato", "risposta dopo N s", "nessuna risposta", "chiamata chiusa"
  (con l'istantanea, se la porta ha una telecamera).
- **Apri dal citofono**: quando si preme Apri sull'app Hik-Connect o sul monitor (in futuro anche dal centralino VoIP),
  il citofono attiva il suo relè e lo segnala. L'add-on manda allora all'A1210 **una sola apertura a tempo**,
  sulla porta associata: solo a serratura bloccata e al massimo una ogni 5 secondi.
  Nel registro compare come apertura remota con il modo e il citofono, ad esempio "App Hik-Connect (Citofono)"
  o "Monitor Interno (Citofono)"; se c'era una chiamata, il dettaglio dice dopo quanti secondi si è aperto.
  Il citofono non comunica quale account Hik-Connect ha aperto, quindi il nome della persona non c'è.
- Le aperture fatte con tessere, PIN o volti salvati sul citofono **non** vengono inoltrate: chi può entrare lo decide solo l'A1210.
- Il relè del citofono non va collegato alla serratura.

Topic letti (con nome del citofono `Citofono` e relè 1):
`hmd/sensor/Citofono/Call-state/state`, `hmd/switch/Citofono/Door-1-relay/state` e `.../attributes`.
La scheda Stato del controller mostra lo stato della chiamata e l'ora dell'ultimo messaggio ricevuto da ogni citofono.
Al primo collegamento il citofono può scaricare lo storico degli eventi e bloccarsi fino a un riavvio: tienine conto nella prima prova.
Se il citofono usa un firmware recente senza evento di chiamata, attiva `call_state_poll` nell'add-on Hikvision Doorbell.

## Stabilità e backup

- **Controller resettato o sostituito.** L'app memorizza numero di serie, modello, firmware e impronta del certificato del controller. Se all'indirizzo risponde un apparecchio diverso, smette di scrivere e di rileggere le tessere finché un amministratore non conferma il cambio nella pagina del controller.
- **Tessere non presenti sul controller.** Le tessere che spariscono dal controller non vengono cancellate dall'app: restano in Persone con l'etichetta "Non Sul Controller", da riscrivere o togliere.
- **Eventi persi.** Se Mosquitto, Home Assistant o l'add-on sono fermi, gli accessi di quel periodo vengono recuperati dal registro interno dell'A1210 (fino a 30.000 eventi, al massimo gli ultimi 7 giorni) appena il collegamento torna.
- **Orologio.** Ogni 5 minuti l'app confronta l'ora del controller con quella di Home Assistant. Se il controller è sincronizzato via NTP l'ora giusta è la sua e uno scarto oltre un minuto segnala l'orologio di Home Assistant; altrimenti il guasto è "Orologio del Controller". Imposta un server NTP nel controller.
- **Database.** Prima di ogni aggiornamento dell'add-on si fa una copia del database in `copie_database` (le ultime 3). Prima di ogni backup di Home Assistant si prepara una copia coerente in `copia_backup`; se all'avvio il database risultasse danneggiato, l'add-on riparte da quella.

## Backup dell'app

In **Impianti → Backup** (con il permesso Backup o da amministratore).

- **Cosa contiene.** I **Dati Comuni**: persone, tessere, tipologie con le regole, fasce orarie, festività, strutture e aree, utenti dell'app e impostazioni. Poi la **configurazione di ogni controller**: porte con nomi e tempi, lettori, punti di accesso e schema, profili di accesso, ingressi, collegamento MQTT e dati dell'apparecchio. Le password dei controller non si salvano: dopo un ripristino completo vanno reinserite.
- **Dove.** Nella cartella dell'add-on (`backup`), quindi entrano nei backup di Home Assistant se includono l'add-on. Tienine una copia fuori sede (NAS o cloud). Ogni parte è un file compresso e, se non è cambiata dal backup precedente, non si scrive di nuovo: cento backup senza modifiche occupano quanto uno.
- **Crea Backup Ora**, con un nome facoltativo ("Prima dei lavori al piano 2"). Ogni riga dell'elenco riassume cosa è cambiato dal backup precedente.
- **Confronta** mostra le differenze con il backup precedente, con lo stato attuale o con un altro backup, sezione per sezione.
- **Ripristina** (solo amministratori) riporta com'erano, a scelta, **Tipologie**, **Persone e Tessere**, **Porte e Lettori**. Prima di cambiare qualcosa la finestra **Cosa Cambierà** elenca ogni modifica con una spunta:
  - persone eliminate: si ricreano con le loro tessere, tipologia e accesso;
  - persone cambiate: tornano com'erano (dati, tipologia, tessere con accesso, validità e stato);
  - persone, tessere e tipologie aggiunte dopo il backup: si tolgono solo se le spunti, perché di solito sono lavoro buono fatto nel frattempo;
  - una tessera che oggi appartiene a un'altra persona non si sposta;
  - tempi della porta oltre i limiti della serratura (apertura 30 s, apertura estesa 60 s) non si riscrivono;
  - una porta che non c'è più sul controller non si ricrea: servirà il ripristino completo (0.20).
- **Prima del Ripristino.** Prima di ogni ripristino l'app crea un backup dello stato attuale: per annullare, ripristina quello.
- **Blocca** tiene il backup finché non lo sblocchi; un backup bloccato non si elimina. **Elimina** è solo per gli amministratori.
- **Controller che non risponde.** Il backup prende la configurazione di quel controller dal backup precedente, si segna **Incompleto** e compare la notifica "Backup Incompleto" tra i guasti, finché un backup successivo non riesce.

## Sicurezza

L'add-on non espone porte: risponde solo alle richieste che arrivano da Home Assistant tramite Ingress, e ogni richiesta è controllata con i permessi dell'utente. La password del controller resta nel database dell'add-on e non viene mai rimandata alla pagina. Ogni modifica è registrata con il nome dell'utente di Home Assistant. L'add-on non invia mai comandi di sblocco prolungato alla porta.
