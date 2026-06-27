"""OIDC authentication (optional).

Activated when ``settings.oidc_enabled`` is true. When disabled (the default
single-user / loopback case), nothing in this package is imported by the
router wiring, and the routes are not mounted.

Flow: browser → ``GET /auth/login`` (302 to IdP) → IdP consent →
``GET /auth/callback?code=...`` (exchanges for tokens, sets signed session
cookie, 302 to /) → every subsequent ``/api/*`` call carries the cookie.

Sessions are stateless — a signed ``itsdangerous`` cookie holding
``{sub, email, name, exp}``. No DB session table.
"""