import logging

import uvicorn

from .config import apply_timezone, load_settings
from .main import create_app


def main() -> None:
    settings = load_settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    debug = settings.log_level.lower() == "debug"
    if not debug:
        # Una riga per ogni richiesta al controller (ogni 30 secondi il controllo di stato): solo in debug
        logging.getLogger("httpx").setLevel(logging.WARNING)
    tz = apply_timezone()
    logging.getLogger(__name__).info("Fuso orario: %s", tz or "UTC (TZ non disponibile)")
    # Anche le richieste della pagina (decine al minuto con il cruscotto aperto) solo in debug
    uvicorn.run(create_app(settings), host="0.0.0.0", port=8099, log_level=settings.log_level, access_log=debug)


if __name__ == "__main__":
    main()
