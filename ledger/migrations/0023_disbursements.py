# Disbursements (D52): batches, lines, decisions, checks and their events.

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

from core.append_only import append_only_trigger_operations


class Migration(migrations.Migration):
    dependencies = [
        ("community", "0022_contact_redesign_notice_consent"),
        ("governance", "0014_home_labels"),
        ("ledger", "0022_assessment_posting_check"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="bankmatch",
            name="kind",
            field=models.CharField(
                choices=[
                    ("deposit", "Deposit"),
                    ("payment", "Payment"),
                    ("returned", "Returned item"),
                    ("payout", "Stripe payout"),
                    ("check", "Owner check"),
                ],
                max_length=8,
            ),
        ),
        migrations.AlterField(
            model_name="reconcileditem",
            name="kind",
            field=models.CharField(
                choices=[
                    ("deposit", "Deposit"),
                    ("payment", "Payment"),
                    ("returned", "Returned item"),
                    ("payout", "Stripe payout"),
                    ("check", "Owner check"),
                ],
                max_length=8,
            ),
        ),
        migrations.CreateModel(
            name="CheckPrintSettings",
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
                ("date_x", models.PositiveIntegerField(default=680)),
                ("date_y", models.PositiveIntegerField(default=75)),
                ("payee_x", models.PositiveIntegerField(default=110)),
                ("payee_y", models.PositiveIntegerField(default=135)),
                ("amount_x", models.PositiveIntegerField(default=700)),
                ("amount_y", models.PositiveIntegerField(default=135)),
                ("words_x", models.PositiveIntegerField(default=50)),
                ("words_y", models.PositiveIntegerField(default=170)),
                ("address_x", models.PositiveIntegerField(default=110)),
                ("address_y", models.PositiveIntegerField(default=205)),
                ("memo_x", models.PositiveIntegerField(default=90)),
                ("memo_y", models.PositiveIntegerField(default=290)),
                ("stub1_y", models.PositiveIntegerField(default=400)),
                ("stub2_y", models.PositiveIntegerField(default=750)),
                ("nudge_x", models.IntegerField(default=0)),
                ("nudge_y", models.IntegerField(default=0)),
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
        ),
        migrations.CreateModel(
            name="DisbursementBatch",
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
                        choices=[
                            ("surplus", "Surplus distribution"),
                            ("refund", "Owner refund"),
                        ],
                        max_length=8,
                    ),
                ),
                ("title", models.CharField(max_length=120)),
                ("total_cents", models.BigIntegerField()),
                ("note", models.TextField(blank=True)),
                ("resolution_ref", models.CharField(blank=True, max_length=120)),
                ("required_approvals", models.PositiveSmallIntegerField()),
                ("second_approval_cents", models.BigIntegerField()),
                ("rule_citation", models.CharField(blank=True, max_length=200)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "proposed_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "resolution",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="governance.resolution",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.CreateModel(
            name="DisbursementCheck",
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
                ("number", models.CharField(max_length=20)),
                ("issued_on", models.DateField()),
                ("amount_cents", models.BigIntegerField()),
                ("payee_name", models.CharField(max_length=120)),
                ("mail_to", models.CharField(blank=True, max_length=300)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "issued_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["issued_on", "id"],
            },
        ),
        migrations.CreateModel(
            name="DisbursementCheckEvent",
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
                        choices=[("cleared", "Cleared"), ("voided", "Voided")],
                        max_length=8,
                    ),
                ),
                ("on", models.DateField()),
                ("reason", models.CharField(blank=True, max_length=200)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "bank_transaction",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="ledger.banktransaction",
                    ),
                ),
                (
                    "check_record",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="events",
                        to="ledger.disbursementcheck",
                    ),
                ),
                (
                    "recorded_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "reversal",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="ledger.ledgerentry",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at", "id"],
            },
        ),
        migrations.CreateModel(
            name="DisbursementDecision",
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
                    "decision",
                    models.CharField(
                        choices=[("approve", "Approved"), ("decline", "Declined")],
                        max_length=8,
                    ),
                ),
                ("is_recipient", models.BooleanField(default=False)),
                ("amount_cents", models.BigIntegerField()),
                ("note", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "approver",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="decisions",
                        to="ledger.disbursementbatch",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at", "id"],
            },
        ),
        migrations.CreateModel(
            name="DisbursementLine",
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
                ("amount_cents", models.BigIntegerField()),
                (
                    "method",
                    models.CharField(
                        choices=[("credit", "Statement credit"), ("check", "Check")],
                        max_length=8,
                    ),
                ),
                ("payee_name", models.CharField(blank=True, max_length=120)),
                ("mail_to", models.CharField(blank=True, max_length=300)),
                ("address_source", models.CharField(blank=True, max_length=12)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="community.account",
                    ),
                ),
                (
                    "batch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="lines",
                        to="ledger.disbursementbatch",
                    ),
                ),
                (
                    "lot",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="community.lot",
                    ),
                ),
            ],
            options={
                "ordering": ["batch", "id"],
            },
        ),
        migrations.CreateModel(
            name="DisbursementEntry",
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
                    "check_record",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="ledger.disbursementcheck",
                    ),
                ),
                (
                    "entry",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="ledger.ledgerentry",
                    ),
                ),
                (
                    "line",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="entries",
                        to="ledger.disbursementline",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.AddField(
            model_name="disbursementcheck",
            name="line",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="checks",
                to="ledger.disbursementline",
            ),
        ),
        migrations.AddConstraint(
            model_name="disbursementbatch",
            constraint=models.CheckConstraint(
                condition=models.Q(("total_cents__gt", 0)),
                name="disbursement_total_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="disbursementcheckevent",
            constraint=models.UniqueConstraint(
                fields=("check_record",), name="disbursement_check_one_event"
            ),
        ),
        migrations.AddConstraint(
            model_name="disbursementdecision",
            constraint=models.UniqueConstraint(
                fields=("batch", "approver"),
                name="disbursement_decision_once_per_person",
            ),
        ),
        migrations.AddConstraint(
            model_name="disbursementline",
            constraint=models.CheckConstraint(
                condition=models.Q(("amount_cents__gt", 0)),
                name="disbursement_line_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="disbursementline",
            constraint=models.UniqueConstraint(
                fields=("batch", "account"), name="disbursement_line_once_per_account"
            ),
        ),
        migrations.AddConstraint(
            model_name="disbursementcheck",
            constraint=models.UniqueConstraint(
                fields=("number",), name="disbursement_check_number_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="disbursementcheck",
            constraint=models.CheckConstraint(
                condition=models.Q(("amount_cents__gt", 0)),
                name="disbursement_check_positive",
            ),
        ),
        # Append-only (CLAUDE.md rule 5): triggers here, grants by core.db_roles.
        *append_only_trigger_operations("ledger_disbursementbatch"),
        *append_only_trigger_operations("ledger_disbursementline"),
        *append_only_trigger_operations("ledger_disbursementdecision"),
        *append_only_trigger_operations("ledger_disbursemententry"),
        *append_only_trigger_operations("ledger_disbursementcheck"),
        *append_only_trigger_operations("ledger_disbursementcheckevent"),
    ]
