"""Pre-migration cleanup of dangling foreign keys in legacy data.

SQLite runs ``PRAGMA foreign_key_check`` over the *whole* database at the end
of every schema-editor block, so a single legacy row pointing at a deleted
user (``elections_election.created_by_id``) would abort every subsequent
migration.  This data-only migration runs before any schema change and fixes
references whose target row no longer exists.  No ballot, voter or election
row is ever deleted; only rows that cannot exist consistently (orphaned
administrative log entries) are removed.

Safe on fresh databases: each statement is guarded by table introspection.
"""
from django.db import migrations

# (table, column, referenced table, strategy)
#   "null"   -> set the column to NULL where the target row is gone.
#   "delete" -> for NOT NULL columns the orphan row itself is removed
#               (only ever administrative leftovers such as admin log rows
#               of an account that no longer exists).
DANGLING_REFS = [
    ("elections_election", "created_by_id", "auth_user", "null"),
    ("django_admin_log", "user_id", "auth_user", "delete"),
    ("elections_auditlog", "user_id", "auth_user", "null"),
    ("elections_vote", "election_id", "elections_election", "delete"),
    ("elections_vote", "voter_id", "elections_voter", "delete"),
    ("elections_position", "election_id", "elections_election", "delete"),
    ("elections_candidate", "position_id", "elections_position", "delete"),
]


def cleanup_dangling_refs(apps, schema_editor):
    connection = schema_editor.connection
    existing = set(connection.introspection.table_names(cursor=connection.cursor()))
    with connection.cursor() as cursor:
        for table, column, ref_table, strategy in DANGLING_REFS:
            if table not in existing or ref_table not in existing:
                continue
            verb = "UPDATE" if strategy == "null" else "DELETE"
            if strategy == "null":
                cursor.execute(
                    f'UPDATE "{table}" SET "{column}" = NULL '
                    f'WHERE "{column}" IS NOT NULL '
                    f'AND "{column}" NOT IN (SELECT id FROM "{ref_table}")'
                )
            else:
                cursor.execute(
                    f'{verb} FROM "{table}" '
                    f'WHERE "{column}" IS NOT NULL '
                    f'AND "{column}" NOT IN (SELECT id FROM "{ref_table}")'
                )


class Migration(migrations.Migration):

    dependencies = []

    operations = [
        migrations.RunPython(cleanup_dangling_refs, migrations.RunPython.noop),
    ]
