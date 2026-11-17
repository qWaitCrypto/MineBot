"""Hold the real Python ownership policy for the client hut fixture.

The Java client has no ledger IPC operation. Do not invent one here: restore
the own-block rows through the same production loader used after a restart,
verify the real policy protects them, and keep that policy alive during the
client's navigation. stdin EOF ends the fixture.
"""

import json
import sys

from minebot.app.ownership_ledger import OwnershipLedger
from minebot.contract import BreakContext
from minebot.game.governance import GovernancePolicy

positions = json.loads(sys.stdin.readline())
policy = GovernancePolicy(bot_name="BodyTestBot")
ledger = OwnershipLedger(policy)
ledger.load(
    {"pos": pos, "block_type": "oak_planks", "purpose": "place", "dimension": "minecraft:overworld"}
    for pos in positions
)
print(json.dumps({
    "owned": len(policy.bot_placements),
    "protected": sum(not policy.can_break(tuple(pos), "oak_planks", BreakContext.TRAVEL).allowed for pos in positions),
    "source": "OwnershipLedger.load",
}), flush=True)
sys.stdin.read()
