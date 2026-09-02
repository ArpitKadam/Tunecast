import logging

import pytest

from tunecast.log import LOGGER_NAME


@pytest.fixture(autouse=True)
def reset_tunecast_logger():
    """Detach handlers after every test so a captured stdout never outlives its test."""
    yield
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
