# Changelog

## 0.21.2

Rifiniture dopo la seconda prova di reset al banco e richieste dell'utente.

- **Sensore Elettroserratura Porta** in Home Assistant, uno per porta: acceso/spento con classe lock (spento bloccata, acceso sbloccata, anche quando è aperta a tempo da una tessera). Nelle dashboard mostra il lucchetto.
- **Tessere rilette appena il controller torna raggiungibile** dopo un riavvio o un reset: l'avviso delle tessere non presenti e il Ripristino Completo compaiono subito, senza premere Rileggi né aspettare il giro dei 15 minuti.
- **Pallino verde** accanto a "Collegato" nella pagina del controller (era grigio).
- **Dettaglio dell'evento nel Registro** che segue lo scorrimento: aprendo un evento in fondo all'elenco il riquadro resta visibile invece di restare in cima alla pagina.
- **Fai Gestire al Controller** mostra "Sto Scrivendo…" mentre scrive.
- La fascia di fabbrica standard_after_hours si chiama **Fuori Orario**, come le altre fasce di fabbrica.

## 0.21.1

Correzioni dal collaudo del Ripristino Completo al banco: dopo il reset le tessere venivano lette ma la porta non si apriva.

- **Punti di accesso gestiti dal controller**. Il controller decide solo per i punti di accesso nell'elenco del suo controllo accessi (AccessController). Il reset svuota quell'elenco e il backup non lo salvava: dopo il ripristino lettori e punti di accesso c'erano, ma nessuna tessera veniva concessa o negata. Ora il backup salva l'elenco e il Ripristino Completo lo rimette dopo aver ricreato i punti di accesso. Dai backup fatti prima di questa versione fa gestire tutti i punti di accesso del backup.
- **Avviso "Punti di Accesso Non Gestiti"** se il controller ha punti di accesso per cui non decide, con il pulsante **Fai Gestire al Controller** nella pagina del controller.
- **Ingressi dell'app gestiti dal controller**: un pulsante o il relè del citofono impostati dall'app finiscono nell'elenco quando li crei, ed escono quando li togli.
- **Configurazioni di porte e lettori scritte senza dispositivo**, come dice VAPIX: il controller ci mette il suo DeviceUUID e le lega a sé.
- **Fasce orarie di fabbrica tolte** dopo un reset: quelle che non erano nel backup (per esempio standard_after_hours) sono già spuntate per essere tolte. Su un controller che funziona le fasce in più non si propongono, perché possono servire a tipologie e ingressi.
- **Pulsante Ripristino Completo** nel riquadro delle tessere non presenti della pagina Persone.
- **Pulsante o relè negato**: nel registro compare con il nome dell'ingresso e non cambia più il sensore Ultimo Accesso di Home Assistant.

## 0.21.0

Ripristino completo di un controller resettato o sostituito.

- **Ripristino Completo** nella scheda Backup del controller. Da un backup ricrea fuso orario, NTP, collegamento ed eventi MQTT, fasce orarie, porte con la configurazione della serratura, lettori, morsetti, punti di accesso e profili, sempre con gli stessi token: tipologie, tessere, ingressi, registro ed entità di Home Assistant restano collegati. Poi riscrive le tessere.
- **Anteprima con l'ordine di scrittura**. La password del broker MQTT si scrive lì e non si salva. Gli oggetti di fabbrica in più sono già spuntati per essere tolti quando il controller è appena resettato. I tempi di apertura si scrivono entro i limiti della serratura. La programmazione oraria delle porte non si ripristina mai.
- **Controller sostituito**: porte e lettori passano al dispositivo nuovo (DeviceUUID). Se un controller rifiutasse un token, l'oggetto si crea con uno nuovo e l'app lo cambia in tipologie, tessere e ingressi.
- **Il backup salva anche i modi di identificazione** (CardOnly, REXOnly e quelli fatti a mano), che servono a ricreare i punti di accesso.
- **Tessere quasi tutte sparite**: il riquadro nella pagina Persone propone il Ripristino Completo.

## 0.20.1

Suggerimenti dal collaudo della 0.19.5.

- **Aperture remote e citofono con l'ora del controller**: nascono in Home Assistant e prendevano la sua ora. Sul banco, con Home Assistant 36 secondi indietro, un'apertura remota poteva finire nel registro prima di un passaggio con tessera avvenuto prima. Ora, se il controller è sincronizzato via NTP, si usa l'ora del controller, la stessa dei passaggi con tessera.
- **CSV del registro con la colonna Recuperato** per gli eventi arrivati dopo dal registro interno del controller.

## 0.20.0

Backup automatico.

- **Backup automatico**, attivo di serie. Ogni ora l'app guarda se è cambiato qualcosa e solo in quel caso crea un backup (si può scegliere ogni 6 ore o solo di notte). Alle 03:00 fa comunque un backup notturno, anche se non è cambiato niente. Tutto si imposta in **Impostazioni → Backup**.
- **Backup prima delle modifiche rischiose**:
  - prima di programmare porte, lettori, ingressi e punti di accesso, e prima di cambiare tipologie, fasce orarie e festività (uno ogni 10 minuti per una serie di modifiche);
  - prima di ogni importazione CSV;
  - subito dopo ogni aggiornamento dell'add-on.
