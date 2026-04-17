# MiniJellyTermux

Un mini clone **molto semplice** di Jellyfin scritto in Python puro (solo libreria standard), pensato per girare facilmente su **Termux**.

## Funzionalità

- Indicizzazione di cartelle media (video/audio)
- Interfaccia web minimale per browsing e riproduzione
- Endpoint JSON `GET /api/library`
- Streaming con supporto `Range` (seek nei player)
- Rescan libreria da UI (`/rescan`)

## Requisiti

- Python 3.10+ (su Termux: `pkg install python`)

## Installazione su Termux

```bash
pkg update && pkg upgrade -y
pkg install python -y
termux-setup-storage
```

Clona/copia questo progetto e avvia:

```bash
python jellytermux.py ~/storage/shared/Movies ~/storage/shared/Music --port 8096
```

Apri il browser su:

- `http://127.0.0.1:8096` (dallo stesso dispositivo)
- oppure `http://<IP_DEL_TELEFONO>:8096` (dalla LAN, se consentito)

## API

### `GET /api/library`

Ritorna l'elenco dei media indicizzati.

Esempio risposta:

```json
[
  {
    "id": "d34db33f...",
    "name": "Movies/Example.mp4",
    "size": 123456789,
    "stream": "/stream/d34db33f...",
    "media_type": "video"
  }
]
```

## Note

- Questo progetto è una demo educativa, non sostituisce Jellyfin completo.
- Nessun sistema utenti/transcoding/metadata avanzati.
