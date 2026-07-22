"""Register agents here — each application's agents load at startup.

Adding a new agent = a new module under this package + an import line here.
"""

from . import west_agent  # noqa: F401  (registers west-agent.mail)
