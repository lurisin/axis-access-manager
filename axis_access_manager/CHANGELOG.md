# Changelog

## 0.22.2

Dal collaudo del 29 ottobre nel sito nuovo, con il controller scollegato dal broker.

- **Stato della porta aggiornato anche con il controller scollegato dal broker.** Se l'app era collegata al broker ma il controller no, lo stato della porta non si rileggeva più: dopo un'apertura da Home Assistant, Stato Porta è rimasto «Aperta a Tempo» per ore. Ora, quando gli eventi del controller non arrivano in diretta, l'app rilegge la porta dal controller ogni 30 secondi, come senza broker. Prima di aprire non si fida della modalità in memoria: decide sempre il controller.
- **Porta già aperta: nessun rifiuto.** Un secondo comando Apri arrivato mentre la porta è ancora aperta a tempo (alle 20:44:38 da Home Assistant, un secondo dopo il primo) finiva nel Registro e in Home Assistant come accesso negato, «la porta non è bloccata (Aperta a Tempo)». Ora non si ripete il comando, che farebbe ripartire il tempo, e non risulta niente: chi ha premuto trova la porta aperta. Restano rifiutate le aperture con la porta sbloccata o in blocco.
- **Guasto per il controller scollegato dal broker anche con più controller.** «Eventi del Controller Non Arrivano» compariva solo se tutti i controller dell'app rispondevano: con un altro controller spento il controller principale poteva restare scollegato dal broker per ore senza nessun guasto. Ora conta solo il controller principale, quello che manda gli eventi; il guasto dice da che ora è scollegato e che i passaggi persi si recuperano quando torna collegato.
- **Schema: contatto porta e REX.** Il Sensore Porta ha il pallino della porta aperta o chiusa, e sotto i pulsanti e i relè collegati a una porta compare l'ora dell'ultima pressione registrata. Contatto porta e REX non mandano lo stato dell'ingresso come gli AUX.

## 0.22.1

Correzioni dalla revisione delle modifiche della 0.22.0.

- **Ripristino di tipologie con un controller tolto e riaggiunto.** Se una regola della tipologia indicava una porta di un controller tolto dall'app (o tolto e aggiunto di nuovo, quindi con un altro numero), l'anteprima diceva «il controller non risponde» e proponeva di ricreare la tipologia, che poi non si ricreava. Ora l'anteprima dice che alcune regole indicano porte che non ci sono più, come per le altre porte eliminate. L'avviso «il controller non risponde» compare solo quando il controller in uso non si è potuto leggere.
- **Sensore Porta nel controllo di stato.** Se il sensore porta sul controller non era collegato alla porta o la porta non usava il sensore (per esempio dopo una scrittura interrotta a metà), la pagina del controller lo segnalava ma il controllo di stato no. Ora compare anche tra i guasti, come per i pulsanti.
- **Unico amministratore con un nome utente riusato.** Se in Home Assistant l'unico amministratore cambiava nome utente e il nome vecchio passava a un'altra persona, l'amministratore perdeva i permessi. Ora li ritrova al primo accesso con il nome nuovo; chi ha preso il nome vecchio resta amministratore (serve quando Home Assistant è stato reinstallato) e Attività lo segnala, così si possono togliere i permessi dalla pagina Utenti.
- **Profili delle tipologie rifiutati dal controller.** Se dopo una modifica a nome, telecamera o videocitofono di una porta il controller rifiutava il profilo di una tipologia da aggiornare, l'errore restava solo nel log. Ora compare «Tipologie Non Aggiornate sul Controller» e l'app riprova dopo la sincronizzazione successiva.
- **Persone: il fuoco resta sulla riga.** Se un altro utente eliminava una persona che stava prima nell'elenco, all'aggiornamento automatico il fuoco della tastiera tornava all'inizio della pagina. Ora resta sulla riga scelta.
- **Esito degli eventi sempre leggibile.** Su tablet e computer gli Ultimi Eventi del cruscotto tagliavano l'esito (concesso, negato, tessera sconosciuta) con i puntini: ora la porta va sotto il nome come sul telefono e il testo va a capo. Anche nel Registro una riga lunga va a capo invece di nascondere l'esito.
- **Documentazione.** DOCS dice quali entità di Home Assistant tornano con il loro valore dopo un riavvio dell'add-on e quali restano sconosciute fino al prossimo evento del controller.

## 0.22.0

Correzioni dall'analisi completa di codice, grafica e testi del 28 ottobre.

**Porte, permessi e dati**

