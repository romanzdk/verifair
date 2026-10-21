from verifair.app import create_app
from verifair.config import load_settings

app = create_app(load_settings())
