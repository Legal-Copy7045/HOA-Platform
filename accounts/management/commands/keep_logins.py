"""Keep the real logins across a demo reseed (decision D53).

    python manage.py keep_logins export --out -   # JSON on stdout
    python manage.py keep_logins import --in -    # JSON on stdin

Run by infra/poc/reseed-demo.sh with the owner database role: export before
the database is emptied (into a root-only temporary file, shredded after),
import after migrating and before seeding. Only on a demo deployment
(SEED_DEMO_ALLOWED=true). What is kept: accounts/keep_logins.py. The file
holds password hashes and MFA secrets: never copy it anywhere.
"""

import json
import sys

from django.core.management.base import BaseCommand, CommandError

from accounts import keep_logins


class Command(BaseCommand):
    help = "Export or import the real (non-demo) logins around a demo reseed."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["export", "import"])
        parser.add_argument("--out", help="export: '-' for stdout, or a file path")
        parser.add_argument("--in", dest="source", help="import: '-' for stdin, or a file path")

    def handle(self, *args, action, out=None, source=None, **opts):
        try:
            if action == "export":
                if not out:
                    raise CommandError("export needs --out (- for stdout).")
                text = keep_logins.dumps(keep_logins.export_data())
                if out == "-":
                    sys.stdout.write(text)
                    sys.stdout.flush()
                else:
                    with open(out, "w", encoding="utf-8") as fh:
                        fh.write(text)
                return
            if not source:
                raise CommandError("import needs --in (- for stdin).")
            if source == "-":
                raw = sys.stdin.read()
            else:
                with open(source, encoding="utf-8") as fh:
                    raw = fh.read()
            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise CommandError("The keep_logins file is not valid JSON.") from exc
            logins, tasks = keep_logins.import_data(data)
        except keep_logins.KeepLoginsError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Kept {logins} real login(s) and {tasks} server task record(s).")
