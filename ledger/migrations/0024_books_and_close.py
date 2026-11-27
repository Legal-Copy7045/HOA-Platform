# Books and close (D69-D71): bank accounts by fund, categories and coding rules,
# coded bank lines (GL entries), period closes and reopens, close exceptions,
# close runs and year-end packages. Append-only tables get their triggers; the
# closed-period trigger refuses rows dated in a closed month.

import django.db.models.deletion
import django.utils.timezone
import simple_history.models
from django.conf import settings
from django.db import migrations, models

import ledger.books.models
from core.append_only import append_only_trigger_operations


class Migration(migrations.Migration):
    dependencies = [
        ("ledger", "0023_disbursements"),
        ("operations", "0010_contract_two_approvals"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CloseRun",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[("month", "Month"), ("year", "Fiscal year")],
                        max_length=5,
                    ),
                ),
                ("period", models.CharField(max_length=8)),
                ("ran_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("passed", models.BooleanField()),
                ("closed", models.BooleanField(default=False)),
                ("blocking", models.PositiveIntegerField(default=0)),
                ("warnings", models.PositiveIntegerField(default=0)),
                ("results", models.JSONField(default=list)),
            ],
            options={
                "ordering": ["-ran_at", "-id"],
            },
        ),
        migrations.AlterField(
            model_name="bankbalance",
            name="source",
            field=models.CharField(
                choices=[
                    ("teller", "Teller"),
                    ("ofx", "OFX/QFX file"),
                    ("csv", "CSV file"),
                    ("entered", "Entered from the statement"),
                ],
                max_length=8,
            ),
        ),
        migrations.AlterField(
            model_name="bankmatch",
            name="confirmed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="banktransaction",
            name="source",
            field=models.CharField(
                choices=[
                    ("teller", "Teller"),
                    ("ofx", "OFX/QFX file"),
                    ("csv", "CSV file"),
                    ("entered", "Entered from the statement"),
                ],
                max_length=8,
            ),
        ),
        migrations.CreateModel(
            name="BankAccountSetup",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "fund",
                    models.CharField(
                        choices=[
                            ("operating", "Operating fund"),
                            ("reserve", "Reserve fund"),
                        ],
                        default="operating",
                        max_length=10,
                    ),
                ),
                ("gl_account", models.CharField(max_length=80)),
                ("is_primary", models.BooleanField(default=False)),
                (
                    "opening_balance_cents",
                    models.BigIntegerField(blank=True, null=True),
                ),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "connection",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="books",
                        to="ledger.bankconnection",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-is_primary", "fund", "id"],
            },
        ),
        migrations.CreateModel(
            name="ExceptionNote",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("period", models.CharField(max_length=8)),
                ("check_key", models.CharField(max_length=30)),
                ("item", models.CharField(blank=True, max_length=40)),
                ("amount_cents", models.BigIntegerField(default=0)),
                ("reason", models.TextField()),
                ("needs_agreement", models.BooleanField(default=True)),
                (
                    "recorded_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                (
                    "recorded_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-recorded_at", "-id"],
            },
        ),
        migrations.CreateModel(
            name="ExceptionAgreement",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("agreed_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "agreed_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "note",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="agreement",
                        to="ledger.exceptionnote",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="GLCategory",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("key", models.SlugField(max_length=40, unique=True)),
                ("name", models.CharField(max_length=80)),
                ("account", models.CharField(max_length=80)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("income", "Income"),
                            ("expense", "Expense"),
                            ("transfer", "Transfer between the association's accounts"),
                            ("asset", "Asset"),
                            ("liability", "Liability"),
                            ("equity", "Fund balance"),
                        ],
                        max_length=10,
                    ),
                ),
                (
                    "fund",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("operating", "Operating fund"),
                            ("reserve", "Reserve fund"),
                        ],
                        max_length=10,
                    ),
                ),
                ("exempt_function", models.BooleanField(default=False)),
                ("active", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["kind", "account", "id"],
            },
        ),
        migrations.CreateModel(
            name="CodingRule",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=80)),
                ("contains", models.CharField(max_length=80)),
                (
                    "sign",
                    models.CharField(
                        choices=[
                            ("any", "Money in or out"),
                            ("in", "Money in"),
                            ("out", "Money out"),
                        ],
                        default="any",
                        max_length=4,
                    ),
                ),
                ("min_cents", models.BigIntegerField(blank=True, null=True)),
                ("max_cents", models.BigIntegerField(blank=True, null=True)),
                ("priority", models.PositiveSmallIntegerField(default=100)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                (
                    "connection",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="ledger.bankconnection",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "ended_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "category",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="rules",
                        to="ledger.glcategory",
                    ),
                ),
            ],
            options={
                "ordering": ["priority", "id"],
            },
        ),
        migrations.CreateModel(
            name="GLEntry",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("posted_on", models.DateField()),
                (
                    "fund",
                    models.CharField(
                        choices=[
                            ("operating", "Operating fund"),
                            ("reserve", "Reserve fund"),
                        ],
                        max_length=10,
                    ),
                ),
                (
                    "cash_fund",
                    models.CharField(
                        choices=[
                            ("operating", "Operating fund"),
                            ("reserve", "Reserve fund"),
                        ],
                        max_length=10,
                    ),
                ),
                ("cash_account", models.CharField(max_length=80)),
                ("category_account", models.CharField(max_length=80)),
                ("amount_cents", models.BigIntegerField()),
                ("memo", models.CharField(blank=True, max_length=200)),
                ("reference", models.CharField(blank=True, max_length=60)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "category",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="ledger.glcategory",
                    ),
                ),
                (
                    "coded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "reverses",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reversed_by",
                        to="ledger.glentry",
                    ),
                ),
                (
                    "rule",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="ledger.codingrule",
                    ),
                ),
                (
                    "transaction",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="gl_entries",
                        to="ledger.banktransaction",
                    ),
                ),
            ],
            options={
                "ordering": ["posted_on", "id"],
            },
        ),
        migrations.CreateModel(
            name="HistoricalBankAccountSetup",
            fields=[
                (
                    "id",
                    models.BigIntegerField(
                        auto_created=True, blank=True, db_index=True, verbose_name="ID"
                    ),
                ),
                (
                    "fund",
                    models.CharField(
                        choices=[
                            ("operating", "Operating fund"),
                            ("reserve", "Reserve fund"),
                        ],
                        default="operating",
                        max_length=10,
                    ),
                ),
                ("gl_account", models.CharField(max_length=80)),
                ("is_primary", models.BooleanField(default=False)),
                (
                    "opening_balance_cents",
                    models.BigIntegerField(blank=True, null=True),
                ),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("history_id", models.AutoField(primary_key=True, serialize=False)),
                ("history_date", models.DateTimeField(db_index=True)),
                ("history_change_reason", models.CharField(max_length=100, null=True)),
                (
                    "history_type",
                    models.CharField(
                        choices=[("+", "Created"), ("~", "Changed"), ("-", "Deleted")],
                        max_length=1,
                    ),
                ),
                (
                    "connection",
                    models.ForeignKey(
                        blank=True,
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        to="ledger.bankconnection",
                    ),
                ),
                (
                    "history_user",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "historical bank account setup",
                "verbose_name_plural": "historical bank account setups",
                "ordering": ("-history_date", "-history_id"),
                "get_latest_by": ("history_date", "history_id"),
            },
            bases=(simple_history.models.HistoricalChanges, models.Model),
        ),
        migrations.CreateModel(
            name="HistoricalCodingRule",
            fields=[
                (
                    "id",
                    models.BigIntegerField(
                        auto_created=True, blank=True, db_index=True, verbose_name="ID"
                    ),
                ),
                ("name", models.CharField(max_length=80)),
                ("contains", models.CharField(max_length=80)),
                (
                    "sign",
                    models.CharField(
                        choices=[
                            ("any", "Money in or out"),
                            ("in", "Money in"),
                            ("out", "Money out"),
                        ],
                        default="any",
                        max_length=4,
                    ),
                ),
                ("min_cents", models.BigIntegerField(blank=True, null=True)),
                ("max_cents", models.BigIntegerField(blank=True, null=True)),
                ("priority", models.PositiveSmallIntegerField(default=100)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("history_id", models.AutoField(primary_key=True, serialize=False)),
                ("history_date", models.DateTimeField(db_index=True)),
                ("history_change_reason", models.CharField(max_length=100, null=True)),
                (
                    "history_type",
                    models.CharField(
                        choices=[("+", "Created"), ("~", "Changed"), ("-", "Deleted")],
                        max_length=1,
                    ),
                ),
                (
                    "category",
                    models.ForeignKey(
                        blank=True,
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        to="ledger.glcategory",
                    ),
                ),
                (
                    "connection",
                    models.ForeignKey(
                        blank=True,
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        to="ledger.bankconnection",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "ended_by",
                    models.ForeignKey(
                        blank=True,
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "history_user",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "historical coding rule",
                "verbose_name_plural": "historical coding rules",
                "ordering": ("-history_date", "-history_id"),
                "get_latest_by": ("history_date", "history_id"),
            },
            bases=(simple_history.models.HistoricalChanges, models.Model),
        ),
        migrations.CreateModel(
            name="HistoricalGLCategory",
            fields=[
                (
                    "id",
                    models.BigIntegerField(
                        auto_created=True, blank=True, db_index=True, verbose_name="ID"
                    ),
                ),
                ("key", models.SlugField(max_length=40)),
                ("name", models.CharField(max_length=80)),
                ("account", models.CharField(max_length=80)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("income", "Income"),
                            ("expense", "Expense"),
                            ("transfer", "Transfer between the association's accounts"),
                            ("asset", "Asset"),
                            ("liability", "Liability"),
                            ("equity", "Fund balance"),
                        ],
                        max_length=10,
                    ),
                ),
                (
                    "fund",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("operating", "Operating fund"),
                            ("reserve", "Reserve fund"),
                        ],
                        max_length=10,
                    ),
                ),
                ("exempt_function", models.BooleanField(default=False)),
                ("active", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("history_id", models.AutoField(primary_key=True, serialize=False)),
                ("history_date", models.DateTimeField(db_index=True)),
                ("history_change_reason", models.CharField(max_length=100, null=True)),
                (
                    "history_type",
                    models.CharField(
                        choices=[("+", "Created"), ("~", "Changed"), ("-", "Deleted")],
                        max_length=1,
                    ),
                ),
                (
                    "history_user",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        db_constraint=False,
                        null=True,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "historical gl category",
                "verbose_name_plural": "historical gl categorys",
                "ordering": ("-history_date", "-history_id"),
                "get_latest_by": ("history_date", "history_id"),
            },
            bases=(simple_history.models.HistoricalChanges, models.Model),
        ),
        migrations.CreateModel(
            name="PeriodClose",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[("month", "Month"), ("year", "Fiscal year")],
                        max_length=5,
                    ),
                ),
                ("period", models.CharField(max_length=8)),
                ("starts_on", models.DateField()),
                ("ends_on", models.DateField()),
                (
                    "how",
                    models.CharField(
                        choices=[
                            ("auto", "Closed automatically"),
                            ("manual", "Closed by a board member"),
                        ],
                        max_length=6,
                    ),
                ),
                ("closed_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("summary", models.JSONField(default=dict)),
                (
                    "closed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-starts_on", "-id"],
            },
        ),
        migrations.CreateModel(
            name="PeriodReopenRequest",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("reason", models.TextField()),
                (
                    "requested_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                (
                    "close",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reopen_requests",
                        to="ledger.periodclose",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-requested_at", "-id"],
            },
        ),
        migrations.CreateModel(
            name="PeriodReopenDecision",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("approved", models.BooleanField()),
                ("note", models.TextField(blank=True)),
                ("decided_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "decided_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "request",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="decision",
                        to="ledger.periodreopenrequest",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="YearEndPackage",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("fiscal_year", models.PositiveSmallIntegerField()),
                ("year_closed", models.BooleanField()),
                (
                    "file",
                    models.FileField(max_length=200, upload_to=ledger.books.models._package_key),
                ),
                ("size", models.PositiveBigIntegerField()),
                ("sha256", models.CharField(max_length=64)),
                ("manifest", models.JSONField(default=dict)),
                ("signature", models.CharField(max_length=64)),
                ("file_count", models.PositiveIntegerField()),
                ("skipped_count", models.PositiveIntegerField(default=0)),
                ("automatic", models.BooleanField(default=False)),
                ("built_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "built_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-built_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="exceptionnote",
            index=models.Index(
                fields=["period", "check_key"], name="ledger_exce_period_044580_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="glentry",
            constraint=models.CheckConstraint(
                condition=models.Q(("amount_cents", 0), _negated=True),
                name="gl_entry_nonzero",
            ),
        ),
        migrations.AddIndex(
            model_name="periodclose",
            index=models.Index(
                fields=["kind", "starts_on", "ends_on"],
                name="ledger_peri_kind_f2f5b6_idx",
            ),
        ),
    ]


APPEND_ONLY = [
    "ledger_glentry",
    "ledger_periodclose",
    "ledger_periodreopenrequest",
    "ledger_periodreopendecision",
    "ledger_exceptionnote",
    "ledger_exceptionagreement",
    "ledger_closerun",
    "ledger_yearendpackage",
]

CLOSED_FUNCTIONS = """
CREATE OR REPLACE FUNCTION ledger_books_month_closed(d date) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM ledger_periodclose c
        WHERE c.kind = 'month' AND d BETWEEN c.starts_on AND c.ends_on
          AND c.id = (SELECT max(m.id) FROM ledger_periodclose m
                      WHERE m.kind = 'month' AND m.period = c.period)
          AND NOT EXISTS (
              SELECT 1 FROM ledger_periodreopenrequest r
              JOIN ledger_periodreopendecision x ON x.request_id = r.id
              WHERE r.close_id = c.id AND x.approved
          )
    );
$$;

CREATE OR REPLACE FUNCTION ledger_books_refuse_closed() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    d date;
BEGIN
    EXECUTE format('SELECT ($1).%I', TG_ARGV[0]) INTO d USING NEW;
    IF d IS NOT NULL AND ledger_books_month_closed(d) THEN
        RAISE EXCEPTION 'books closed: % dated % is in a closed month', TG_TABLE_NAME, d
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
"""

# (table, date column): rows that post money on a date.
LOCKED = [
    ("ledger_ledgerentry", "posted_on"),
    ("ledger_glentry", "posted_on"),
    ("ledger_depositbatch", "deposited_on"),
    ("ledger_disbursementcheck", "issued_on"),
    ("operations_invoicepayment", "paid_on"),
]


def _closed_triggers():
    ops = [migrations.RunSQL(CLOSED_FUNCTIONS, reverse_sql=migrations.RunSQL.noop)]
    for table, column in LOCKED:
        name = f"{table}_books_closed"
        ops.append(
            migrations.RunSQL(
                f'CREATE TRIGGER "{name}" BEFORE INSERT ON "{table}" FOR EACH ROW '
                f"EXECUTE FUNCTION ledger_books_refuse_closed('{column}');",
                reverse_sql=f'DROP TRIGGER IF EXISTS "{name}" ON "{table}";',
            )
        )
    return ops


# The standard categories as first installed (ledger/books/funds.py keeps
# the same list for new installs and tests; this copy never changes).
DEFAULT_CATEGORIES = [
    ("interest_income", "Interest income", "4500 Interest income", "income", False),
    ("refunds_received", "Refunds received from vendors", "4800 Refunds received", "income", False),
    ("other_income", "Other income", "4900 Other income", "income", False),
    ("bank_fees", "Bank fees", "6100 Bank fees", "expense", False),
    (
        "processing_fees",
        "Payment processing fees",
        "6150 Payment processing fees",
        "expense",
        False,
    ),
    ("insurance", "Insurance", "5300 Insurance", "expense", False),
    ("utilities", "Utilities", "5600 Utilities", "expense", False),
    ("taxes", "Taxes and government fees", "6400 Taxes and fees", "expense", False),
    (
        "penalties_paid",
        "Penalties and interest paid",
        "6900 Penalties and interest paid",
        "expense",
        False,
    ),
    ("adjustments", "Adjustments", "6950 Adjustments", "expense", False),
    (
        "transfer",
        "Transfer between the association's accounts",
        "3900 Transfers between funds",
        "transfer",
        False,
    ),
]


def _default_categories(apps, schema_editor):
    GLCategory = apps.get_model("ledger", "GLCategory")
    for key, name, account, kind, exempt in DEFAULT_CATEGORIES:
        GLCategory.objects.get_or_create(
            key=key,
            defaults={"name": name, "account": account, "kind": kind, "exempt_function": exempt},
        )


Migration.operations += [
    op for table in APPEND_ONLY for op in append_only_trigger_operations(table)
]
Migration.operations += _closed_triggers()
Migration.operations += [migrations.RunPython(_default_categories, migrations.RunPython.noop)]
