"""Package split from system.py."""
from ._common import *  # noqa
from .settings import *  # noqa
from .item_definitions import *  # noqa
from .logs import *  # noqa
from .daily_backup import *  # noqa
from .sidebar import *  # noqa
from .notifications import *  # noqa
from .extra import *  # noqa
from .tenants import *  # noqa

from utils.pkg_wire import wire_package
wire_package('app.blueprints.system')
