import logging

from verifair.app import create_app
from verifair.config import load_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = create_app(load_settings())
