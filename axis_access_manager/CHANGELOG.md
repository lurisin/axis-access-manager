# Changelog

## 0.15.3

- **Barra in alto più snella**, al posto della riga di riquadri con i numeri di oggi.
  - **Guasti** (controller non raggiungibile, MQTT perso, telecamera che non fa la foto) e **Allarmi** (porta forzata o aperta troppo a lungo, manomissioni non ancora viste) compaiono solo quando ci sono, in tutte le pagine. Altrimenti si vede **Sistema OK**.
  - Un clic apre l'elenco a tendina, con il collegamento alle Notifiche. Dagli Allarmi puoi segnarli tutti come visti. I guasti restano finché non si risolvono.
  - Nel cruscotto, accanto al titolo, la data di oggi e due badge piccoli: accessi e negati di oggi. Il clic sui negati apre il Registro con la vista Solo Negati.
- Pagina Notifiche più compatta: "Ultime 24 ore" va accanto al titolo, ricerca e pulsanti stanno sulla riga di Da Gestire (sul telefono restano solo le icone), la ricerca non si allarga più su tutta la riga.
- La linea sotto la barra in alto compare solo quando la pagina scorre.
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
