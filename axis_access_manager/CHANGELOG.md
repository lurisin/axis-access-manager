# Changelog

## 0.11.2

- Pagina della porta, scheda Dispositivi, con testi più chiari:
  - i lettori dicono da che lato della porta sono ("Lettore tessere sul lato entrata");
  - pulsanti e relè collegati agli ingressi del controller mostrano ruolo e morsetto ("Pulsante Uscita · morsetto Aux 1 del controller") invece di "Uscita · Ingresso IO1";
  - il videocitofono dice se apre la porta e con quale relè;
  - i bollini "Attivo" e "Apre la Porta" sono stati tolti: compare un bollino solo per un lettore disattivato.

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
