"""Send one clearly-labelled TEST notification to both recipients (Gate 5).

    python scripts/test_email.py --confirm-send

Provider acceptance is not inbox delivery: both recipients must reply/confirm they received it
before ALERTS_ENABLED is set to true.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import Settings  # noqa: E402
from src.notifier import TEST_BODY, TEST_SUBJECT, Outcome  # noqa: E402


def main(argv: list[str] | None = None, sender_factory=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm-send", action="store_true", help="required: actually send the test emails")
    args = ap.parse_args(argv)
    s = Settings.from_env()
    s.validate_for(need_db=False, need_email=True)
    if not args.confirm_send:
        print(f"Dry run. Would send '{TEST_SUBJECT}' to {len(s.alert_email_to)} recipient(s). "
              "Re-run with --confirm-send to send.")
        return 0
    if sender_factory is None:
        from src.monitor import _default_sender
        sender = _default_sender(s)
    else:
        sender = sender_factory()
    run = uuid.uuid4().hex[:12]
    failures = 0
    try:
        for r in s.alert_email_to:
            res = sender.send(to=r, subject=TEST_SUBJECT, text=TEST_BODY, html_body=None,
                              idempotency_key=f"nyrr-manual-test-{run}-{r}")
            if res.outcome is Outcome.ACCEPTED:
                print(f"  accepted by the mail provider → {r} (message id {res.message_id})")
            else:
                failures += 1
                print(f"  NOT accepted → {r}: {res.error}")
    finally:
        sender.close()
    print("\nAccepted ≠ delivered. Ask both recipients to confirm the email arrived (check spam too).")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
