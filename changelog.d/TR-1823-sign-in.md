Fixed
Signing in with a token on a self-hosted cell with auth on said Invalid token for every token (TR-1823). The form checked the token against an address 2.0 removed; it now checks it with whoami, so the root token and any read, ingest or admin key sign in.
