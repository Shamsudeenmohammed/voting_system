"""Convert legacy plaintext data into the secret-ballot architecture.

1. Every legacy ``Voter.token`` becomes a peppered-hashed
   ``BallotAuthorization`` (tokens stay valid for holders, the database now
   only stores hashes).
2. Every legacy ``Vote`` (voter <-> selections link, plus IP address) becomes
   a sealed ``Ballot``: encrypted selections, random receipt, **no** voter
   reference.  The legacy table is dropped afterwards by elections/0003.

Rollback note: reversing this migration restores the legacy tables (empty of
the converted rows only after elections/0003 is reversed, which is inherently
lossy for dropped rows - see docs/OPERATIONS.md for backups).
"""

import uuid

from django.db import migrations


def convert(apps, schema_editor):
    from ballot import crypto

    hash_token = crypto.hash_token

    Voter = apps.get_model("elections", "Voter")
    Vote = apps.get_model("elections", "Vote")
    Ballot = apps.get_model("ballot", "Ballot")
    BallotBox = apps.get_model("ballot", "BallotBox")
    Authorization = apps.get_model("ballot", "BallotAuthorization")

    db_alias = schema_editor.connection.alias

    # --- 1. hashed authorizations for legacy tokens ------------------------
    issued = 0
    for voter in Voter.objects.using(db_alias).all():
        if not voter.token:
            continue
        Authorization.objects.using(db_alias).update_or_create(
            token_hash=hash_token(voter.token),
            defaults={
                "election_id": voter.election_id,
                "voter_id": voter.id,
                "consumed_at": voter.voted_at,  # legacy votes are already cast
            },
        )
        voter.credentials_issued_at = voter.created_at
        voter.save(
            using=db_alias, update_fields=["credentials_issued_at"]
        )
        issued += 1

    # --- 2. sealed ballots for legacy votes --------------------------------
    fernet = crypto.get_fernet()
    converted = 0
    box_seq: dict[str, int] = {}
    for vote in Vote.objects.using(db_alias).order_by("created_at").iterator():
        election_id = str(vote.election_id)
        seq = box_seq.get(election_id, 0) + 1
        box_seq[election_id] = seq
        submission_id = uuid.uuid4()
        receipt = crypto.new_receipt_code()
        payload = crypto.encrypt_payload(
            {"v": 1, "selections": vote.selections, "legacy": True}, fernet=fernet
        )
        ballot_hash = crypto.ballot_hash(
            election_id=election_id,
            submission_id=submission_id,
            receipt=receipt,
            payload=payload,
            config_hash="",
        )
        Ballot.objects.using(db_alias).create(
            id=uuid.uuid4(),
            election_id=vote.election_id,
            submission_id=submission_id,
            receipt_code=receipt,
            payload=payload,
            ballot_hash=ballot_hash,
            config_hash="",
            station=None,
            box_seq=seq,
            accepted_at=vote.created_at,
        )
        converted += 1

    for election_id, seq in box_seq.items():
        BallotBox.objects.using(db_alias).update_or_create(
            election_id=election_id, defaults={"last_seq": seq}
        )

    print(
        f"[ballot 0002] hashed {issued} legacy token(s), "
        f"sealed {converted} legacy vote(s)"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("ballot", "0001_initial"),
        ("elections", "0002_pollingstation_alter_voter_options_and_more"),
    ]

    operations = [
        migrations.RunPython(convert, migrations.RunPython.noop),
    ]
