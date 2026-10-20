"""Entry point: python -m alarm"""
import logging
import os
import threading

from . import checker, db, notify, settings, web


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init()
    checker.start()

    # Prepare the "confirmed" message in the background so the first call is not delayed.
    def warm_up():
        try:
            notify.ensure_ack_sound(settings.get_all())
        except Exception as e:  # noqa: BLE001 - TTS is optional at startup
            logging.getLogger("tts").warning("Bevestigingsbericht niet aangemaakt: %s", e)

    threading.Thread(target=warm_up, daemon=True).start()
    web.serve(int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
