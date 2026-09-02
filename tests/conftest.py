import logging

import pytest

from tunecast.log import LOGGER_NAME


@pytest.fixture(autouse=True)
def reset_tunecast_logger():
    """Detach handlers after every test so a captured stdout never outlives its test."""
    yield
    for name in list(logging.Logger.manager.loggerDict):
        if name == LOGGER_NAME or name.startswith(LOGGER_NAME + "."):
            logger = logging.getLogger(name)
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)
