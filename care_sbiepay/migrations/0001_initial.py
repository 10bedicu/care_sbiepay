import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("emr", "0080_alter_activitydefinition_category_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="SbiEpayPayment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("external_id", models.UUIDField(db_index=True, default=uuid.uuid4, unique=True)),
                ("created_date", models.DateTimeField(auto_now_add=True, db_index=True, null=True)),
                ("modified_date", models.DateTimeField(auto_now=True, db_index=True, null=True)),
                ("deleted", models.BooleanField(db_index=True, default=False)),
                ("order_number", models.CharField(max_length=15, unique=True)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14)),
                ("status", models.CharField(choices=[("created", "Created"), ("paid", "Paid"), ("failed", "Failed"), ("expired", "Expired"), ("cancelled", "Cancelled")], default="created", max_length=16)),
                ("reference", models.CharField(blank=True, default="", max_length=255)),
                ("payment_url", models.TextField(blank=True, default="")),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="emr.invoice")),
            ],
            options={
                "abstract": False,
            },
        ),
    ]
