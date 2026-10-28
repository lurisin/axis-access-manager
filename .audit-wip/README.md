# Analisi in corso (cartella temporanea)

Risultati parziali dell'analisi del codice (bug, coerenza grafica e dei testi) avviata il 28/10/2026,
fermata a metà per il limite di utilizzo. Non fa parte dell'add-on: va tolta a fine analisi.

- `script/axis-audit.js`: lo script del workflow (un gruppo di aree per esecuzione, `args.group`).
- `journals/<gruppo>--<run>.jsonl`: una riga per agente completato, con il suo risultato.
  - `backend-api`: completato (43 segnalazioni verificate, 1 smentita), anche in `results/backend-api.json`.
  - `backend-core`, `backend-mod1`, `backend-mod2`, `frontend`: ricerche finite, verifiche a metà.
  - `ui`: 5 ricerche su 6 finite, verifiche non iniziate.
- `scratch.tar.gz`: script di riproduzione e il banco di prova dell'interfaccia (`ui-harness/`).

Ripresa: con lo stesso container, `resumeFromRunId` con il run del gruppo; altrimenti si ripartono
solo le verifiche mancanti usando i risultati salvati nei diari.
