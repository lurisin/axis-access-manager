# Changelog

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
