# Prove al banco sull'A1210

`prove_a1210.py` esegue sul controller **di banco** le prove T1-T12, T16 e T18 del passaggio «l'app è l'unica
fonte di verità» e scrive un rapporto da mandare a chi sviluppa l'add-on. Usa il client VAPIX dell'add-on
(`axis_access_manager/app/vapix.py`), così le richieste sono quelle che manderà l'app. T18 senza `--limiti` è
parziale: misura solo i tempi con molte tessere, e il rapporto lo dice.

Il kit sta fuori dall'add-on: l'immagine copia solo `axis_access_manager/app`, quindi non finisce mai in Home
Assistant. **Sul controller di un impianto in servizio si usa solo con `--impianto-in-servizio`** (sezione
[Impianto in servizio](#impianto-in-servizio)): senza, il kit si rifiuta di partire.

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
  in servizio (con `--impianto-in-servizio` parte, con le regole della sezione qui sotto).
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
fermato (controller sbagliato o in servizio), 130 interrotto. Con `--impianto-in-servizio` anche 3 impianto cambiato
e 4 impianto non verificato, che contano più di tutti gli altri.

`tests/test_banco_kit.py` esegue il kit tutto sul controller finto dei test, anche con `--impianto-in-servizio` su un
finto configurato come l'impianto del Teatro Eliseo.

## Impianto in servizio

Con `--impianto-in-servizio` il kit lavora sul controller di un impianto **in servizio**, per esempio quello del
Teatro Eliseo mentre la porta è aperta per una manutenzione, senza aspettare l'A1210 di banco. Fa solo le prove che
non possono toccare niente di quello che c'è, e alla fine dimostra che l'impianto è rimasto com'era. **Nessun reset
di fabbrica**: T12 non si fa.

### Cosa fa e cosa non fa

- Scrive e toglie soltanto **utenti, tessere, profili e fasce** con token e nome `aam_prova_`. Non scrive e non
  toglie mai porte, configurazioni delle porte, lettori, configurazioni dei lettori, punti di accesso, controllo
  accessi (AccessController), modi di identificazione, morsetti e IO, impostazioni del dispositivo (fuso, NTP, MQTT,
  rete, account) e non manda comandi alle porte, neppure su oggetti del kit. Lo impedisce il filtro delle richieste:
  una richiesta vietata non parte mai, il kit si ferma, toglie quello che ha creato e lo scrive nel rapporto.
- Le tessere del kit non citano mai utenti o profili dell'impianto e non hanno mai il numero di una tessera vera. I
  profili del kit possono citare un solo punto di accesso vero, quello del lettore dove si passa la tessera di scorta
  (T4 e T8), mai altri. La tessera di scorta non viene mai scritta abilitata con un profilo che apre la porta, e
  non viene mai riattivata dopo essere stata scritta sospesa con un profilo così.
- Prove: T11, T1, T2, T3, T5, T6, T7, T9, T10, T16, T18, T4 e T8. T18 scrive 100 tessere (`--quante` fino a 500)
  e mai con `--limiti`. T12, `--limiti` e `--tetto` sono rifiutati, come `--non-interattivo` (servono le conferme).
  Porte, lettori e punti di accesso in T1, T2, T6, T7 e T10 non si provano, T3 si fa solo su utenti, tessere e
  profili, T11 solo sugli elenchi che sono vuoti anche sull'impianto (per gli altri il rapporto scrive «non fattibile
  in servizio»). Il rapporto lo dice in testa e prova per prova.
- **Fotografia.** Prima di scrivere qualsiasi cosa il kit legge tutto: utenti, tessere, profili, fasce, porte,
  configurazioni delle porte, lettori, configurazioni dei lettori, punti di accesso, controllo accessi, modi di
  identificazione, morsetti e assegnazioni IO, dati del dispositivo, ora e fuso, NTP, MQTT ed eventi MQTT, e li salva
  nel rapporto. Se qualcosa non si legge **non parte**. Dopo ogni prova rilegge tutto: se un oggetto dell'impianto
  risulta cambiato si ferma e non fa le prove successive. Alla fine, dopo la pulizia, rilegge tutto e confronta ogni
  oggetto che non è del kit con la fotografia di partenza, e controlla che non sia rimasto niente di `aam_prova_`.
  Non contano l'ora, la sincronizzazione NTP, i server NTP dati dalla rete e lo stato del collegamento MQTT, che
  cambiano da soli. Le password (per esempio quella del broker MQTT, se il controller la restituisce) nel rapporto
  sono nascoste.

### Da preparare prima, sul PC che porterai

1. **Python 3.12 o più recente**: `python3 --version` (su Windows `py --version`).
2. **Una copia del repository**, con questa versione del kit (ramo `f1/kit-impianto`, finché non è unito a `main`):

   ```
   git clone https://github.com/lurisin/axis-access-manager.git
   cd axis-access-manager
   git checkout f1/kit-impianto
   ```

3. **Un ambiente Python a parte con l'unica dipendenza del kit, `httpx` 0.28.1.** Il kit usa il client VAPIX
   dell'add-on, che importa solo `httpx`; pip installa da solo quello che serve a httpx (`httpcore`, `h11`, `anyio`,
   `certifi`, `idna`, `sniffio`). FastAPI, MQTT e il resto dell'add-on non servono. Dalla cartella del repository:

   ```
   python3 -m venv .venv-banco
   . .venv-banco/bin/activate
   pip install httpx==0.28.1
   ```

   Su Windows: `py -3.12 -m venv .venv-banco`, poi `.venv-banco\Scripts\activate` e lo stesso `pip install`.
