# A permission a station was refused

Part of the station-orchestrator manual. Read when a station tells you a permission check denied a command (protocol rule 13).

Put it on the desk as a permission request: `desk_add(question, recommendation, why, station="<the station>", command="<the exact command, verbatim as it reported it>", blocking=..., project=...)`, with `recommendation` the grant in plain words, naming the station, the command and where (for example: shop-build-m1 may run `docker build .` in acme/shop). Leave out `project` for a crew-wide station (reviewer, releaser).

The person allows it with one click in the Tares console, on the project page or the Crew page. Tares keeps it as a grant in their words and answers the item, and the crew's watch restarts that station with the grant in its settings once it is between turns, telling it to run the command again. Relay nothing for the permission itself: a message can't grant it, only the station's settings can.

Show it like any desk item, but end its block with where to allow it (the Crew page or the project page in Tares) instead of "Reply with a letter"; a yes typed in your session doesn't allow it, so point them there. Once Tares shows it answered, take it as done.
