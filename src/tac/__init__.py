try:
    from importlib.metadata import version
    __version__ = version("twilio-agent-connect")
except Exception:
    __version__ = "0.0.0-local"

# isort: off
# ``tac.core`` must load before ``tac.context.base`` to avoid a circular import
# (``tac.context.base`` -> ``tac.core.logging`` -> ``tac.core`` -> clients).
from tac.core import TAC, TACConfig, get_logger
from tac.context.base import PartnerConnector
from tac.models import TwiMLOptions
from tac.utils.redaction import mask_address, mask_email, mask_phone

# isort: on

__all__ = [
    "TAC",
    "TACConfig",
    "PartnerConnector",
    "get_logger",
    "TwiMLOptions",
    "mask_address",
    "mask_email",
    "mask_phone",
    "__version__",
]
