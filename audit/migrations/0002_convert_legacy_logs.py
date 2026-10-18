"""Convert legacy ``elections.AuditLog`` rows into the hash-chained store.

The chain "global" is rebuilt from the legacy rows in timestamp order so the
new chain verification covers historical activity as well.  Rows containing
sensitive keys (defensive filter) are stored without those keys.
"""

from django.db import migrations


def convert(apps, schema_editor):
    from audit import services
    from audit.models import AuditChain, AuditEvent

    AuditLog = apps.get_model("elections", "AuditLog")
    User = apps.get_model("auth", "User")

    db_alias = schema_editor.connection.alias
    users = {
        u.id: (u.email or u.username or str(u.id))
        for u in User.objects.using(db_alias).all()
    }

    chain_key = "global"
    seq = 0
    prev_hash = services.GENESIS
    created = 0

    for log in AuditLog.objects.using(db_alias).order_by("timestamp", "id").iterator():
        details = dict(log.details or {})
        try:
            services._check_details(details)
        except ValueError:
            details = {k: v for k, v in details.items()
                       if str(k).lower() not in services.FORBIDDEN_DETAIL_KEYS}
        seq += 1
        created_at = log.timestamp
        actor_label = users.get(log.user_id, "")
        core = {
            "action": log.action,
            "actor": str(log.user_id) if log.user_id else "",
            "actor_label": actor_label,
            "organization": "",
            "election": "",
            "object_type": "",
            "object_id": "",
            "reason": "",
            "details": details,
            "ip_hash": "",
        }
        event_hash = services._event_hash(
            chain_key=chain_key,
            seq=seq,
            prev_hash=prev_hash,
            core={**core, "created_at": created_at.isoformat()},
        )
        AuditEvent.objects.using(db_alias).create(
            chain_key=chain_key,
            seq=seq,
            created_at=created_at,
            action=core["action"],
            actor_id=log.user_id,
            actor_label=actor_label,
            organization=None,
            election=None,
            object_type="",
            object_id="",
            reason="",
            details=details,
            ip_hash="",
            prev_hash=prev_hash,
            event_hash=event_hash,
        )
        prev_hash = event_hash
        created += 1

    AuditChain.objects.using(db_alias).update_or_create(
        chain_key=chain_key,
        defaults={"head_hash": prev_hash, "last_seq": seq},
    )
    print(f"[audit 0002] converted {created} legacy audit log(s) into chain '{chain_key}'")


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
        ("elections", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(convert, migrations.RunPython.noop),
    ]
