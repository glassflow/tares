Fixed
A station on Codex runs its commands in a plain shell instead of a login shell, so they keep the PATH the station was started with (TR-1600). On Debian and Ubuntu a login shell's /etc/profile sets PATH anew, and a `tares` installed in a virtualenv was then not found by the station's own commands, such as `tares factory report`.
