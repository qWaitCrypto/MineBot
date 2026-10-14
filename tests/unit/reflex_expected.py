"""The verified question changes since the historical fixtures were recorded.

Historical requests stay as recorded; tests apply only these named changes
and compare everything else byte for byte.
"""
import copy


def main_hand_beside_inventory(request, facts):
    """The recorded main hand, said beside the inventory (not in the body sentence)."""
    selected = facts.get("selected_item")
    if not isinstance(selected, str) or facts.get("selected_item_known") is False:
        return request
    request = copy.deepcopy(request)
    state = {}
    for key, value in request["state"].items():
        if key == "body" and "main_hand" not in state:
            state["main_hand"] = selected or "nothing"
        state[key] = value
        if key == "inventory":
            state["main_hand"] = selected or "nothing"
    request["state"] = state
    return request


def intent_question_while_running(request, intent):
    """While work runs for an intent, the next step is decided by question a."""
    request = copy.deepcopy(request)
    questions = request["questions"]
    questions["q"]["criteria"].pop("next_step", None)
    ordered = {"q": questions["q"], "a": {"type": "choice", "instructions": (
        "Compare each part of the result the Agent wants with the inventory and Body facts above. "
        "Do the facts already show every part of it?"
    ), "criteria": {
        "shown": f"Yes: the facts above already show “{intent}”.",
        "not_shown": f"No: the facts above do not show all of “{intent}” yet.",
    }}}
    ordered.update({key: value for key, value in questions.items() if key != "q"})
    request["questions"] = ordered
    return request
