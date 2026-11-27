"""Shared application logger."""

import logging

logger = logging.getLogger("ct_viewer")
logger.setLevel(logging.INFO)
logger.propagate = False

handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s.%(module)s: %(message)s"))
logger.addHandler(handler)
