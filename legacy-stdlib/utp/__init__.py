"""Universal Ticketing, Booking & Access Management Platform.

A multi-tenant, configuration-driven platform that sells, reserves, delivers,
validates and reconciles admission across every channel a venue operates.

The package is deliberately free of any venue-type-specific code path (R1.6).
Aquaria Phuket, the first production deployment, exists only as configuration
data under ``config/`` and is loaded by ``utp.provisioning``.

Implementation notes
--------------------
* Storage is SQLite via the standard library. All capacity-critical writes are
  serialized through ``BEGIN IMMEDIATE`` and guarded by table CHECK constraints
  and partial unique indexes so that overselling is impossible at the data
  layer, not merely at the service layer (R10.5, R57.9).
* Money is handled exclusively in integer minor units. No float arithmetic ever
  touches a price, tax or total (R5.5).
* Every service call takes a ``RequestContext`` carrying the authenticated
  principal, tenant, venue scope, channel and correlation id. Tenant scoping and
  permission enforcement happen in the service layer, below the API, so that no
  transport can bypass them (R42.1).
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
