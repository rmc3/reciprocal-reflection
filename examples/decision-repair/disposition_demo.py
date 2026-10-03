"""One disposable, explicitly inert illustration of ending a proposal."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from evidence_action import EvidenceEngine
from examples import envelope, contract, proposal


def demo():
    with TemporaryDirectory(prefix="disposition-demo-") as folder:
        e = EvidenceEngine.initialize(Path(folder) / "ledger.sqlite", envelope(1), contract())
        before = e.propose(proposal())
        blocked_inquiry = e.propose(proposal("inspect-before", "inspect"))
        withdrawn = e.withdraw("apply-1", "fixture-owner", "Do not pursue the unsupported consequence.",
                               "Underlying question unresolved; no review assigned.")
        inquiry = e.propose(proposal("inspect-after", "inspect"))
        e.execute("inspect-after")
        checked = e.reconcile("inspect-after")
        e.close_envelope("fixture", "Bounded demonstration complete.")
        return {"demonstration": "inert software only", "before": before,
                "inquiry_before_withdrawal": blocked_inquiry, "withdrawal": withdrawn,
                "inquiry_after_withdrawal": inquiry, "inquiry_effect": checked,
                "withdrawn_action_after_closure": e.action("apply-1"),
                "ledger": e.export(), "semantic_verification": "not_performed",
                "review_assignment": "not_represented"}


if __name__ == "__main__":
    print(json.dumps(demo(), indent=2))