4. **Prova a vuoto**, senza controller: `python tools/banco/prove_a1210.py --help` mostra l'aiuto con
   `--impianto-in-servizio`.

### Cosa serve sul posto

- Il PC nella rete del controller: dal browser deve aprirsi `https://10.103.200.10`.
- Il **numero di serie** del controller (etichetta, o pagina del controller): nell'esempio `B8A44FE6A074`. Se non è
  quello letto dal controller il kit non parte.
- Nome e password di un **amministratore del controller** (lo stesso account dell'add-on, in Impianti). Il nome va
  in `--utente` (di serie `root`), la password la chiede il kit e non va scritta sulla riga di comando.
- Una **tessera di scorta mai registrata sull'impianto**, con il numero come lo legge il controller (per esempio
  `c0110b18`), da dare con `--tessera`. Il kit controlla che non sia già sul controller: se c'è, non parte.
- **L'add-on Axis Access Manager fermo**: Home Assistant → Impostazioni → Componenti aggiuntivi → Axis Access Manager
  → Arresta. Acceso, la sincronizzazione importerebbe le tessere di prova.
- **Il tecnico sul posto**, con la porta aperta per la manutenzione: in T2 stacca e riattacca il cavo di rete del
  controller, in T4 e T8 passa la tessera di scorta sul lettore. Durante le prove nessuno cambia la configurazione
  del controller dalla sua pagina web: il kit lo vedrebbe come un impianto cambiato e si fermerebbe.

### Il comando

Dalla cartella del repository, con l'ambiente attivo:

```
python tools/banco/prove_a1210.py --impianto-in-servizio --host 10.103.200.10 --serie B8A44FE6A074 --utente root --tessera c0110b18
```

Il kit chiede la password, poi due **conferme**, a cui si risponde scrivendo `sì` (o `si`): l'add-on è fermo; il
tecnico è sul posto. Con qualsiasi altra risposta non parte e non manda niente al controller. Poi a schermo dice cosa
fa e chiede quando serve:

- **T2**: «Stacca ora il cavo di rete dal controller»: il tecnico lo stacca, Invio; poi lo riattacca, Invio. Se il
  controller prende la corrente dal cavo di rete (PoE, senza un alimentatore a parte) staccandolo si spegne: alla
  riaccensione riparte con la configurazione di prima e il kit aspetta fino a 10 minuti che risponda. Se preferite
  non spegnerlo, scrivete `salta`.
- **T4 e T8**: «Passa la tessera di scorta … sul lettore di …»: il tecnico la passa, Invio. La porta non si apre: la
  tessera è sospesa o senza un profilo che la apra.
- **T16**: come compare l'utente `aam_prova_impianto` nella pagina del controller (Invio per saltare).
- **T18**: se intanto la pagina del controller è rallentata (Invio per niente).

Scrivendo `salta` a una domanda quella parte non si fa. Ctrl+C (una volta sola; se il kit aspetta una risposta, poi
Invio) interrompe: il kit toglie quello che ha creato, rilegge tutto e dà comunque il verdetto. Il rapporto va nella cartella da cui si lancia il comando,
`prove_a1210_B8A44FE6A074_<data>_<ora>.json` (oppure `--uscita file.json`), e si aggiorna dopo ogni prova.

### Il risultato

L'ultima riga a schermo è il verdetto:

- `IMPIANTO INVARIATO`: tutto riletto, ogni oggetto dell'impianto identico a prima, niente di `aam_prova_` rimasto.
- `ATTENZIONE: IMPIANTO CAMBIATO`, seguita da ogni differenza: elenco, oggetto, campo, valore prima e dopo (o
  l'oggetto intero se è comparso o sparito, o un oggetto del kit non tolto). Codice di uscita 3.
- `ATTENZIONE: IMPIANTO NON VERIFICATO`: qualcosa alla fine non si è riletto (per esempio il controller non risponde):
  non vale come invariato. Codice di uscita 4.

`NON PARTO` vuol dire che il kit non ha scritto niente: il motivo è a schermo e nel rapporto.

### Dopo le prove

1. **Riavvia l'add-on**: Home Assistant → Impostazioni → Componenti aggiuntivi → Axis Access Manager → Avvia.
2. In **Da Registrare** comparirà la tessera di scorta passata durante le prove: **Ignora**. Nel registro degli
   accessi i suoi passaggi negati sono quelli delle prove.
3. **Manda il file JSON** a chi sviluppa l'add-on, anche se il kit si è fermato a metà o non è partito. Contiene la
   fotografia dell'impianto (nomi, tessere, profili, configurazioni) e tutte le richieste e risposte, senza password.
   Se il verdetto non è `IMPIANTO INVARIATO` dillo subito, con la schermata.
