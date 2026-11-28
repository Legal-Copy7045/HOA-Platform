# Backlog

Agreed but not yet scheduled. Each line says what was agreed and where it
came from; a line goes when its work ships (name the release in its
CHANGELOG section). Linked from `docs/claude/development.md`.

- Give the web container fewer keys than the worker (review finding EXP-002, 0.44.0): per-service keys, or an encrypt-only public key in web with the key tests run by the worker.
- Crawl a simulation snapshot on every simulation run and fix what it finds (0.47.0, month-6 crawl): the manager's phone view of Vendor requirements is 545 px too wide with that data (the demo data fits), and a former owner's `/requests/?mine=1` goes to My history, so the crawl should expect that redirect.
- ARC coverage when the applicant is one of two reviewers and the other is their neighbour, so nobody may decide (0.47.0 simulation): `check_coverage` should warn the board to appoint a third reviewer.
- Job health uses the database clock (`procrastinate_events.at`), which the simulation cannot move (0.47.0): judge job health from Python's clock if the simulation needs it.
- Production server set-up for 1.0.0: a VPS behind the Cloudflare tunnel, with the POC kept as the demo and staging server.
