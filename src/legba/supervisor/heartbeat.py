"""
Heartbeat: Challenge-Response Protocol

The supervisor generates a challenge nonce each cycle. The agent must include
this nonce in its LLM output and return it in the response file, proving the
LLM was actually in the loop.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ..shared.schemas.cycle import Challenge, CycleResponse


class HeartbeatManager:
    """Manages the challenge-response heartbeat protocol."""

    def __init__(self, shared_path: str = "/shared"):
        self._shared = Path(shared_path)
        self._shared.mkdir(parents=True, exist_ok=True)
        self._current_challenge: Challenge | None = None
        self._consecutive_failures: int = 0

    @property
    def challenge_path(self) -> Path:
        return self._shared / "challenge.json"

    @property
    def response_path(self) -> Path:
        return self._shared / "response.json"

    def issue_challenge(self, cycle_number: int, timeout_seconds: int = 300) -> Challenge:
        """Generate and write a new challenge for the agent."""
        # Short nonce (8 hex chars) — full UUIDs cause LLM character-drop errors
        nonce = uuid4().hex[:8]

        challenge = Challenge(
            cycle_number=cycle_number,
            nonce=nonce,
            timeout_seconds=timeout_seconds,
        )

        self.challenge_path.write_text(challenge.model_dump_json(indent=2))
        self._current_challenge = challenge
        return challenge

    @staticmethod
    def compute_expected_nonce(nonce: str, cycle_number: int) -> str:
        """Compute the expected transformed nonce: nonce:cycle_number."""
        return f"{nonce}:{cycle_number}"

    def validate_response(self) -> tuple[bool, CycleResponse | None, str]:
        """
        Read and validate the agent's response.

        Returns (valid, response, error_message).
        """
        if not self.response_path.exists():
            self._consecutive_failures += 1
            return False, None, "No response file found"

        try:
            data = json.loads(self.response_path.read_text())
            response = CycleResponse(**data)
        except Exception as e:
            self._consecutive_failures += 1
            return False, None, f"Failed to parse response: {e}"

        if self._current_challenge is None:
            self._consecutive_failures += 1
            return False, response, "No challenge was issued"

        # Validate transformed nonce
        expected = self.compute_expected_nonce(
            self._current_challenge.nonce,
            self._current_challenge.cycle_number,
        )
        if response.nonce != expected:
            self._consecutive_failures += 1
            return False, response, (
                f"Nonce mismatch: expected={expected}, "
                f"got={response.nonce}"
            )

        # Validate cycle number
        if response.cycle_number != self._current_challenge.cycle_number:
            self._consecutive_failures += 1
            return False, response, (
                f"Cycle mismatch: expected={self._current_challenge.cycle_number}, "
                f"got={response.cycle_number}"
            )

        # Valid response
        self._consecutive_failures = 0
        return True, response, ""

    def cleanup(self) -> None:
        """Remove challenge and response files between cycles."""
        for path in [self.challenge_path, self.response_path]:
            if path.exists():
                path.unlink()

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures
