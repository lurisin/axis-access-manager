# Prove al banco sull'A1210

`prove_a1210.py` esegue sul controller **di banco** le prove T1-T12, T16 e T18 del passaggio «l'app è l'unica
fonte di verità» e scrive un rapporto da mandare a chi sviluppa l'add-on. Usa il client VAPIX dell'add-on
(`axis_access_manager/app/vapix.py`), così le richieste sono quelle che manderà l'app. T18 senza `--limiti` è
parziale: misura solo i tempi con molte tessere, e il rapporto lo dice.

Il kit sta fuori dall'add-on: l'immagine copia solo `axis_access_manager/app`, quindi non finisce mai in Home
Assistant. **Non usarlo sul controller dell'impianto.**

## Cosa serve

- Python 3.12 e httpx (`pip install httpx==0.28.1`), in una copia del repository: il kit importa il client
  dall'add-on.
- Un A1210 di banco appena resettato, raggiungibile dal PC, con il numero di serie letto sull'etichetta.
- Per T4 e T8: una tessera e un lettore. Se sul controller non c'è un lettore configurato, il kit chiede di
  configurarlo con la procedura guidata della pagina del controller.

## Come si lancia

```
python3 tools/banco/prove_a1210.py --host 192.168.0.90 --serie B8A44FE6A074 --tessera c0110b18
```

La password la chiede il kit (oppure `AAM_BANCO_PASSWORD`). Senza `--prova` fa tutte le prove: prima T11, che
vuole il controller appena resettato, poi quelle che non usano il lettore, poi T4 e T8, che chiedono di configurarne
uno, e per ultima T12, con il reset. Con `--prova T7 --prova T11` solo quelle. `--non-interattivo` salta le parti
che chiedono all'operatore (T2, T8, T12 e le domande di T4, T16, T18). `--quante 100` cambia le tessere di T18
(di serie 500).

### T18 con `--limiti`

```
python3 tools/banco/prove_a1210.py --host 192.168.0.90 --serie B8A44FE6A074 --prova T18 --limiti
```

Dopo i tempi, T18 cerca i limiti del controller per `hardware.LIMITS`: scrive oggetti `aam_prova_` uno alla volta,
a blocchi, finché il controller risponde `CapabilityViolated` o finché arriva al tetto, e dopo ogni ricerca li toglie
con la stessa pulizia di sempre. Le ricerche, con il tetto di serie:

| nome | cosa scrive | tetto |
|---|---|---|
| `utenti` | utenti senza tessere | 2000 |
| `tessere` | tessere, con gli utenti appena scritti e un profilo senza regole | 2000 |
| `profili` | profili senza regole | 200 |
| `fasce` | fasce orarie (contano anche quelle di serie) | 200 |
| `eccezioni` | giornate di festività in una fascia sola | 500 |
| `regole` | regole in un profilo solo, una per punto di accesso del kit | 100 |

Il tetto si cambia con `--tetto nome=numero`, anche più volte (`--tetto tessere=5000 --tetto regole=20`). Arrivato
al tetto senza `CapabilityViolated` il rapporto dice «almeno»: per sapere il limite vero si rilancia con un tetto più
alto. Per le eccezioni e le regole, dopo il primo `CapabilityViolated` il kit trova il numero esatto dimezzando
l'intervallo. I punti di accesso per le regole sono tutti sulla stessa porta e sullo stesso lettore del kit, senza
configurazione; anche il loro limite finisce nel rapporto. Con i tetti di serie la ricerca fa qualche migliaio di
scritture e altrettante rimozioni: a 0,2 secondi l'una (i tempi veri li misura T18 prima) è mezz'ora. Con
`--non-interattivo` la ricerca si fa lo stesso, senza la prova del pulsante di uscita (il rapporto lo dice).

## Cosa chiede all'operatore

- **T2**: staccare il cavo di rete del controller e premere Invio (ogni scrittura aspetta fino a 10 secondi),
  poi riattaccarlo e premere Invio.
