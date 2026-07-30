"""Register agents here — each application's agents load at startup.

Adding a new agent = a new module under this package + an import line here.
"""

from . import mail_agent  # noqa: F401  (registers mail-agent.mail)
from . import voice_agent  # noqa: F401  (registers voice-agent.call)
