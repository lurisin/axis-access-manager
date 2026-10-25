# Changelog

## 0.8.0

- Nuove entità in Home Assistant, per poter togliere il vecchio package YAML:
  - per ogni porta, **Allarme**, **Preallarme** e **Manomissione** come sensori acceso/spento, con il tipo e l'ora dell'ultimo cambio negli attributi;
  - per il controller, **Controller Online**, **Coperchio Controller Aperto** e **Ultimo Avvio Controller**;
  - gli ingressi I/O del controller: **Ingresso AUX 1**, **Ingresso AUX 2**, **Ingresso Manomissione** e **Ingresso Allarme**.
- Tolte dalla scheda Configurazione le vecchie opzioni "Obsoleta" (fra cui la password dell'A1210): da tempo si configura tutto in Impianti.

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