- **T4, T8**: passare la tessera sul lettore quando lo chiede, poi Invio. La tessera non apre mai: il kit la
  scrive sospesa, oppure abilitata ma senza nessun profilo che apra quella porta. La tessera vera con il profilo
  che apre si scrive sospesa solo se prima una tessera di prova, con un numero del kit, è tornata davvero sospesa;
  altrimenti il rapporto dice «SetCredential non sospende: passaggio non fatto». Prima di ogni passaggio il kit
  rilegge tessere e profili: se una tessera con quel numero risulta abilitata con un profilo che apre quella porta,
  il passaggio non lo chiede, e la sospende o la toglie (se non è del kit non la tocca e si ferma).
- **T18 con `--limiti`**: mentre il kit scrive il primo blocco di tessere, premere il **pulsante di uscita** della
  porta del banco una o due volte e dire se la porta si è aperta subito, in ritardo o per niente. **Non passare
  tessere.** Se sul controller non c'è la porta configurata, il kit chiede di configurarla con la procedura guidata.
- **T12**: fare il reset di fabbrica dalla pagina web del controller, ricreare l'account con lo stesso utente e la
  stessa password e rimettere lo stesso indirizzo, poi Invio. Il reset non lo fa il kit: dopo legge soltanto.
- **T16**: dire come compare l'utente `aam_prova_impianto` nella pagina del controller.

Scrivendo `salta` a una domanda quella parte non si fa.

## Sicurezza

- Non parte se il numero di serie letto non è quello di `--serie`, e lo ricontrolla prima di ogni prova e di
  ogni pulizia.
- Non parte se sul controller ci sono utenti o tessere che non sono suoi (`aam_prova_`): un controller così sembra
  in servizio.
- Crea solo oggetti con token e nome `aam_prova_` e li toglie tutti dopo ogni prova e alla fine, anche dopo un
  errore o con Ctrl+C (una volta sola; se il kit aspetta una risposta, poi Invio), e stampa quello che non è
  riuscito a togliere. Alla partenza toglie quelli rimasti da una volta precedente. Con `--limiti` la pulizia dopo
  un Ctrl+C può durare minuti: un secondo Ctrl+C la interrompe, e quello che resta lo toglie il kit alla partenza
  successiva.
- La tessera vera dell'operatore non ha mai insieme un profilo che apre la porta e lo stato abilitato quando il kit
  chiede di passarla (vedi T4 e T8). Gli oggetti di T18 con `--limiti` non aprono niente: profili senza regole, o
  con regole solo per punti di accesso del kit, e tessere con numeri del kit.
- Le richieste passano da un filtro (`Registratore`) che lascia partire solo le letture e le scritture elencate nel
  kit, e le scritture solo su oggetti `aam_prova_`. Non partono mai comandi alle porte (AccessDoor, UnlockDoor,
  LockDoor, DoorSchedule…), configurazioni di porte e lettori, morsetti, controllo accessi, modi di
  identificazione, fuso, NTP, MQTT, rete o account. Porte, lettori e punti di accesso `aam_prova_` sono oggetti
  senza configurazione: nessuna serratura, nessun relè, nessun morsetto.
- Prima e dopo ogni prova fotografa tutto quello che non è suo e segnala se qualcosa è cambiato.
- T13, T14, T15, T17 e T19 non fanno parte del kit.

## Il risultato

A schermo un rapporto per prova, e un file `prove_a1210_<serie>_<ora>.json` (o `--uscita`) con tutte le
richieste e le risposte grezze, senza intestazioni e senza password: è quello da mandare. Il file si aggiorna
dopo ogni prova, anche se il kit si ferma a metà. Nella ricerca dei limiti, delle scritture e delle rimozioni
riuscite il file tiene solo metodo e durata; quelle non riuscite, come il `CapabilityViolated`, restano per intero.
In fondo al rapporto di T18 la riga «limiti per hardware.LIMITS» li riassume.

Uscita: 0 tutto bene, 1 qualcosa non tolto o una richiesta vietata fermata, 2 il kit non è partito o si è
fermato (controller sbagliato o in servizio), 130 interrotto.

`tests/test_banco_kit.py` esegue il kit tutto sul controller finto dei test.