- **Controller tolto e riaggiunto senza porte scambiate.** Controller, porte, aree, strutture e tipologie nuove prendono sempre un numero mai usato. Prima, se si toglieva e si riaggiungeva un controller, le righe delle tipologie e i pulsanti di Home Assistant di una porta tolta passavano alla porta nuova con lo stesso numero, che così si apriva per le persone sbagliate. Ora un controller non si può togliere finché le sue porte sono nelle righe di una tipologia, e il suo Storico Modifiche si cancella con lui; pulsanti e sensori storici di Home Assistant (Apri Porta, Stato Porta) e il comando MQTT «apri» senza porta restano legati alla loro porta, e se quella porta non c'è più il comando è rifiutato.
- **Tipologie che non aprono più porte tolte.** Se dopo lo spostamento di una porta o di un controller una tipologia non comprende più nessuna porta, il profilo sul controller perde davvero quell'accesso (e si spegne se non gli resta nessuna regola), e Chi Può Entrare nel dettaglio della porta non la elenca più. Se i profili non si possono scrivere (controller che non risponde o Controller Cambiato), l'app li riscrive dopo la sincronizzazione successiva. Nel frattempo compare il nuovo guasto «Tipologie Non Aggiornate sul Controller».
- **Aree e fasce in uso non si eliminano.** Un'area o una struttura non si può eliminare se le sue porte uscirebbero da una struttura scelta nelle righe di una tipologia, che così perderebbe l'accesso, o se una tipologia comincerebbe ad aprire una porta che prima non apriva: il messaggio nomina porte e tipologie e chiede di spostare prima porte e controller. Una fascia citata nelle righe Dove e Quando (anche su un'area ancora senza porte) o scelta per un pulsante risulta in uso e non si può eliminare. Se una riga cita una fascia sparita dal controller o un luogo eliminato, il menu lo dice («Fascia Non Più sul Controller», «Porta Eliminata») e il salvataggio indica la riga da correggere.
- **Cambi di accesso tutto o niente.** Cambiare la tipologia o l'accesso di una persona è tutto o niente: se il controller smette di rispondere a metà, le tessere già scritte tornano come prima e tipologia e Attività non cambiano. Quando si salva una tipologia con un profilo in comune, Riprova sposta anche le persone rimaste indietro, e una persona con un problema non blocca più le altre ma viene indicata nel messaggio. Aggiungere o eliminare una festività con il controller che non risponde non cambia più l'elenco nell'app, così app e controller restano uguali.
- **Regole delle fasce non più perse dopo un reset.** Aprire Persone o Regole dopo un reset del controller cancellava le regole e l'opzione festivi delle fasce create dall'app. Ora restano e il Ripristino Completo le rimette, anche quelle perse con le versioni precedenti, e le festività tornano ad arrivare al controller. Se la fascia è già uguale sul controller compare la riga «Riporta le regole della fascia nell'app».
- **Tipologia non più persa con il controller spento.** Se il controller non risponde, Persone, Nuova Persona e la finestra Accesso leggono le tipologie dall'app: salvando la validità la tipologia non si perde più, e se le tipologie non si possono leggere la scheda mostra «Non letta» invece di «Nessuna». Una persona senza tipologia resta su Nessuna e le sue tessere tengono il profilo di adesso invece di prendere quello predefinito. Se si passa da una tipologia con durata a una senza durata, torna la validità di prima.
- **Persone che non si fondono e non si spezzano.** Gli utenti creati sul controller hanno sempre un codice unico (se serve si aggiunge _2, _3…). Due persone con lo stesso nome create, importate o ripristinate nello stesso secondo non finiscono più sullo stesso utente con le tessere dell'altra. Quando una persona letta da una tessera senza utente riceve un utente, gli passa anche le sue tessere: la sincronizzazione non la divide più in una persona nuova perdendo ditta, note e tipologia.
- **Togli dall'App solo per le tessere contate.** Per le tessere non presenti sul controller, «Togli dall'App» rilegge le tessere e toglie solo quelle contate nella domanda, mai «tutte». Se è in corso un'altra operazione sul controller (per esempio Riscrivi sul Controller) chiede di riprovare tra qualche secondo. Se manca più della metà delle tessere la conferma suggerisce il Ripristino Completo, e l'avviso in Persone si aggiorna da solo dopo una rilettura.
- **Utenti di Home Assistant senza nome utente.** Chi entrava da Home Assistant senza nome utente (login LDAP, OIDC o simili) riceveva tutti i permessi dell'app. Ora viene riconosciuto dal suo id di Home Assistant e parte come Solo Stato, come ogni utente nuovo. Se in Home Assistant cambia il nome utente i permessi seguono la persona, il nome dato nella pagina Utenti non viene più sovrascritto e Nuovo Utente propone Solo Stato, il ruolo dei permessi già spuntati (prima mostrava Amministratore).
- **Permessi più stretti.** Sbloccare un backup è ora riservato agli amministratori, perché un backup sbloccato può essere eliminato dalla ritenzione al backup successivo; chi ha il permesso Backup può ancora bloccarlo, e ogni backup eliminato dalla ritenzione compare in Attività. La presa visione vale solo per le notifiche che l'utente vede, quindi chi ha solo il permesso Stato non può più nasconderne altre a tutti; in compenso vede gli allarmi delle porte nella barra in alto e in Da Gestire, dove prima leggeva «Sistema OK» anche con una porta forzata. La telecamera di porte e controller deve essere un'entità camera.* valida: un valore diverso non si salva, e uno già salvato non si usa per le foto.
- **Indirizzo e password del controller.** Un indirizzo del controller che l'app non sa usare (porta non numerica, parentesi sbagliate) viene rifiutato al salvataggio, e a un IPv6 scritto senza parentesi le aggiunge l'app. Un indirizzo sbagliato già salvato non impedisce più all'add-on di avviarsi: il controller risulta non raggiungibile e l'indirizzo si corregge da Impianti. Se cambi l'indirizzo o l'utente di un controller devi scrivere di nuovo la password: quella salvata non viene più mandata a un indirizzo nuovo, né quando salvi né con Prova Collegamento.
- **Database dopo uno spegnimento improvviso.** Un database rimasto a metà di una scrittura veniva scambiato per danneggiato, e l'app tornava alla copia dell'ultimo backup di Home Assistant perdendo il lavoro fatto dopo: ora SQLite lo rimette a posto da solo. Se il database è davvero danneggiato o manca, l'app prepara prima la copia accanto al file, così con il disco pieno non riparte più vuota, e il file danneggiato resta da parte insieme al suo journal. Il nuovo tipo di notifica Database dell'Add-on lo segnala con «Database Ripristinato dalla Copia» o «Database Non Ripristinato», e con «Copia per il Backup Non Aggiornata» quando non si è potuta preparare la copia per il backup di Home Assistant.
- **Journal WAL e copia a blocchi.** Il database usa il journal WAL e la copia per il backup di Home Assistant si fa a blocchi: anche con un database grande non blocca più per secondi passaggi di tessera, aperture e pagine. Se la copia non riesce resta quella precedente e il backup di Home Assistant non fallisce. I backup di Home Assistant non contengono più le copie fatte prima degli aggiornamenti né i database danneggiati.

**Sincronizzazione, eventi e registro**

- **Sincronizzazione che non si ferma più.** Prima, dopo un errore imprevisto (per esempio il numero di una tessera passato a un'altra tessera sul controller, o una foto vecchia che non si riesce a cancellare), la sincronizzazione delle tessere e la pulizia del registro si fermavano fino al riavvio. Ora l'errore va nel log e l'app riprova dopo un minuto. Le riparazioni dell'avvio (fasce, festività, tipologie, permesso dei pulsanti) sono indipendenti, si ritentano e non danno più «Controller Non Raggiungibile» per sbaglio; subito prima di ogni sincronizzazione l'app controlla chi risponde all'indirizzo, così un controller sostituito non viene letto prima che compaia Controller Cambiato.
- **Eventi persi recuperati davvero.** Dopo un'interruzione del broker o del controller gli eventi persi si recuperano dal registro del controller anche se nel frattempo è arrivato un passaggio in diretta, e un evento con l'ora nel futuro non blocca più i recuperi. Un messaggio MQTT con l'ora malformata non ferma più l'ascolto degli eventi, che prima si portava dietro anche i pulsanti Apri di Home Assistant e il videocitofono. Con più porte, gli eventi recuperati all'avvio e le letture delle tessere vanno sulla porta giusta.
- **Più leggero con registri grandi.** La sincronizzazione scrive solo le tessere cambiate, con un solo salvataggio, e il recupero degli eventi salva una volta per pagina; l'ultimo passaggio di ogni porta si legge con un indice. Niente più blocchi di secondi su scheda SD o con registri grandi. Il CSV del Registro contiene tutti gli eventi richiesti e non si ferma più in silenzio a 50.000 righe.
- **Registro: ricerca e filtri.** La ricerca trova anche il nome della porta e chi ha aperto da Home Assistant, dai pulsanti o dal videocitofono. Apri nel Registro dalla scheda di una persona e Tutti i Suoi Eventi filtrano per persona e non per nome: comprendono gli eventi di prima di un cambio di nome, non mescolano gli omonimi, e lo stesso filtro vale per il conteggio e per il CSV. I giorni scelti si calcolano con l'ora di Home Assistant e sono giusti anche nel giorno del cambio dell'ora, quando prima gli eventi tra le 23:00 e le 24:00 restavano fuori dalla lista e dal CSV.
- **Registro: elenco e conteggi.** Dopo Carica Altri l'aggiornamento automatico tiene gli eventi già caricati, e un doppio clic non duplica più le righe. Foto e dettagli arrivati dopo la riga compaiono anche nel Dettaglio Evento aperto. Il riquadro degli accessi riusciti non mostrati conta come il cruscotto, e l'ultimo accesso della persona conta solo i passaggi concessi, non più le letture di una tessera sospesa, scaduta o fuori fascia. Con una porta sola il coperchio aperto e il controller inclinato non contano più come allarmi della porta, e i conteggi di oggi e degli ultimi 7 giorni partono dalla mezzanotte giusta anche al cambio dell'ora.

**Backup e ripristino**

- **Ripristino di persone e tipologie.** Riporta com'era di una persona non toglie più le tessere aggiunte dopo il backup: per ognuna c'è una riga «Elimina la tessera …» non spuntata. Una tipologia del backup si riconosce dal numero solo se ha anche lo stesso nome o lo stesso profilo, le porte si riconoscono dal token e aree e strutture dal nome. Così una tipologia nuova con il numero di una eliminata non diventa più quella vecchia, e una persona tiene la sua tipologia anche se è stata rinominata. Se il profilo di una tessera non c'è più, l'anteprima dice quale accesso avrà, e la tipologia predefinita non si usa mai in silenzio.
- **Anteprima che dice cosa farà.** Porte e Lettori mostra solo le differenze che il ripristino scrive davvero, e per serratura e contatto porta rimanda al Ripristino Completo. Se un elenco non si legge compare una riga bloccata «dati non letti», invece di far sembrare il controller appena resettato o le porte sparite. Se il controller smette di rispondere prima della conferma, le righe risultano non riuscite con il motivo, e la finestra dice «Ripristino Non Riuscito» se non ne riesce nessuna. Nel Confronta e nei riassunti un controller che non risponde non compare più come tolto e l'app non aspetta più che scada il tempo.
- **Ripristino Completo ripetibile.** L'app ricorda i token nuovi dati dal controller: un secondo Ripristino Completo dallo stesso backup non ricrea doppioni, e Storico Modifiche e Registro passano al token nuovo. Il backup da cui si ripristina non viene più eliminato dalla ritenzione nell'ora successiva. Porte e Lettori si applica prima delle tipologie, così una tipologia che indica una porta rimessa si ripristina al primo tentativo.
- **File dei backup più robusti.** Un file di backup rovinato (per esempio vuoto dopo una mancanza di corrente) non viene più riusato ma riscritto, e torna sano anche nei backup vecchi che lo usano. Con l'ultimo backup illeggibile non nasce più un «Primo backup» ogni ora. Un backup con l'indice illeggibile si può eliminare dall'avviso Backup Rovinati (solo amministratori), così il guasto si toglie. Al ritorno all'ora solare non nasce più un backup in più, e in Impostazioni → Backup se si scrive 0415 come orario notturno si salva 04:15.

**Importazione ed esportazione CSV**

- **Colonna stato e file ANSI.** L'importazione legge la colonna stato: con «sospesa» la tessera viene creata, aggiunta o portata sospesa, e sul controller è scritta già sospesa. Con «attiva» una tessera sospesa nel frattempo non si riattiva. I file salvati da Excel in ANSI si leggono senza rovinare le lettere accentate («Niccolò» non diventa più «Niccol�»), e una riga con caratteri che non si leggono diventa un errore, con l'invito a salvare il file come CSV UTF-8.
- **Reimportare senza perdere dati.** In un file senza intestazione le celle vuote lasciano com'erano validità, ditta e note: un elenco «nome;tessera» non svuota più i dati e non riapre più una tessera scaduta. L'anagrafica esportata si reimporta così com'è, con le date con i secondi. Le tessere fatte di sole cifre restano testo in Excel e LibreOffice, e i testi che iniziano con = + - @ non diventano formule. Una tessera nuova di una persona già registrata applica tipologia, validità, ditta e note della riga, che l'anteprima mostra prima della conferma.

**Controller, fasce e festività**

- **Ingressi programmati a metà.** Un ingresso rimasto programmato a metà (controller fermo durante la scrittura), o i cui oggetti sono spariti dal controller (reset, backup più vecchio, modifiche dal sito del controller), compare come «Ingresso da Riscrivere» nelle notifiche, nella pagina del controller e in giallo nello Schema. Basta riaprirlo e premere Scrivi sul Controller, anche senza cambiare niente. Dopo Togli Controller e una riaggiunta, una reinstallazione o un Ripristino Completo, all'avvio l'app non toglie più il permesso di apertura a pulsanti e relè che non conosce, e quando si riscrive un ingresso riprende gli oggetti già creati sul controller invece di farne dei doppioni.
- **Fuso orario del controller.** Ogni 5 minuti l'app confronta il fuso orario del controller con quello di Home Assistant: oggi, d'inverno e d'estate. Se le ore non coincidono (per esempio con un controller resettato rimasto su UTC) compare il guasto «Fuso Orario del Controller», perché fasce e festività scatterebbero all'ora sbagliata. Nella scheda Stato il pulsante «Usa il Fuso di Home Assistant» scrive nel controller il fuso di Home Assistant.
- **Fascia Sempre solo da rinominare.** L'app usa la fascia Sempre per le tipologie con fasce diverse per porta e per i pulsanti. Ora resta valida tutti i giorni 00:00–24:00 anche nei festivi: Modifica cambia solo nome e descrizione, e il pulsante Elimina non c'è più. Se una versione precedente l'aveva cambiata la scheda lo dice e Ripristina la rimette com'era, e una tipologia con fasce diverse non si salva se Sempre manca dal controller.
- **4 ottobre e festività doppie.** Le Festività Nazionali Italiane comprendono il 4 ottobre (San Francesco d'Assisi, festa nazionale dal 2026): a chi le aveva già importate viene aggiunto una volta sola all'avvio, con una voce in Attività, e anche il controller viene aggiornato. Ricliccando le festività nazionali in un altro anno non si duplicano più, e quelle già doppie si uniscono all'avvio. Eliminando una festività annuale si toglie davvero quel giorno ogni anno; il 1° maggio si chiama Festa dei Lavoratori.
- **Schema e Storico Modifiche.** Nello Schema, cambiare la direzione di un lettore non riattiva più in silenzio un punto di accesso disattivato, e i collegamenti non salvati restano anche dopo aver scritto un ingresso. Un collegamento verso una porta tolta fuori dall'app non blocca più il salvataggio. Nello Storico Modifiche Annulla compare solo sulla modifica più recente di ogni oggetto, perché annullarne una più vecchia cancellava in silenzio quelle successive; porte e collegamenti dicono cosa è cambiato con i nomi di porte e lettori, senza i codici Axis.
- **Pagine del controller.** Con il controller che non risponde la pagina si apre dopo un solo tentativo, in una decina di secondi invece di più di un minuto, con lo Storico Modifiche subito visibile. Salvare la Connessione quando le strutture non si leggono non toglie più l'area al controller. Se si sposta una telecamera o un videocitofono e la nuova posizione viene rifiutata, la porta di prima resta com'era. In Nome e Tempi un tempo lasciato vuoto non diventa più 0, e nella finestra di un ingresso Contatto e Linea Supervisionata spariscono per Non Usato e Ingresso di Allarme, dove non hanno effetto.

**Home Assistant, MQTT e videocitofono**

- **Nessun Broker MQTT.** Se all'avvio non c'è un broker (per esempio perché Mosquitto è stato installato dopo), l'app lo cerca ogni minuto e quando lo trova avvia da sola l'ascolto degli eventi, senza riavviare l'add-on. Nel frattempo la barra in alto non dice più «Sistema OK»: mostra il nuovo guasto «Nessun Broker MQTT», che sparisce da solo quando l'app trova il broker. Un prefisso MQTT di Home Assistant non valido, o uguale a quello del controller, non ferma più gli eventi: l'app usa un prefisso di riserva e la riga Topic di Impostazioni mostra quello in uso.
- **Entità di Home Assistant.** Quando l'add-on si ferma o si riavvia, le entità diventano non disponibili invece di restare con l'ultimo stato; una modifica in Impianti non le rende più non disponibili per un attimo, così le automazioni non ripartono da sole. Il sensore «Controller Online» si chiama ora «Controller Collegato al Broker», che è quello che indica (l'identificativo dell'entità non cambia). Salvare una porta o il controller non rende più sconosciuti allarmi, coperchio e ingressi. Le entità di una porta tolta e i pulsanti Apri spenti spariscono davvero da Home Assistant, anche quelli rimasti dalle versioni precedenti, e il dispositivo Controllo Accessi tiene sempre lo stesso identificativo. Accessi Oggi e Accessi Negati Oggi tornano a zero a mezzanotte anche senza eventi.
- **Aperture da Home Assistant e dal videocitofono.** Dopo un'interruzione MQTT, e prima di rifiutare un'apertura, l'app rilegge dal controller la modalità della porta: le aperture non vengono più rifiutate con «Aperta a Tempo» quando la porta è già chiusa. Salvare una porta non interrompe più un'apertura in corso, e due comandi Apri ravvicinati aprono una volta sola. Un'apertura non riuscita compare nel Registro e in Home Assistant come accesso negato, con il motivo e il nome di chi ha premuto proprio quel pulsante.
- **Videocitofono su due porte.** Con lo stesso videocitofono su due porte (relè 1 e 2), la chiamata, i secondi passati dalla chiamata e lo stato nella scheda Stato valgono per tutte e due. Un'apertura dal relè 2 non lascia più «nessuna risposta» sulla prima porta. Con la spunta «"Apri" dal Videocitofono Apre Questa Porta» spenta, un'apertura dall'app o dal monitor durante la chiamata conta come risposta.
- **Foto che non si salvano.** Allarmi, accessi negati e chiamate arrivano subito a Home Assistant anche quando la foto non si può salvare (disco pieno, cartella Media non scrivibile), e il problema compare nella scheda Telecamere e nelle notifiche. La foto si chiede solo per entità camera.* e si salva solo se arriva davvero un'immagine.

**Notifiche**

- **Porte Che Non Si Aprono.** Punti di Accesso Non Gestiti, Pulsanti Senza Permesso di Apertura, Ingresso da Riscrivere e Tipologie Non Aggiornate sul Controller sono ora un tipo di notifica a sé, attivo di serie e con l'icona del lucchetto. Chi ha disattivato Controller Non Raggiungibile non perde più questi avvisi. Le notifiche nominano il pulsante da usare (Fai Gestire al Controller, Dai il Permesso), e Dettagli compare solo se porta a una pagina che l'utente può aprire.
- **Guasti già visti.** La presa visione dei guasti non si perde più a ogni riavvio o aggiornamento. Un guasto già visto torna da gestire se peggiora, per esempio se spariscono altre tessere o se lo scarto dell'orologio supera i 10 minuti e poi l'ora. Allarmi e tentativi ripetuti già visti non tornano da gestire quando il primo evento esce dalle 24 ore. I guasti visti ma ancora in corso compaiono in Guasti Ancora in Corso nella pagina Notifiche, e finché ce ne sono Da Gestire non dice più «Tutto in Ordine».
- **Notifiche coerenti con il resto dell'app.** Tessere in Scadenza segnala le tessere che scadono nei prossimi 7 giorni, come il cruscotto (prima erano 3), e non comprende più quelle non ancora valide. Cruscotto, Impianti e notifiche usano la stessa regola per il controller. Risulta Non Raggiungibile solo quando lo dice il controllo periodico, e gli eventi risultano In Arrivo solo se anche il controller è collegato al broker; se non lo è, il pallino in Impianti è giallo. Allarmi e manomissioni ripetuti dicono «3 volte in 12 minuti» invece di «3 tentativi».

**Interfaccia**

- **Testi, parole e maiuscole.** La direzione dei lettori si chiama ovunque Entrata o Uscita, per non confonderla con gli ingressi del controller, e ogni cosa ha sempre lo stesso nome: Videocitofono per il dispositivo Hikvision, foto, accessi negati, Togli Controller (prima Rimuovi Controller) come per porte e telecamere, Chiamata Citofono e AUX 1 nel Registro. Le etichette sono in Title Case con articoli e preposizioni minuscoli («Non sul Controller», «Backup Prima delle Modifiche», «Segna Tutti come Visti»), i giorni del Registro hanno la maiuscola («Lunedì 26 ottobre 2026»), gli stati vuoti sono frasi con il punto e i singolari sono giusti («1 riga», «1 giorno»). I messaggi d'errore sono in italiano e iniziano con la maiuscola, senza testi tecnici in inglese, codici Axis o «errore 422», e Attività mostra i nomi di fasce, tipologie e permessi invece dei codici, con le date in ora locale.
- **Colori e tema scuro.** Nel tema scuro il testo sul rosso (contatori del menu, pulsanti Elimina, toast d'errore) si legge bene, e la barra Scarta/Salva dello Schema si distingue dalla pagina. Le etichette delle tipologie si leggono in tutti i colori, e nel tema chiaro verde e giallo sono più scuri. Lo stesso stato ha lo stesso colore ovunque: gravità Bassa grigia; Backup Rovinati, orologio sbagliato e Disattivato gialli; Doppia Chiusura e Bloccata da Regola verdi come Bloccata; l'etichetta del controller è gialla se gli eventi non arrivano. I bordi dei campi sono più marcati e i campi disattivati sono grigi.
- **Telefono.** Le liste con etichetta e valore (Stato del controller, Database, Videocitofoni) mettono l'etichetta sopra il valore, che prima andava a capo lettera per lettera. Nel Registro si legge di nuovo l'esito. Il menu Cambia Porta non esce più dallo schermo e le porte nella scheda Stato mostrano il loro stato. I nomi dei riquadri con i numeri del cruscotto e la ricerca delle Notifiche non vengono più tagliati, e i nomi lunghi nello Schema vanno a capo con il trattino.
- **Tante piccole correzioni.** Una pagina lenta non sovrascrive più quella aperta dopo, e mentre salva una finestra non si chiude più con × o Esc. Un link dentro una finestra chiude la finestra, e la barra in alto non perde più il focus e i clic a ogni aggiornamento. La finestra Il Controller Non Ha Risposto compare solo quando il controller non risponde davvero, e quando l'add-on si sta riavviando il messaggio lo dice invece di «errore 502». Fasce Orarie e Tipologie mostrano «Dati Non Aggiornati» sopra un'ultima lettura vecchia, una tessera scaduta o non ancora valida non compare più come «Attiva» in verde, e le intestazioni Oggi e Ieri del Registro si aggiornano dopo mezzanotte.

**Documentazione**

- **Documentazione aggiornata.** La documentazione si apre con una guida alla prima installazione: Aggiungi Controller con il Prefisso MQTT, il client MQTT dell'A1210 (topic degli eventi, stato online/offline, filtro eventi), le porte, le telecamere e i videocitofoni. Le opzioni sono citate con il nome della scheda Configurazione (Broker MQTT, Conservazione Registro…). Sono descritti Festività, Da Registrare con Ignora, Togli dall'App, Rileggi dal Controller, i riquadri del cruscotto e tutti i guasti con la loro gravità, e la spunta «"Apri" dal Videocitofono Apre Questa Porta», spenta di serie. Il README elenca cosa fa l'add-on oggi invece di fermarsi alla 0.3, e le vecchie sezioni «Novità» sono sostituite da note brevi per chi aggiorna da versioni vecchie.

**Da provare sul controller vero**

- **Tessere importate «sospesa».** Ora vengono scritte sul controller già disattivate (Enabled falso, stato Disabled), invece di essere create attive e sospese subito dopo. Da verificare che l'A1210 le accetti così e le neghi fin dal primo passaggio.
- **Tipologia rimasta senza porte.** Il suo profilo viene riscritto senza regole e spento (Enabled falso). Da verificare che il controller lo accetti e che le tessere di quella tipologia vengano negate.
- **Modalità della porta dopo un'interruzione.** Dopo un'interruzione MQTT, e prima di rifiutare un'apertura, l'app rilegge la modalità della porta con GetDoorState. Da verificare che un'apertura da Home Assistant vada a buon fine se la porta si è richiusa mentre il broker era fermo. Allarme e manomissione non si rileggono, perché i nomi dei loro campi nella risposta dell'A1210 non si possono verificare senza il controller.
- **Messaggi MQTT conservati.** All'avvio dopo l'aggiornamento l'app toglie da Home Assistant le entità rimaste, leggendo le configurazioni che il broker conserva (retained), e allo spegnimento pubblica «offline» conservato. Da verificare con Mosquitto e Home Assistant veri tre cose: che le entità vecchie spariscano, che le altre risultino non disponibili durante l'aggiornamento e che lo stato «online» del controller resti com'è.
- **Fuso orario.** Il confronto e il pulsante «Usa il Fuso di Home Assistant» sono stati provati solo con un controller simulato su Europe/Rome. Da verificare con un controller resettato rimasto su UTC che compaia il guasto e che il controller accetti il fuso scritto.
- **Ingressi ripresi dopo una riaggiunta.** Quando si riscrive un pulsante o un relè dopo Togli Controller o una reinstallazione, l'app riprende gli oggetti che aveva già creato sul controller. Da verificare che non nascano doppioni e che «Ingresso da Riscrivere» non compaia per ingressi a posto. Per pulsanti e relè l'app non controlla il collegamento al morsetto, perché il controller vero potrebbe indicarlo in modo diverso.

## 0.21.7

- **Ingressi AUX dei pulsanti giusti in Home Assistant.** Dai tempi al millisecondo del banco: un AUX collegato come Pulsante Uscita o Relè Citofono il controller lo manda al contrario degli ingressi classici (1 a riposo, 0 mentre si preme: il REX scatta 40-50 ms dopo il passaggio a 0), e dopo l'avvio manda 0 fino alla prima pressione. Ora per questi ingressi l'app inverte la lettura e ignora il primo valore finché non vede un cambio; "Ingresso AUX 1" non resta più acceso a riposo. Gli ingressi classici (AUX non collegati, Manomissione, Allarme) restano come prima. Vale anche per lo stato in tempo reale nella pagina del controller.

## 0.21.6

- **"Più pressioni" solo per pressioni vere.** All'avvio l'app recupera dal controller gli eventi persi e rilegge anche gli ultimi già registrati: al banco un solo pulsante negato era stato segnato "più pressioni" perché la sua copia recuperata contava come una seconda pressione. Ora lo stesso evento arrivato due volte (via MQTT e dal registro del controller) non viene più contato. Vale anche per coperchio e inclinazione.
- **Niente più avvisi sugli AUX usati come pulsante.** Con i tempi al millisecondo il banco ha visto che l'AUX non si blocca: il controller apre 40-50 ms dopo ogni pressione, prima e dopo il presunto blocco. Era la lettura dello stato dell'ingresso a essere al contrario (a riposo 1, premuto 0). Tolti l'avviso "Ingresso Bloccato" e il consiglio di non usare gli AUX per i pulsanti, arrivati con la 0.21.4.

## 0.21.5

Ritocchi dal collaudo a distanza della 0.21.4.

- **Contatto Porta subito giusto.** Il controller manda lo stato del contatto porta solo quando cambia: dopo l'aggiornamento al banco l'entità restava "sconosciuto" fino alla prima apertura. Ora l'app lo legge dal controller all'avvio, dopo ogni interruzione e dopo aver programmato un ingresso.
- **Uscita nel Registro.** L'apertura da un Pulsante Uscita compare come "Uscita" (nuovo tipo, anche nell'evento di Home Assistant come `uscita`), non più come Apertura Remota. Il Relè Citofono resta Apertura Remota, perché apre a distanza dal citofono. Le aperture dai pulsanti già registrate con la 0.21.4 diventano Uscita. Accessi Oggi conta anche le uscite.
- **Pulsante negato con il nome dell'ingresso** nella riga del Registro e nel Cruscotto: prima si vedeva solo "Accesso Negato".

## 0.21.4

Correzioni dal collaudo sul posto del 26 ottobre, con pulsanti premuti davvero, contatto porta e coperchio.

- **Pulsanti e relè nel Registro con il nome dell'ingresso.** Il controller manda il punto di accesso nel topic MQTT e non nei dati: l'app non riconosceva l'ingresso e registrava un "accesso concesso" dal controller, e Ultimo Accesso restava "nessuno". Ora le sorgenti del topic (punto di accesso, porta) fanno parte dei dati dell'evento.
- **Pulsante negato non è più una "Tessera Sconosciuta"** nelle notifiche e nel Cruscotto: un negato senza tessera compare come Accesso Negato, con l'ingresso e il motivo.
- **Pressioni ripetute accorpate.** Più pressioni dello stesso ingresso entro 10 secondi fanno una riga sola nel Registro ("più pressioni"); al banco un ponticello aveva dato 4 righe in 5 secondi. Coperchio aperto e chiuso di continuo e **controller inclinato** (nuovo) fanno una riga di Manomissione ogni 5 minuti.
- **Sensore Porta**: il ruolo sul contatto porta è attivo. Scrive il DoorMonitor della porta e collega l'ingresso come provato al banco; un contatto impostato a mano viene riconosciuto.
- **Contatto Porta** in Home Assistant, per ogni porta: aperta o chiusa secondo il contatto magnetico.
- **Avviso "Ingresso Bloccato"** quando un pulsante o un relè resta attivo per più di un minuto (al banco un AUX è rimasto bloccato dopo la prima pressione fino al riavvio), e un consiglio in Impianti: per i pulsanti è meglio l'ingresso REX della porta.
- **Confronto dei backup** con la configurazione delle porte (serratura, relè, contatto porta), una riga per voce cambiata, anche per i lettori.

## 0.21.3

Correzione dall'analisi della documentazione Axis sul REX: il pulsante di uscita e il relè del citofono venivano negati.

- **Permesso di apertura per pulsanti e relè**. Per l'A1210 una pressione è una richiesta di accesso senza tessera, e il controller apre solo se un profilo di accesso con il modo REXOnly ha una regola sul punto di accesso dell'ingresso (come nell'esempio Axis). L'app non lo creava, così il controller rispondeva "profilo di accesso non valido" e la porta non si apriva. Ora l'app crea e mantiene il profilo **Uscite e Citofono**, con fascia Sempre e una regola per ogni ingresso con ruolo Pulsante Uscita o Relè Citofono.
- **Ingressi già programmati riparati all'avvio** e dopo un Ripristino Completo, con una voce in Impostazioni → Attività.
- **Avviso "Pulsanti Senza Permesso di Apertura"** se un ingresso resta senza regola, con il pulsante **Dai il Permesso** nella pagina del controller. I pulsanti di riparazione della pagina sistemano insieme punti di accesso non gestiti e permessi.
- Il profilo non compare tra i gruppi e non si assegna alle tessere.
- Su telefono gli avvisi della pagina del controller mettono il pulsante sotto il testo.

## 0.21.2

Rifiniture dopo la seconda prova di reset al banco e richieste dell'utente.

- **Sensore Elettroserratura Porta** in Home Assistant, uno per porta: acceso/spento con classe lock (spento bloccata, acceso sbloccata, anche quando è aperta a tempo da una tessera). Nelle dashboard mostra il lucchetto.
- **Tessere rilette appena il controller torna raggiungibile** dopo un riavvio o un reset: l'avviso delle tessere non presenti e il Ripristino Completo compaiono subito, senza premere Rileggi dal Controller né aspettare il giro dei 15 minuti.
- **Pallino verde** accanto a "Raggiungibile" nella pagina del controller (era grigio).
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
- **Backup**: i lettori si contano senza gli ingressi (pulsante di uscita, relè del citofono), che hanno il loro numero. Il backup fatto prima di un ripristino si chiama "Prima del Ripristino del Backup del 25/10/2026 19:05", senza catene di nomi, e ogni riga mostra sempre il tipo.
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

- **Tessere sparite dal controller**: non vengono più cancellate dall'app. Restano in Persone con l'etichetta "Non sul Controller" e un avviso in cima all'elenco; da lì le riscrivi sul controller con gruppo e validità salvati, oppure le togli dall'app. Ditta, note e tipologia non si perdono più dopo un reset. Se sparisce più di una piccola quota delle tessere, o tutte, il guasto è critico.
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
- Presa Visione e Segna Tutte come Viste finiscono in Impostazioni → Attività, con l'utente e le notifiche viste.
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
  - Sotto, il **Registro Eventi** delle ultime 24 ore, con le schede Tutte, Accessi Negati e Allarmi e Manomissioni, il conteggio di ciascuna e la colonna Stato (Da Gestire o Visto). La spunta "Solo da Gestire" filtra le righe.
  - Ricerca per persona, tessera o porta, filtro per porta se ce n'è più di una, e "Segna Tutte come Viste".
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
- Una sola istantanea per passaggio, sull'esito: accesso negato sempre, accesso concesso con "Foto Anche sugli Accessi Concessi". La lettura della tessera e la tessera sconosciuta non scattano più una seconda foto uguale.
- Istantanea anche sulle aperture a distanza (pulsanti di Home Assistant e citofono), anche senza "Foto Anche sugli Accessi Concessi".
- Il nome del file usa l'ora dell'evento data dal controller, non l'orologio di Home Assistant.
- Su computer le pagine occupano tutta la larghezza dello schermo: prima si fermavano a 1180 pixel e a destra restava spazio vuoto.

## 0.11.3

- Impianti: nella scheda del controller la scritta "Collegato" lascia il posto a un pallino accanto al nome. Verde: il controller risponde e manda gli eventi. Giallo: risponde, ma gli eventi MQTT non arrivano. Rosso: non risponde. Passando sopra al pallino si legge il dettaglio.

## 0.11.2

- Pagina della porta, scheda Dispositivi, con testi più chiari:
  - i lettori dicono da che lato della porta sono ("Lettore tessere in entrata");
  - pulsanti e relè collegati agli ingressi del controller mostrano ruolo e morsetto ("Pulsante Uscita · morsetto AUX 1 del controller") invece di "Uscita · Ingresso IO1";
  - il videocitofono dice se apre la porta e con quale relè;
  - i bollini "Attivo" e "Apre la Porta" sono stati tolti: compare un bollino solo per un lettore disattivato.
- Impianti: la scheda del controller conta a parte lettori e ingressi usati ("1 lettore · 2 ingressi"); prima pulsanti e relè sugli ingressi risultavano lettori.

## 0.11.1

- Scheda della persona: nel riquadro Accesso Personalizzato il nome della tipologia resta nella frase invece di andare a capo.
- Impostazioni → Attività: creazione, modifica ed eliminazione degli utenti mostrano il nome visualizzato invece del nome utente.

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
- Registro: le aperture compaiono con il nome dato all'ingresso (ad esempio "Pulsante Portineria"), con ruolo e morsetto nel dettaglio ("Pulsante Uscita · AUX 1").
- Home Assistant: le entità degli ingressi AUX, Manomissione e Allarme prendono il nome scelto nell'app.

## 0.8.4

- Registro e Notifiche: anche i dinieghi salvati prima della 0.8.2 compaiono con il motivo in italiano ("fuori fascia oraria" invece di "NotPermittedAtThisTime"); la traduzione si fa una volta all'avvio.
- Controller → Stato: **Porte sul Controller** mostra i nomi delle porte invece dei codici interni (il codice resta nel suggerimento al passaggio del mouse).
- Telefono: nelle Notifiche i pulsanti Apri e Presa Visione vanno sotto il testo (prima il titolo si spezzava lettera per lettera); nell'elenco Porte lo stato passa sotto il nome.
- Impostazioni → Esporta e Importa: il testo spiega che le tessere già registrate vengono aggiornate, non più saltate.

## 0.8.3

- Porte: **Accessi Oggi** conta come il cruscotto (accessi concessi e aperture remote). Prima sommava anche la lettura della tessera che precede ogni accesso e le chiamate al citofono, e il numero risultava quasi doppio.

## 0.8.2

- **Correzione importante:** i gruppi creati o modificati dall'add-on non facevano entrare nessuno, nemmeno con la fascia "Sempre" (il controller rispondeva "fuori fascia oraria"). L'A1210 controlla la fascia oraria sia nelle regole sia nel profilo, e l'add-on la scriveva solo nelle regole. Ora la scrive in entrambi; i gruppi già salvati si riparano da soli al primo avvio dopo l'aggiornamento (in Impostazioni → Attività compare "gruppo riparato").
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
- Scheda "Da Registrare": le tessere passate sul lettore e non registrate, da assegnare in un clic.
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
