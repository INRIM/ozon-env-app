import logging
from pathlib import Path

logging.config.fileConfig(
    Path(__file__).parent.joinpath('logging.conf').absolute(),
    disable_existing_loggers=False)