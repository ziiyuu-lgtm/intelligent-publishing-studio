"""Stable snapshots for fictional online-first issue proposals."""
import hashlib
import json


def issue_snapshot(issue):
    return {"id": str(issue.pk), "volume": issue.volume, "number": issue.number,
            "year": issue.year, "title": issue.title}


def proposal_token(proposal):
    payload = {"id": str(proposal.pk), "draft": str(proposal.draft_id), "issue": str(proposal.issue_id),
               "proposed_by": str(proposal.proposed_by_id), "source": proposal.source_fingerprint,
               "issue_snapshot": proposal.issue_snapshot}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()
