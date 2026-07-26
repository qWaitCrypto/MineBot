"""Production adapter pieces for the ``fakeplayer-body/1`` Java Body.

``GovernanceAnswerer`` is the Tier-2 authority of the two-tier governance
contract: it binds every ``MUTATION_PROPOSAL`` to the real
:class:`~minebot.game.governance.GovernancePolicy` decision — protected
regions, bot-placement ledger, natural-region rules, and the structure-risk
assessor's authoritative voxel re-read. It never caches a permit and answers
exactly one verdict per proposal; staying silent is safe because the Java
side times a missing verdict out into a denial.

The full threaded Body-contract client lands with agent tool routing; probes
and early integrations drive :class:`~minebot.game.java_body_protocol.JavaBodyProtocol`
directly and plug this answerer into their proposal path.
"""

from __future__ import annotations

from minebot.contract.governance import BreakContext
from minebot.game.governance import GovernancePolicy
from minebot.game.java_body_protocol import ServerProposal


class GovernanceAnswerer:
    """Answers Java Body mutation proposals with the production decision."""

    def __init__(self, policy: GovernancePolicy, *, context: BreakContext = BreakContext.COLLECT) -> None:
        self._policy = policy
        self._context = context

    def verdict(self, proposal: ServerProposal) -> tuple[bool, str]:
        if proposal.kind != "break":
            # Only mutation kinds with a mapped governance decision may pass;
            # anything else is denied, never guessed.
            return False, f"unsupported_mutation_kind:{proposal.kind}"
        decision = self._policy.can_break(
            (proposal.x, proposal.y, proposal.z),
            proposal.block_id,
            self._context,
            explicit_target=True,
        )
        return decision.allowed, decision.reason
