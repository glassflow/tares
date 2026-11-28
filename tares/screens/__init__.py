"""One module per console screen. A screen adds tares/screens/<name>.py with register(app, h),
where `h` is the daemon's Deps (tares/deps.py), and make_app() registers it in its _SCREENS list."""
