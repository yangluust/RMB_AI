#!/usr/bin/env python3
"""
Review bridge: a signed file handoff between the orchestrator and the host
that runs the project's main chat.

The orchestrator has no credentials for any chat or mail service and sends
nothing itself. It writes a signed notice file to an outbox folder. The
host (outside this repository) posts the notice in the main chat, then
writes a signed acknowledgement (ack) to the inbox folder. The owner's
decision comes back the same way, as a signed decision file.

    outbox/<event_id>.json          notice, written by the orchestrator
    inbox/<event_id>.ack.json       ack, written by the host after posting
    inbox/<decision_id>.decision.json   decision, written by the host

All three are JSON objects signed with HMAC-SHA256 over their canonical
JSON, with a secret both sides hold. A notice without a valid ack was not
delivered: console output is never counted as delivery.

build_ack(), build_decision() and HostReceiver are the reference for the
host side. They are used by the tests as an offline stand-in for the host.
This module does not post anything anywhere.
"""

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, Optional

SCHEMA = "rmb-review-bridge/1"
MIN_SECRET_LENGTH = 16
SIGNATURE_FIELDS = ("payload_sha256", "signature")

ACTION_ACCEPT = "accept"
ACTION_START_STAGE = "start_stage"
ACTION_ITERATE = "iterate"
ACTION_REDO = "redo"
ACTION_RAISE_LIMIT = "raise_limit"
ACTION_HALT = "halt"
ACTIONS = (
    ACTION_ACCEPT, ACTION_START_STAGE, ACTION_ITERATE, ACTION_REDO,
    ACTION_RAISE_LIMIT, ACTION_HALT,
)
# The parameters a decision may carry, by action. Anything else is refused.
ACTION_PARAMS = {
    ACTION_ACCEPT: (),
    ACTION_START_STAGE: (),
    ACTION_ITERATE: ("feedback", "base_commit", "approve_batch"),
    ACTION_REDO: (),
    ACTION_RAISE_LIMIT: ("by",),
    ACTION_HALT: ("reason",),
}


class BridgeError(Exception):
    """A notice, ack or decision could not be written, read or trusted."""


def canonical_json(record: Any) -> bytes:
    """The bytes that are hashed and signed: sorted keys, no extra spaces, UTF-8."""
    return json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def payload_sha256(record: dict) -> str:
    """SHA-256 of a record's canonical JSON, without its signature fields."""
    unsigned = {key: value for key, value in record.items() if key not in SIGNATURE_FIELDS}
    return hashlib.sha256(canonical_json(unsigned)).hexdigest()


def _mac(secret: str, text: str) -> str:
    return hmac.new(secret.encode("utf-8"), text.encode("utf-8"), hashlib.sha256).hexdigest()


def sign(record: dict, secret: str) -> dict:
    """Return the record with its payload hash and signature added."""
    digest = payload_sha256(record)
    return {**record, "payload_sha256": digest, "signature": _mac(secret, digest)}


def verify(record: Any, secret: str, expected_type: str) -> None:
    """
    Raises:
        BridgeError: unless the record is a signed object of this schema
            and type whose content matches its hash and signature.
    """
    if not isinstance(record, dict):
        raise BridgeError(f"there is no '{expected_type}' record (it is not a JSON object)")
    if record.get("schema") != SCHEMA or record.get("type") != expected_type:
        raise BridgeError(
            f"it is not a '{expected_type}' record of schema {SCHEMA} "
            f"(schema {record.get('schema')!r}, type {record.get('type')!r})"
        )
    digest = record.get("payload_sha256")
    signature = record.get("signature")
    if not isinstance(digest, str) or not isinstance(signature, str):
        raise BridgeError("it is not signed (payload_sha256 and signature are required)")
    if not hmac.compare_digest(digest, payload_sha256(record)):
        raise BridgeError("its content does not match its payload_sha256")
    if not hmac.compare_digest(signature, _mac(secret, digest)):
        raise BridgeError("its signature is not valid for the shared secret")


def stable_id(prefix: str, identity: dict) -> str:
    """An ID that is the same whenever the same identity is given."""
    return f"{prefix}-{hashlib.sha256(canonical_json(identity)).hexdigest()[:24]}"


def decision_token(secret: str, event_id: str, action: str) -> str:
    """The token that ties a decision to one notice and one action."""
    return _mac(secret, f"decision-token|{event_id}|{action}")[:32]