- **Ritenzione come restic**: si tengono gli ultimi 24, poi il più recente di ogni giorno per 14 giorni, di ogni settimana per 8 settimane, di ogni mese per 12 mesi e di ogni anno per 3 anni. I bloccati restano sempre. Se cambi le regole in modo che dei backup scadano subito, l'app chiede conferma dicendo quanti sono.
- **Verifica giornaliera** dei file dei backup, e a richiesta con **Verifica Ora**. I backup rovinati diventano il guasto "Backup Rovinati", con l'elenco nella pagina Backup.
- **Pagina Backup** con lo stato in cima: frequenza, prossimo backup e ultima verifica. Passando il mouse sulla data di un backup si vede perché si tiene.
- **Riquadro Backup** per il cruscotto: ultimo backup, prossimo automatico e verifica.
- **Controller irraggiungibili o cambiati**: il backup non aspetta i controller che il controllo di stato dà per irraggiungibili, e non salva la configurazione di un apparecchio diverso non ancora confermato. Per quel controller tiene la configurazione del backup precedente.
- Nell'attività degli utenti finiscono solo i backup chiesti da una persona (manuali e prima di un ripristino), non quelli automatici.

## 0.19.5

- **Registro in ordine di orario**: gli eventi recuperati dal controller dopo un'interruzione (per esempio Mosquitto fermo con l'add-on acceso) finivano in cima al Registro perché arrivati per ultimi. Ora Registro, Notifiche, Ultimi Eventi, la pagina della porta e quella della persona li mostrano al loro orario, con il segno "recuperato". I separatori Oggi/Ieri restano interi, "Carica Altri" prosegue dall'ultimo evento mostrato senza saltarne o ripeterne e l'export CSV segue lo stesso ordine.

## 0.19.4

- **Pagine che non lampeggiano**: Notifiche, Cruscotto e Da Registrare si aggiornano ogni 15 secondi cambiando solo quello che è cambiato, invece di ridisegnare tutta la pagina. Il testo nella ricerca e il cursore restano, la pagina non torna in cima e le foto non si ricaricano.

## 0.19.3

Ultime correzioni dal collaudo al banco della 0.19.

- **Eventi delle persone eliminate**: quando si elimina una persona i suoi eventi restano nel registro con il nome, ma staccati dalla persona. Così nessuno li può ereditare, nemmeno una persona ripristinata da un backup con lo stesso numero. All'aggiornamento si staccano anche gli eventi delle persone già eliminate, compresi quelli rimasti dai numeri riusati prima della 0.19.2.
- **Pagina aggiornata da sola**: dopo un aggiornamento dell'add-on la pagina aperta in Home Assistant si ricarica appena non c'è una finestra aperta o una modifica da salvare, invece di restare alla versione vecchia fino a un ricaricamento a mano. La pagina non viene più presa dalla cache del browser.

## 0.19.2

Correzioni dal collaudo al banco della 0.19.

- **Persone nuove con il numero di una persona eliminata**: una persona creata dopo averne eliminata un'altra poteva prenderne il numero interno, e con quello i passaggi nel registro e l'ultimo accesso. Ora i numeri non si riusano mai. All'aggiornamento gli eventi più vecchi della persona a cui erano attaccati vengono staccati: restano nel registro con il loro nome.
- **Log dell'add-on più pulito**: niente più una riga per ogni richiesta della pagina (decine al minuto con il cruscotto aperto). Con il livello debug tornano.
- **Orologio appena avviato**: nella scheda Stato non compare più per qualche istante la vecchia frase "36 s avanti" prima di "Home Assistant è 36 s indietro".
- **Backup**: i lettori si contano senza gli ingressi (pulsante di uscita, relè del citofono), che hanno il loro numero. Il backup fatto prima di un ripristino si chiama "Prima del ripristino del backup del 25/10/2026 19:05", senza catene di nomi, e ogni riga mostra sempre il tipo.
- **Persona tolta dall'app**: il nome negli accessi recuperati viene dai suoi accessi già nel registro, con gli accenti ("Prova Stabilità (tolta dall'app)").

## 0.19.1

Correzione dalla prova al banco della 0.18.

- **Eventi recuperati senza doppioni**: se una tessera veniva tolta dall'app dopo un passaggio, il recupero dal registro del controller registrava di nuovo quel passaggio come "sconosciuta" (finendo in Da Registrare) e come "concesso" con il codice dell'utente al posto del nome. Ora ogni evento del controller ha un'impronta (porta, tipo di evento, tessera letta) che non dipende dall'anagrafica, e lo stesso evento non si registra due volte. I doppioni già creati vengono tolti all'aggiornamento. Un accesso di una persona non più nell'app mostra il nome leggibile con "(tolta dall'app)".

## 0.19.0

Backup dell'app, con confronto e ripristino selettivo. Il salvataggio automatico con la pulizia dei vecchi backup arriva nella 0.20.

