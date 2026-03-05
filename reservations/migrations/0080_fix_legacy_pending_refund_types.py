from django.db import migrations


def fix_pending_refund_types(apps, schema_editor):
    """
    Legacy pending refund requests were all migrated as 'full_cancellation'.
    Change them to 'price_adjustment' (safest default — refund only, no cancellations)
    so admins can pick the correct type when approving.
    """
    RefundRequest = apps.get_model('reservations', 'RefundRequest')
    updated = RefundRequest.objects.filter(
        status__in=['requested', 'processing'],
        refund_type='full_cancellation',
    ).update(refund_type='price_adjustment')
    if updated:
        print(f"  Updated {updated} pending refund request(s) from full_cancellation → price_adjustment")


def reverse(apps, schema_editor):
    RefundRequest = apps.get_model('reservations', 'RefundRequest')
    RefundRequest.objects.filter(
        status__in=['requested', 'processing'],
        refund_type='price_adjustment',
    ).update(refund_type='full_cancellation')


class Migration(migrations.Migration):

    dependencies = [
        ("reservations", "0079_migrate_existing_refunds_to_refundrequest"),
    ]

    operations = [
        migrations.RunPython(fix_pending_refund_types, reverse),
    ]
