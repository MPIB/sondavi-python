"""Read your own study data from the Sondavi survey platform.

    from sondavi import connect

    con = connect("https://survey.example.org")   # token from SONDAVI_TOKEN
    rows = con.responses(42)

The token belongs in the environment, not in the script: a token written into an
analysis travels with it into version control, onto shared drives and into
supplementary material. That is the usual way one leaks.
"""

from .client import Connection, ApiError, connect, unnest, markings
from .snapshots import Snapshot

__all__ = ["connect", "Connection", "ApiError", "Snapshot", "unnest", "markings"]
__version__ = "0.3.0"
