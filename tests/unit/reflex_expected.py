"""The verified question changes since the historical fixtures were recorded.

Historical requests stay as recorded; tests apply only these named changes
and compare everything else byte for byte.
"""
import copy

from minebot.app import reflex_question as rq


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
    """While work runs for an intent, next_step is no longer beside keep_going;
    whether the intent is shown is its own request (returned second)."""
    request = copy.deepcopy(request)
    request["questions"]["q"]["criteria"].pop("next_step", None)
    return request, rq.done_question(request["state"], intent)


def protecting_while_running(request, facts, choice):
    """A running protective action: only the protection facts, the protective
    choice again with the running action as one option, no keep_going."""
    request = copy.deepcopy(request)
    request["state"] = {key: request["state"][key] for key in rq.PROTECTION_FACTS if request["state"].get(key)}
    request["questions"] = {"q": {"type": "choice", "instructions": "Choose how the Body protects itself right now.",
                                  "criteria": rq.protecting_options(facts, choice)}}
    return request
