"""Read your own study data from the Sondavi survey platform.

    from sondavi import connect

    con = connect("https://survey.example.org")   # token from SONDAVI_TOKEN
    rows = con.responses(42)

The token belongs in the environment, not in the script: a token written into an
analysis travels with it into version control, onto shared drives and into
supplementary material. That is the usual way one leaks.

A certified dataset - a CSV file the platform issued with a `.certificate.json` - is checked
without a connection or a token:

    from sondavi import verify

    print(verify("study.csv", "study.certificate.json"))
"""

from .client import Connection, ApiError, connect, unnest, options, markings, conversations, items, scale
from .snapshots import Snapshot
from .certificates import Verification, verify

__all__ = ["connect", "Connection", "ApiError", "Snapshot", "unnest", "options", "markings", "conversations", "items", "scale",
           "verify", "Verification"]
__version__ = "0.9.0"
