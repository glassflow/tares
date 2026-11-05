# ee

This directory holds the Tares enterprise code: teams, and later enterprise integrations. It is
under its own licence, `ee/LICENSE`, not the MIT licence of the rest of the repository.

The code ships in the same `tares` wheel and image as the open source code, as the package
`tares_ee`. It stays off until you give Tares an enterprise licence key, in the `TARES_LICENSE`
environment variable or under Settings in the console.

Without a key, Tares runs for one person. Nothing in `tares/` imports `tares_ee`; it is loaded
through the `tares.ee` entry point group.