- **Impianti → Backup**: con **Crea Backup Ora** salvi, con un nome se vuoi, i **Dati Comuni** (persone, tessere, tipologie, fasce orarie, festività, strutture e aree, utenti dell'app, impostazioni) e la **configurazione di ogni controller** (porte con nomi e tempi, lettori, punti di accesso, schema, profili, ingressi, collegamento MQTT). Le password dei controller non si salvano mai.
- **Spazio minimo**: ogni parte è un file compresso; se non è cambiata dal backup precedente non si scrive di nuovo. Ogni riga dell'elenco dice cosa è cambiato rispetto al precedente, per esempio "+1 persona, +1 tessera, Porta Scala Teatro modificata", oppure "Nessuna modifica".
- **Confronta**: per ogni backup vedi le differenze con il precedente, con lo stato attuale o con un altro backup, divise per sezione (persone, tessere, tipologie, porte, lettori…) con il valore di prima barrato e quello nuovo.
- **Ripristina**, solo per gli amministratori: scegli tra Tipologie, Persone e Tessere, Porte e Lettori; la finestra **Cosa Cambierà** elenca ogni modifica con una spunta, e decidi tu cosa riportare com'era. Le persone eliminate si ricreano con le loro tessere, quelle cambiate tornano com'erano; quello che è stato aggiunto dopo il backup si toglie solo se lo spunti. Una tessera che oggi è di un'altra persona non si tocca. Tempi della porta oltre i limiti della serratura non si riscrivono.
- **Prima di ogni ripristino** l'app crea il backup "Prima del Ripristino": per tornare indietro basta ripristinare quello.
- **Blocca** tiene un backup finché non lo sblocchi (non si può eliminare). Elimina è solo per gli amministratori.
- **Scheda Backup del controller**: lo storico della sola configurazione di quel controller, con confronto e ripristino di porte e lettori.
- **Controller che non risponde**: il backup prende la sua configurazione dal backup precedente, si segna "Incompleto" e compare la notifica "Backup Incompleto" tra i guasti.
- Nuovo permesso **Backup** per vedere, creare e confrontare i backup senza essere amministratore.
- Cambiando scheda in Impianti mentre la pagina carica, la scheda nuova non viene più coperta da quella vecchia.

## 0.18.1

Correzioni dalla prova al banco della 0.18.0.

- **Orologio: si dice quale è sbagliato.** Lo scarto confronta il controller con Home Assistant, ma da solo non dice chi dei due è fuori. Se il controller è sincronizzato via NTP l'ora giusta è la sua: la scheda Stato scrive "Giusto, sincronizzato via NTP · Home Assistant è 36 s indietro" e oltre un minuto compare la notifica "Orologio di Home Assistant", di gravità bassa, perché le fasce orarie le applica il controller. Il guasto "Orologio del Controller" resta solo per un controller non sincronizzato. In più la scheda Stato mostra lo scarto dal server NTP e, se Home Assistant non concorda con l'ora del telefono o del PC, anche quello.
- **Controllo ogni 30 secondi più leggero**: ora e NTP si rileggono ogni 5 minuti e quando il controller torna raggiungibile; ogni 30 secondi resta solo la verifica del collegamento. Nel log dell'add-on non compare più una riga per ogni richiesta al controller (resta con il livello debug).
- Testi al singolare corretti: "Togliere dall'app la tessera non presente…", "1 tessera" nelle attività, nelle notifiche e nella rilettura dal controller.
- Date con l'anno a quattro cifre (25/10/2026) in Impostazioni, Attività e negli altri elenchi.

## 0.18.0

Stabilità: l'app non perde dati se il controller viene resettato o sostituito, se Mosquitto si ferma o se un backup viene preso nel momento sbagliato.

- **Tessere sparite dal controller**: non vengono più cancellate dall'app. Restano in Persone con l'etichetta "Non Sul Controller" e un avviso in cima all'elenco; da lì le riscrivi sul controller con gruppo e validità salvati, oppure le togli dall'app. Ditta, note e tipologia non si perdono più dopo un reset. Se sparisce più di una piccola quota delle tessere, o tutte, il guasto è critico.
- **Identità del controller**: al primo collegamento l'app memorizza numero di serie, modello, firmware e impronta del certificato. Se all'indirizzo risponde un apparecchio diverso compare il guasto "Controller Cambiato" e l'app smette di scrivere sul controller e di rileggere le tessere, finché un amministratore non conferma con "Conferma Nuovo Controller". Aprire la porta resta possibile. Un aggiornamento del firmware non è un cambio: si annota nell'attività.
- **Controllo di ogni controller ogni 30 secondi**, in parallelo: spia verde, gialla o rossa nella pagina Impianti, guasto "Controller Non Raggiungibile" dopo un minuto senza risposta. Se il controller risponde ma non manda eventi a MQTT, il guasto lo dice.
- **Orologio del controller**: confronto con l'ora di Home Assistant e guasto oltre un minuto di scarto, perché le fasce orarie scattano con l'ora del controller. Nella scheda Stato ci sono scarto, fuso orario e server NTP.
- **Eventi persi**: all'avvio e dopo ogni interruzione (Mosquitto, Home Assistant o controller offline) l'app recupera dal registro interno dell'A1210 gli accessi del periodo scoperto. Nel Registro hanno la nota "recuperato"; niente foto e niente evento in Home Assistant, perché arrivano in ritardo. Nessun doppione con quelli arrivati via MQTT.
- **Tasto Riprova**: se il controller non risponde a una modifica compare una finestra con Riprova, invece del solo messaggio d'errore.
- **Aggiornamenti del database**: versione dello schema con migrazioni numerate, ognuna in una transazione. Prima di ogni aggiornamento dell'add-on si fa una copia del database e si tengono le ultime 3.
- **Backup di Home Assistant coerente**: subito prima di ogni backup l'add-on prepara una copia sicura del database. Se all'avvio il database risultasse danneggiato, riparte da quella copia. In Impostazioni → Generale si vedono versione dello schema e copie.

## 0.17.0

- **Cruscotti predefiniti per ruolo**, tutti a griglia piena senza buchi:
  - **Amministratore**: Tessere in Scadenza e Tessere da Registrare, i quattro numeri del giorno, Ultimi 7 Giorni, Ultimi Eventi e Da Gestire (la disposizione che hai preparato tu).
  - **Operatore** (registra e modifica tessere): Tessere da Registrare e Da Gestire in alto, poi i numeri del giorno, Ultimi Eventi, Tessere in Scadenza e Ultimi 7 Giorni.
  - **Consultazione** (vede il registro): i numeri del giorno, Ultimi Eventi in grande, Da Gestire e Ultimi 7 Giorni.
  - **Solo Stato**: i numeri del giorno, Da Gestire, Porte, Ultimi 7 Giorni e Stato Impianto.
  - Il predefinito si sceglie dai permessi, quindi vale anche per i ruoli personalizzati. Chi ha già salvato il suo cruscotto lo tiene; Ripristina Predefinito porta a quello del suo ruolo.
- **Barra in alto più bassa**: tolti i bollini di accessi e negati (restano nei riquadri). Restano solo Allarmi, Guasti, Sistema OK e Add-on Non Raggiungibile.
- La riga "Modifica Cruscotto" sotto la barra non c'è più: al suo posto una matita accanto all'ora, la scritta compare passandoci sopra.
- Sul telefono gli elenchi larghi un quarto vanno a riga intera; a metà riga restano solo i riquadri con un numero.

## 0.16.2

- **Telecamera del controller**: se il controller è in un altro locale, nella scheda Telecamere puoi dargli una sua telecamera ("Inquadra: Coperchio del Controller"). Quando si apre il coperchio la foto viene da quella telecamera. Senza, come prima, nessuna foto.
- Tutto quello che succede alla porta (accessi, aperture, citofono, allarmi della porta) continua a usare la telecamera della porta.
- Se la telecamera del controller non fa la foto, compare tra i Guasti come le altre.

## 0.16.1

- L'elenco a tendina dei bollini in alto si stacca meglio dai riquadri, soprattutto con il tema scuro: sfondo più chiaro, bordo e ombra più marcati. Il bollino della tendina aperta resta evidenziato.

## 0.16.0

- **Cruscotto a griglia**: ogni riquadro ha larghezza (quarti di riga) e altezza (righe da 64 px), quindi i bordi restano allineati.
  - I riquadri piccoli riempiono da soli gli spazi vuoti: per esempio quattro riquadri da una riga si impilano accanto a un riquadro alto quattro righe.
  - In Modifica Cruscotto si trascina la maniglia nell'angolo in basso a destra: il riquadro cambia larghezza e altezza mentre muovi, a scatti di griglia. Con la tastiera, le frecce sulla maniglia.
  - Se il contenuto è più lungo, nel riquadro compare la barra di scorrimento.
  - Ogni riquadro ha una misura minima sotto la quale non si legge più (per esempio il grafico dei 7 giorni almeno mezza riga e tre righe); i riquadri con un solo numero arrivano al massimo a due righe.
  - In modifica la barra con maniglia, nome e × sta sopra il contenuto, così anche i riquadri piccoli mantengono la loro misura.
- Il bollino Allarmi in alto conta gli allarmi da gestire (non ancora visti), mentre il riquadro Allarmi Oggi li conta tutti: l'elenco a tendina ora si chiama Allarmi da Gestire.
- L'apertura del coperchio del controller non scatta più la foto della telecamera della porta, che non la inquadra.
- Il cruscotto predefinito ora è: Da Gestire e Ultimi Eventi affiancati, sotto Porte e Ultimi 7 Giorni. I cruscotti già salvati prendono l'altezza predefinita di ogni riquadro.

## 0.15.4

- Pagina Notifiche più compatta: "Ultime 24 ore" va accanto al titolo, ricerca e pulsanti stanno sulla riga di Da Gestire (sul telefono restano solo le icone), la ricerca non si allarga più su tutta la riga.
- La linea sotto la barra in alto compare solo quando la pagina scorre.

## 0.15.3

- **Barra in alto più snella**, al posto della riga di riquadri con i numeri di oggi.
  - **Guasti** (controller non raggiungibile, MQTT perso, telecamera che non fa la foto) e **Allarmi** (porta forzata o aperta troppo a lungo, manomissioni non ancora viste) compaiono solo quando ci sono, in tutte le pagine. Altrimenti si vede **Sistema OK**.
  - Un clic apre l'elenco a tendina, con il collegamento alle Notifiche. Dagli Allarmi puoi segnarli tutti come visti. I guasti restano finché non si risolvono.
  - Nel cruscotto, accanto al titolo, la data di oggi e due badge piccoli: accessi e negati di oggi. Il clic sui negati apre il Registro con la vista Solo Negati.
- I riquadri Accessi, Negati e Allarmi di oggi non sono più nel cruscotto predefinito. Restano in Aggiungi Riquadro, più compatti. Se avevi già salvato il tuo cruscotto, toglili con Modifica Cruscotto o usa Ripristina Predefinito.

## 0.15.2

- Modifica del cruscotto:
  - cambiando la larghezza o togliendo un riquadro la pagina resta dov'era, non torna in cima;
  - il titolo non compare più due volte e la × resta sulla stessa riga anche nei riquadri da ¼;
  - i riquadri di una riga non si allungano più fino all'altezza del più alto.
- Presa Visione e Segna Tutte Come Viste finiscono nel Registro Attività, con l'utente e le notifiche viste.
- Gli eventi del coperchio del controller mostrano il nome attuale del controller. Quelli già registrati vengono aggiornati all'avvio, e di nuovo quando rinomini il controller.

## 0.15.1

- Le telecamere si vedono ovunque con il nome di Home Assistant, non con l'entity id: porte, dettaglio della porta, impostazioni della porta, scheda Telecamere e scelta della telecamera. L'entity id compare solo se due telecamere hanno lo stesso nome, o se la telecamera non esiste più in Home Assistant.

## 0.15.0

- **Cruscotto personalizzabile**, per ogni utente dell'app.
  - Con **Modifica Cruscotto** trascini i riquadri per spostarli, scegli la larghezza (un quarto, metà, tre quarti o riga intera) e togli con × quelli che non ti servono.
  - Con **Aggiungi Riquadro** rimetti quelli tolti o ne aggiungi di nuovi: Tessere in Scadenza, Tessere da Registrare, Stato Impianto (controller, eventi MQTT, coperchio, ultimo evento).
  - Salva o Annulla; **Ripristina Predefinito** torna ai riquadri di partenza.
  - Il cruscotto è salvato nell'add-on e lo ritrovi da ogni dispositivo. Ogni utente ha il suo. Sul telefono i riquadri piccoli vanno due per riga e gli altri a riga intera.
- Il riquadro Da Gestire del cruscotto mostra le prime cinque notifiche aperte con la loro gravità, come la pagina Notifiche.

## 0.14.0

- **Pagina Notifiche rifatta.**
  - In alto, **Da Gestire**: le notifiche sono ordinate per gravità, Critica, Media o Bassa.
    - Critica: allarmi, manomissioni, controller o MQTT persi, tessera sconosciuta ripetuta.
    - Media: più accessi negati di fila alla stessa persona, oppure una tessera sconosciuta passata una volta.
    - Bassa: un singolo accesso negato, una tessera in scadenza.
  - Gli eventi uguali vengono raggruppati in una sola notifica con il conteggio, ad esempio "×3 · 3 tentativi in 3 minuti". Sono uguali quando hanno stessa persona o tessera, stessa porta e stesso motivo, e arrivano a meno di 5 minuti l'uno dall'altro.
  - Ogni notifica ha la foto della porta (o il motivo per cui manca), Dettagli e Presa Visione. Un nuovo tentativo dopo la Presa Visione la rimette da gestire.
  - Sotto, il **Registro Eventi** delle ultime 24 ore, con le schede Tutte, Accessi Negati e Allarmi e Manomissioni, il conteggio di ciascuna e la colonna Stato (Da Gestire o Visto). La spunta "Solo da gestire" filtra le righe.
  - Ricerca per persona, tessera o porta, filtro per porta se ce n'è più di una, e "Segna Tutte Come Viste".
- Il numero sulla campanella conta le notifiche da gestire. In Impostazioni → Notifiche ogni tipo è **Attiva** o **Disattivata**: le informative non esistono più e contano come attive.
  - Accessi Negati è ora attiva in modo predefinito.
  - Una tessera sconosciuta compare come notifica solo quando viene letta: l'elenco completo resta in Da Registrare.
- Corretto: aprendo più volte il registro, un clic su una foto la apriva più volte.

## 0.13.2

- Scelta della telecamera: segnate anche le telecamere ferme (stato "idle" di Home Assistant, che Frigate usa per quelle spente) e quelle la cui ultima foto non è riuscita. Prima erano segnate solo quelle non disponibili, che con Frigate non capitano quasi mai.

## 0.13.1

- Istantanea non riuscita: non resta più solo nel log dell'add-on.
  - Nel dettaglio dell'evento del registro compare la riga Foto con il motivo (per esempio "telecamera non disponibile, risposta 503 di Home Assistant").
  - Nella scheda Telecamere del controller la telecamera è segnata Da Controllare, con l'ora dell'ultima foto non riuscita, se non è disponibile in Home Assistant o se non esiste più.
- Nella scelta della telecamera, quelle non disponibili in Home Assistant sono segnate.

## 0.13.0

- **Telecamere e Videocitofoni** diventano due schede della pagina del controller, accanto a Porte e Lettori: si aggiungono, si spostano su un'altra porta e si tolgono da lì. Prima erano nascoste nelle impostazioni della porta.
- Menu laterale come in Home Assistant: in fondo, separati dalle pagine dell'impianto, ci sono Notifiche, Impostazioni dell'app e l'utente collegato con il suo ruolo. Per l'amministratore l'utente apre la gestione degli utenti.

## 0.12.0

- **Istantanee nella cartella Media di Home Assistant**: le foto stanno in Media → axis_access_manager (cartella `/media/axis_access_manager`, visibile anche da File editor e Samba). All'aggiornamento quelle già salvate vengono spostate lì. Non entrano più nel backup dell'add-on.
- Una sola istantanea per passaggio, sull'esito: accesso negato sempre, accesso concesso con "Istantanea Anche sugli Accessi Concessi". La lettura della tessera e la tessera sconosciuta non scattano più una seconda foto uguale.
- Istantanea anche sulle aperture a distanza (pulsanti di Home Assistant e citofono), anche senza "Istantanea Anche sugli Accessi Concessi".
- Il nome del file usa l'ora dell'evento data dal controller, non l'orologio di Home Assistant.
- Su computer le pagine occupano tutta la larghezza dello schermo: prima si fermavano a 1180 pixel e a destra restava spazio vuoto.

## 0.11.3

- Impianti: nella scheda del controller la scritta "Collegato" lascia il posto a un pallino accanto al nome. Verde: il controller risponde e manda gli eventi. Giallo: risponde, ma gli eventi MQTT non arrivano. Rosso: non risponde. Passando sopra al pallino si legge il dettaglio.

## 0.11.2

- Pagina della porta, scheda Dispositivi, con testi più chiari:
  - i lettori dicono da che lato della porta sono ("Lettore tessere sul lato entrata");
  - pulsanti e relè collegati agli ingressi del controller mostrano ruolo e morsetto ("Pulsante Uscita · morsetto Aux 1 del controller") invece di "Uscita · Ingresso IO1";
  - il videocitofono dice se apre la porta e con quale relè;
  - i bollini "Attivo" e "Apre la Porta" sono stati tolti: compare un bollino solo per un lettore disattivato.
- Impianti: la scheda del controller conta a parte lettori e ingressi usati ("1 lettore · 2 ingressi"); prima pulsanti e relè sugli ingressi risultavano lettori.

## 0.11.1

- Scheda della persona: nel riquadro Accesso Personalizzato il nome della tipologia resta nella frase invece di andare a capo.
- Registro Attività: creazione, modifica ed eliminazione degli utenti mostrano il nome visualizzato invece del nome utente.

## 0.11.0

- **Ogni permesso mostra solo le sue sezioni.** Nuovo permesso **Stato** per Cruscotto, Porte e Notifiche: senza di esso le tre sezioni spariscono dal menu e il server non dà più i loro dati. Impostazioni compare solo a chi può esportare e importare, configurare gli impianti o gestire gli utenti.
- Dopo l'accesso l'utente arriva sulla prima sezione che gli è permessa; chi non ha permessi vede un avviso.
- Nuovi ruoli: **Tessere** (anagrafica, tipologie e fasce orarie), **Solo Registro** e **Nessun Accesso**. In fondo al menu compare il ruolo dell'utente.
- Gli utenti già presenti ricevono il permesso Stato all'aggiornamento, una volta sola: nessuno perde sezioni finché l'amministratore non toglie la spunta.
- Scheda della persona: nel riquadro Accesso Personalizzato il profilo appare con il suo nome anche se è stato creato da poco, non più come codice.

## 0.10.0

- **Tipologie al posto di gruppi e tipologie.** La tipologia, in Persone → Tipologie, dice chi è la persona e con le righe **Dove e Quando** quali porte, aree o strutture apre e in quale fascia. L'app crea e aggiorna il suo profilo sul controller. In Regole restano Fasce Orarie e Festività; la scheda della persona chiede solo la tipologia.
- Aree e strutture valgono anche per le porte aggiunte dopo: spostare una porta in un'area aggiorna i profili delle tipologie che la comprendono.
- Migrazione all'avvio, senza scritture sul controller e senza cambiare chi entra:
  - le tipologie prendono il loro gruppo;
  - chi non aveva una tipologia entra in quella del suo gruppo;
  - il gruppo predefinito diventa la tipologia predefinita.
- Un profilo in comune tra più tipologie si separa alla prima modifica: la tipologia modificata riceve il suo profilo e le altre non cambiano.
- **Accesso Personalizzato**: etichetta nell'elenco Persone e tasto Allinea alla Tipologia nella scheda.
- La pagina della porta mostra le tipologie che la aprono; le fasce orarie dicono quali tipologie le usano.
- Porte, aree e strutture usate da una tipologia non si eliminano finché sono nelle sue righe.
- Importazione CSV: una nuova tipologia porta con sé il suo accesso; la colonna gruppo serve solo per un accesso personalizzato.

## 0.9.1

- Registro: l'apertura del coperchio del controller non mostra più una porta. Compare come "coperchio del controller aperto" con il nome del controller; anche gli eventi già salvati vengono corretti all'avvio.
- Ingressi:
  - togliendo un ruolo si rimuove anche la configurazione del punto di identificazione, che prima restava orfana sul controller;
  - un ingresso supervisionato torna davvero al modo semplice: il firmware ignora il cambio di modo se l'ingresso non è assegnato, quindi l'app lo assegna prima come pulsante normale e poi lo libera;
  - salvare un ingresso aggiorna in Home Assistant solo le entità degli ingressi, senza riconnettersi: le altre entità non passano più da "sconosciuto".
- Scheda dell'ingresso:
  - il titolo non ripete più "Ingresso";
  - la nota sul nome dell'entità di Home Assistant compare solo per gli ingressi che hanno un'entità;
  - REX e Contatto Porta mostrano un trattino, perché il controller non ne invia lo stato.
- Fasce orarie: tolta l'icona dell'orologio che copriva l'ora negli intervalli.
- Cambiando scheda mentre la pagina del controller si carica, la risposta arrivata tardi non sovrascrive più la nuova scheda.

## 0.9.0

- **Ingressi del controller** nello schema, sotto i lettori: contatto porta e REX del connettore Door, i due AUX, Manomissione e Allarme, con lo stato in tempo reale dove il controller lo manda. Tocca un ingresso e poi una porta per collegarlo, oppure tocca due volte l'ingresso per aprirne la scheda: nome, ruolo, porta, contatto (Normalmente Aperto o Chiuso) e **Linea Supervisionata** sì o no.
- Ruoli:
  - **Pulsante Uscita**: sblocca la porta, per esempio dalla portineria;
  - **Relè Citofono**: apre la porta anche con Home Assistant spento;
  - **Ingresso di Allarme**;
  - **Non Usato**.
- Per pulsante e relè l'app scrive sul controller il punto di identificazione, l'assegnazione dell'ingresso e il punto di accesso in uscita. Ogni modifica si annulla dallo Storico Modifiche.
- Registro: le aperture compaiono con il nome dato all'ingresso (ad esempio "Pulsante Portineria"), con ruolo e morsetto nel dettaglio ("Pulsante Uscita · Aux 1").
- Home Assistant: le entità degli ingressi AUX, Manomissione e Allarme prendono il nome scelto nell'app.

## 0.8.4

- Registro e Notifiche: anche i dinieghi salvati prima della 0.8.2 compaiono con il motivo in italiano ("fuori fascia oraria" invece di "NotPermittedAtThisTime"); la traduzione si fa una volta all'avvio.
- Controller → Stato: **Porte sul Controller** mostra i nomi delle porte invece dei codici interni (il codice resta nel suggerimento al passaggio del mouse).
- Telefono: nelle Notifiche i pulsanti Apri e Presa Visione vanno sotto il testo (prima il titolo si spezzava lettera per lettera); nell'elenco Porte lo stato passa sotto il nome.
- Impostazioni → Esporta e Importa: il testo spiega che le tessere già registrate vengono aggiornate, non più saltate.

## 0.8.3

- Porte: **Accessi Oggi** conta come il cruscotto (accessi concessi e aperture remote). Prima sommava anche la lettura della tessera che precede ogni accesso e le chiamate al citofono, e il numero risultava quasi doppio.

## 0.8.2

- **Correzione importante:** i gruppi creati o modificati dall'add-on non facevano entrare nessuno, nemmeno con la fascia "Sempre" (il controller rispondeva "fuori fascia oraria"). L'A1210 controlla la fascia oraria sia nelle regole sia nel profilo, e l'add-on la scriveva solo nelle regole. Ora la scrive in entrambi; i gruppi già salvati si riparano da soli al primo avvio dopo l'aggiornamento (nel Registro Attività compare "gruppo riparato").
- **Correzione importante:** con le festività inserite non si potevano creare né modificare fasce orarie con "Non Valida nei Giorni Festivi" (attiva di serie): il controller rifiutava la regola annuale. Ora le festività ricorrenti si scrivono in una forma che l'A1210 accetta, e al primo avvio l'add-on riscrive quelle che prima non era riuscito a salvare.
- Tempi della porta: **Tempo di Apertura** al massimo 30 secondi e **Apertura Estesa** al massimo 60, per non tenere alimentata a lungo la serratura. Un valore più alto già presente sul controller non impedisce di salvare le altre modifiche. I messaggi d'errore usano i nomi dell'interfaccia ("Tempo di Apertura: tra 1 e 30 secondi").
- Registro: i motivi dei dinieghi in italiano anche per "fuori fascia oraria", "PIN errato", "passaggio ripetuto (antipassback)" e "rifiutata dal controller".
- Importazione CSV: una data di fine validità senza ora vale fino a fine giornata ("valida al 31/12" comprende il 31).
- Cruscotto più leggibile: gli avvisi sono raccolti in un solo riquadro **Da Gestire** (o "Tutto in Ordine"), il grafico mostra solo accessi concessi e negati, e c'è il nuovo numero **Allarmi Oggi**.

## 0.8.1

- Importazione CSV: per le tessere già registrate aggiorna gruppo, validità, ditta, note e tipologia della persona se nel file sono diversi, con l'anteprima di cosa cambia ("da aggiornare"). Così si può esportare l'anagrafica, modificarla nel foglio di calcolo e reimportarla.
- Aperture dal citofono nel registro con etichette in Title Case, come quelle da Home Assistant: "App Hik-Connect (Citofono)" invece di "citofono · centrale", "Monitor Interno (Citofono)". Il tempo dalla chiamata passa nel dettaglio; le chiamate riportano il nome del citofono.

## 0.8.0

- Nuove entità in Home Assistant, per poter togliere il vecchio package YAML:
  - per ogni porta, **Allarme**, **Preallarme** e **Manomissione** come sensori acceso/spento, con il tipo e l'ora dell'ultimo cambio negli attributi;
  - per il controller, **Controller Online**, **Coperchio Controller Aperto** e **Ultimo Avvio Controller**;
  - gli ingressi I/O del controller: **Ingresso AUX 1**, **Ingresso AUX 2**, **Ingresso Manomissione** e **Ingresso Allarme**.
- Tolte dalla scheda Configurazione le vecchie opzioni "Obsoleta" (fra cui la password dell'A1210): da tempo si configura tutto in Impianti.

## 0.7.5

- Registro: lo stesso diniego che il controller manda su più topic compare una volta sola (prima una tessera sconosciuta dava tre righe). **Accessi Negati Oggi** conta i dinieghi del controller e non somma più anche la lettura della tessera sconosciuta.
- Identificativo del dispositivo (DeviceUUID) di un lettore ricollegato dallo schema: ora si legge prima dalla configurazione del lettore sul controller, poi dallo Storico Modifiche, e infine dal dispositivo della porta. Così funziona anche senza storico.

## 0.7.4

- Schema lettori ↔ porte: un lettore scollegato e poi ricollegato riprende l'identificativo del dispositivo (DeviceUUID) dallo Storico Modifiche, invece di essere scritto senza. Se un collegamento è già stato salvato senza identificativo, il prossimo Salva dello schema lo ripara.

## 0.7.3

- Aperture dai pulsanti di Home Assistant con il nome di chi ha premuto: nel registro compare "Luca (Home Assistant)" invece di "Home Assistant". Il nome è quello della persona di Home Assistant collegata all'utente; le aperture da automazioni compaiono come "Automazione (Home Assistant)".

## 0.7.2

- Il sensore **Ultimo Accesso** di Home Assistant riparte dall'ultimo passaggio del registro dopo un riavvio, invece di "nessuno"; per le aperture remote senza origine mostra "Apertura Remota".
- Schema lettori ↔ porte: il messaggio per una direzione che la porta non ha sul controller non rimanda più alla pagina web dell'A1210, che non gestisce i punti di accesso.

## 0.7.1

- **Impostazioni** riguarda solo l'applicazione: Home Assistant, opzioni dell'add-on, esportazione e importazione, notifiche, utenti e attività.
- Lo stato di controller, porte, eventi MQTT e citofoni passa nella nuova scheda **Stato** della pagina del controller. Avvisi e notifiche di collegamento portano lì.

## 0.7.0

- **Notifiche**: campanella nel menu con il numero delle notifiche urgenti (allarmi delle porte, controller non raggiungibile, MQTT scollegato), pagina Notifiche con Da Gestire e Informative, Presa Visione. In Impostazioni → Notifiche l'amministratore sceglie per ogni tipo se è urgente, informativo o disattivato.
- Tolto lo stato della porta dalla barra in alto: si vede nel dettaglio della porta.
- **Impianti**: controller come schede per struttura e area; l'ingranaggio apre la pagina Connessione, la scheda apre la configurazione del controller.
- **Schema lettori ↔ porte**: tocca un lettore e poi una porta per collegarli, ingranaggio sul filo per direzione, attivazione e scollegamento; le modifiche si scrivono sul controller solo con Salva, con richiesta di conferma uscendo dalla pagina.
- Porte, lettori e storico delle modifiche ora stanno nella pagina del controller (sostituiscono la scheda Programmazione).
- **L'area è del controller**: porte e lettori la ereditano; la descrizione del controller dice la posizione precisa. Migrazione automatica dalla 0.6.
- **Porte**: elenco essenziale per struttura e area; il dettaglio si apre con un clic anche con una porta sola.

## 0.6.0

- **Menu laterale** sempre presente con le icone, allargabile con ☰; sul telefono si richiude da solo dopo la scelta. Sostituisce la barra in basso.
- **Strutture e aree**: ogni porta sta in un luogo (es. Teatro › Accesso Scala). Gestione in Impianti → Strutture e Aree; la porta esistente finisce in "Struttura Principale › Generale".
- **Pagina Porte** e **dettaglio porta**: stato, numeri di oggi, lettori, citofono, telecamera, gruppi che la aprono, ultimi eventi.
- **Cruscotto** con le porte raggruppate per struttura e area.
- **Registro compatto**: una riga per evento, dettagli al clic; viste Da Controllare, Solo Negati, Solo Accessi, Allarmi e Tutto, con il conteggio degli accessi non mostrati; filtro e colonna Porta quando le porte sono più di una (anche nel CSV).
- **Persone**: elenco essenziale con il solo nome, il colore della tipologia e le etichette solo per i problemi; filtri rapidi per tipologia.

## 0.5.0

- **Impianti nell'app**: controller (indirizzo, utente, password, prefisso MQTT) e porte si configurano nell'app, con prova del collegamento e riavvio automatico del collegamento. Al primo avvio la configurazione delle opzioni viene importata da sola.
- Per ogni porta: nome, telecamera per le istantanee, videocitofono associato e pulsanti in Home Assistant. Struttura pronta per più porte e più controller.
- **Programmazione del controller**: nomi e tempi delle porte, nomi dei lettori, collegamento lettore-porta e direzione, con storico e annullamento di ogni modifica.
- **L'apertura della porta non è più nell'app**: resta nei pulsanti di Home Assistant, sempre gestita dall'add-on con gli stessi controlli.
- **Tipologie di tessera** (Manutentore, Pulizie, Ospite…): colore, gruppo e durata assegnati in automatico; filtro nell'elenco persone; colonna `tipologia` nel CSV.
- Gruppi e fasce orarie predefiniti ora si modificano o si nascondono; il gruppo predefinito si sceglie nell'app.
- Fasce create con altri strumenti mostrate con l'anteprima settimanale quando possibile.
- **Utenti e permessi**: amministratore e utenti con ruoli (Operatore, Consultazione, Solo Stato) o permessi a spunta, controllati anche dal server. Sostituisce `readonly_users`.
- Etichette con le iniziali maiuscole, anche nelle entità di Home Assistant (gli identificativi non cambiano).
- Le opzioni dell'add-on si riducono a quelle tecniche; le vecchie restano come "Obsoleta" solo per l'importazione.

## 0.3.1

- Citofono Hikvision (tramite l'add-on Hikvision Doorbell): le chiamate entrano nel log (ha suonato, risposta, nessuna risposta).
- Facoltativo: "Apri" dall'app Hik-Connect o dal monitor apre la porta Axis con una sola apertura a tempo; mai uno sblocco permanente.
- Nuove opzioni: nome del citofono, inoltro dell'apertura, relè, prefisso MQTT.

## 0.3.0

- Nuova interfaccia: cruscotto con accessi del giorno, grafico della settimana e avvisi; navigazione laterale (in basso sul telefono); tema chiaro e scuro.
- Scheda persona: gruppo, validità, tessere, dati, note e ultimi passaggi in un unico pannello.
- Gruppi (profili di accesso Axis): creazione, modifica ed eliminazione dall'add-on, con la fascia oraria di ciascuno.
- Fasce orarie settimanali con anteprima, modelli pronti ed esclusione dei giorni festivi; festività (anche le nazionali italiane con un clic).
- Validità temporanea delle tessere (ospiti, imprese): dal/al con scorciatoie, avviso per le tessere in scadenza.
- Ditta o reparto e note per ogni persona.
- Registro con filtri per data, separatori per giorno, istantanee della telecamera ed esportazione CSV.
- Home Assistant: entità via MQTT Discovery (stato porta, ultimo accesso, contatori del giorno, tessere da registrare), pulsanti di apertura ed entità evento per le automazioni.
- Istantanee da una telecamera di Home Assistant su accessi negati, tessere sconosciute, citofono e allarmi.
- Esportazione dell'anagrafica in CSV e importazione da CSV con anteprima.
- Registro delle attività (chi ha fatto cosa) e utenti di Home Assistant in sola lettura.
- Giornate, statistiche e CSV usano il fuso orario di Home Assistant.

## 0.2.0

- Anagrafica: persone e tessere con creazione, rinomina, sospensione ed eliminazione, scritte direttamente sull'A1210.
- Importazione automatica di utenti e tessere già presenti sul controller (all'avvio e ogni 15 minuti).
- Scheda "Da registrare": le tessere passate sul lettore e non registrate, da assegnare in un clic.
- Log degli accessi con ricerca e filtri, conservato per il numero di giorni scelto.
- Pulsanti di apertura porta (normale e a lungo), solo quando la serratura è bloccata.
- Nuove opzioni: profilo di accesso delle nuove tessere e giorni di conservazione del log.

## 0.1.2

- Tabella degli eventi leggibile sul telefono: i topic vanno a capo invece di uscire dallo schermo.

## 0.1.1

- L'indirizzo dell'A1210 funziona anche se scritto con http:// o https://.
- Il broker MQTT di Home Assistant viene trovato anche con s6-overlay; se non c'è, il registro spiega il motivo.

## 0.1.0

- Prima versione: scheletro dell'add-on con Ingress e pagina di stato (controller A1210, MQTT, eventi ricevuti).