def write_json_once(path: Path, record: dict) -> bool:
    """Write a record to a new file in one step. Writing the same record again changes nothing.

    Returns:
        True if the file was written, False if it already held this record.

    Raises:
        BridgeError: if the file holds something else, or cannot be written.
    """
    content = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
    try:
        if path.exists():
            if path.read_bytes() == content:
                return False
            raise BridgeError(
                f"{path} already exists with other content. A record is never "
                f"overwritten; remove the file yourself only if you know it is wrong."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        with open(temporary, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise BridgeError(f"{path} could not be written: {error}") from error
    return True


def read_json(path: Path) -> Any:
    """
    Raises:
        BridgeError: if the file cannot be read as JSON.
    """
    try:
        return json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BridgeError(f"{path} could not be read as JSON: {error}") from error


class ReviewBridge:
    """The orchestrator's side of the handoff: one outbox and one inbox folder."""

    def __init__(self, directory: Path, secret: Optional[str], secret_env: str = ""):
        """
        Args:
            directory: Folder that holds `outbox/` and `inbox/`.
            secret: The shared secret, or None when it is not set.
            secret_env: Name of the environment variable the secret comes
                from; used in messages only.
        """
        self.directory = Path(directory)
        self.secret = secret
        self.secret_env = secret_env

    @property
    def outbox(self) -> Path:
        return self.directory / "outbox"

    @property
    def inbox(self) -> Path:
        return self.directory / "inbox"

    def check_ready(self) -> None:
        """Check that notices could be written and signed. Writes no notice.

        Raises:
            BridgeError: if the secret is missing or too short, or a folder
                cannot be created.
        """
        self.check_secret()
        try:
            self.outbox.mkdir(parents=True, exist_ok=True)
            self.inbox.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise BridgeError(
                f"The review bridge folders under {self.directory} could not be created: {error}"
            ) from error

    def check_secret(self) -> None:
        """Check that the shared secret is set. Writes nothing.

        Raises:
            BridgeError: if it is missing or too short.
        """
        self._secret()

    def _secret(self) -> str:
        if not self.secret or len(self.secret) < MIN_SECRET_LENGTH:
            source = (
                f"the environment variable {self.secret_env}" if self.secret_env
                else "the review bridge"
            )
            raise BridgeError(
                f"The shared secret of {source} is not set, or is shorter than "
                f"{MIN_SECRET_LENGTH} characters. Without it no review notice can "
                f"be signed, so no output could be announced for review."
            )
        return self.secret

    def notice_path(self, event_id: str) -> Path:
        return self.outbox / f"{event_id}.json"

    def ack_path(self, event_id: str) -> Path:
        return self.inbox / f"{event_id}.ack.json"

    def sign_notice(self, notice: dict) -> dict:
        """Return the notice with a decision token per allowed action, signed."""
        secret = self._secret()
        with_tokens = {
            **notice,
            "allowed_decisions": [
                {**entry, "token": decision_token(secret, notice["event_id"], entry["action"])}
                for entry in notice["allowed_decisions"]
            ],
        }
        return sign(record={**with_tokens, "schema": SCHEMA, "type": "notice"}, secret=secret)

    def emit(self, notice: dict) -> Path:
        """Write a signed notice to the outbox. Emitting it again changes nothing.

        Raises:
            BridgeError: if it is not validly signed or cannot be written.
        """
        verify(record=notice, secret=self._secret(), expected_type="notice")
        path = self.notice_path(event_id=notice["event_id"])
        write_json_once(path=path, record=notice)
        return path

    def find_ack(self, notice: dict) -> Optional[dict]:
        """Return the host's valid ack of a notice, or None if there is none yet.

        Raises:
            BridgeError: if an ack file is there but cannot be trusted: bad
                signature, another notice, or no chat message reference.
        """
        path = self.ack_path(event_id=notice["event_id"])
        if not path.exists():
            return None
        ack = read_json(path=path)
        try:
            verify(record=ack, secret=self._secret(), expected_type="ack")
            if ack.get("event_id") != notice["event_id"]:
                raise BridgeError(f"it acknowledges {ack.get('event_id')!r}, not this notice")
            if ack.get("event_payload_sha256") != notice["payload_sha256"]:
                raise BridgeError(
                    "it acknowledges other content than this notice has "
                    "(event_payload_sha256 differs)"
                )
            chat = ack.get("chat")
            if not (
                isinstance(chat, dict) and chat.get("channel") == "main"
                and isinstance(chat.get("message_ref"), str) and chat["message_ref"].strip()
            ):
                raise BridgeError(
                    "it names no message in the main chat (chat.channel must be "
                    "'main' and chat.message_ref must be set)"
                )
        except BridgeError as error:
            raise BridgeError(f"The ack file {path} is not accepted: {error}") from error
        return ack

    def read_decision(self, path: Path) -> dict:
        """Read a decision file and check its signature and form.

        Raises:
            BridgeError: if it cannot be read or trusted.
        """
        decision = read_json(path=path)
        try:
            verify(record=decision, secret=self._secret(), expected_type="decision")
            self.check_decision_form(decision=decision)
        except BridgeError as error:
            raise BridgeError(f"The decision file {path} is not accepted: {error}") from error
        return decision

    def check_decision_form(self, decision: dict) -> None:
        """
        Raises:
            BridgeError: unless the decision names a known action, carries
                only that action's parameters, has the token of its notice
                and action, and quotes a message of the main chat.
        """
        action = decision.get("action")
        if action not in ACTIONS:
            raise BridgeError(f"unknown action {action!r}; the actions are {', '.join(ACTIONS)}")
        params = decision.get("params")
        if not isinstance(params, dict):
            raise BridgeError("params must be an object")
        unexpected = sorted(set(params) - set(ACTION_PARAMS[action]))
        if unexpected:
            raise BridgeError(f"action '{action}' takes no parameter(s) {unexpected}")
        expected_token = decision_token(self._secret(), str(decision.get("event_id")), action)
        if not hmac.compare_digest(str(decision.get("token")), expected_token):
            raise BridgeError("its token is not the token of this notice and action")
        if decision.get("decision_id") != decision_id_of(decision=decision):
            raise BridgeError("its decision_id does not match its content")
        chat = decision.get("chat")
        if not (
            isinstance(chat, dict)
            and isinstance(chat.get("message_ref"), str) and chat["message_ref"].strip()
            and isinstance(chat.get("quote"), str) and chat["quote"].strip()
        ):
            raise BridgeError(
                "it does not quote the owner's written message (chat.message_ref "
                "and chat.quote must be set)"
            )

    def verify_stored(self, record: Any, expected_type: str) -> None:
        """Check a notice, ack or decision that was stored in a manifest.

        Raises:
            BridgeError: if it is not validly signed.
        """
        verify(record=record, secret=self._secret(), expected_type=expected_type)


def decision_id_of(decision: dict) -> str:
    """The ID a decision must carry: derived from everything else it says."""
    identity = {
        key: value for key, value in decision.items()
        if key not in (*SIGNATURE_FIELDS, "decision_id")
    }
    return stable_id(prefix="dec", identity=identity)


# ---------------------------------------------------------------------------
# Host side: reference implementation, used by the tests as an offline
# stand-in. The real host is outside this repository.
# ---------------------------------------------------------------------------

def build_ack(
    notice: dict, secret: str, message_ref: str, posted_at: str, voice: bool = False,
) -> dict:
    """Return the ack the host writes AFTER it has posted the notice in the main chat."""
    return sign(
        record={
            "schema": SCHEMA,
            "type": "ack",
            "event_id": notice["event_id"],
            "event_payload_sha256": notice["payload_sha256"],
            "chat": {"channel": "main", "message_ref": message_ref, "posted_at": posted_at},
            "voice": voice,
        },
        secret=secret,
    )


def build_decision(
    notice: dict,
    secret: str,
    action: str,
    message_ref: str,
    quote: str,
    decided_at: str,
    params: Optional[dict] = None,
) -> dict:
    """Return the decision the host writes after the owner's written message.

    The decision is bound to the notice it answers: project, stage, round,
    commit and config are copied from the notice, and the token is the one
    the notice lists for this action.

    Raises:
        BridgeError: if the notice does not allow this action.
    """
    tokens = {entry["action"]: entry["token"] for entry in notice["allowed_decisions"]}
    if action not in tokens:
        raise BridgeError(
            f"The notice {notice['event_id']} does not allow '{action}'. "
            f"It allows: {', '.join(tokens) or 'nothing'}."
        )
    record = {
        "schema": SCHEMA,
        "type": "decision",
        "event_id": notice["event_id"],
        "event_payload_sha256": notice["payload_sha256"],
        "project_id": notice["project_id"],
        "stage": notice["stage"]["name"],
        "round": notice["round"],
        "commit": notice["commits"]["output"],
        "config_sha256": notice["config_sha256"],
        "action": action,
        "token": tokens[action],
        "params": params or {},
        "chat": {"message_ref": message_ref, "quote": quote},
        "decided_at": decided_at,
    }
    record["decision_id"] = decision_id_of(decision=record)
    return sign(record=record, secret=secret)


class HostReceiver:
    """How a host must receive notices: verify, then deduplicate by ID AND content.

    The same notice may be written or read more than once. A notice with
    an ID that was already received is a duplicate only if its content is
    the same; the same ID with other content is refused, never posted.
    """

    def __init__(self, secret: str):
        self.secret = secret
        self.received: dict[str, str] = {}

    def receive(self, notice: Any) -> bool:
        """Return True if the notice is new and must be posted, False for a duplicate.

        Raises:
            BridgeError: if the notice is not validly signed, or has the ID
                of an earlier notice with other content.
        """
        verify(record=notice, secret=self.secret, expected_type="notice")
        event_id = notice["event_id"]
        if event_id in self.received:
            if self.received[event_id] != notice["payload_sha256"]:
                raise BridgeError(
                    f"A notice with ID {event_id} was already received with other "
                    f"content. It is not posted."
                )
            return False
        self.received[event_id] = notice["payload_sha256"]
        return True
