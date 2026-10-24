import logging

from verifair.app import create_app
from verifair.config import load_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
try:
    app = create_app(load_settings())
except RuntimeError as error:
    # a config mistake needs its message, not a traceback through the app factory
    logging.getLogger("verifair").error("%s", error)
    raise SystemExit(1) from None
