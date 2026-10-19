"""Mesh Button — the agent half of the plugin.

The real work lives in two other places:

* ``dashboard/plugin_api.py`` — backend routes, mounted by the gateway at
  ``/api/plugins/mesh-button/`` at server start (the plugin must be listed in
  ``plugins.enabled`` for the import to happen at all). It owns the broker
  process: status / start / stop / restart / pairing.
* ``desktop/plugin.js`` — the desktop UI half (statusbar chip + sidebar page),
  hot-loaded from ``$HERMES_HOME/desktop-plugins/mesh-button/``.

No tools, no hooks — but the loader treats a module without ``register()`` as a
broken plugin, so the no-op below keeps the manifest honest.
"""

__version__ = "1.0.0"


def register(ctx) -> None:  # noqa: ARG001 - loader contract, nothing to register
    """No tools, no hooks: the plugin's surface is the dashboard + desktop half."""
    return None
