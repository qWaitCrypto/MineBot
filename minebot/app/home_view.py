"""What the Agent reads of its home each model call (vision-and-home-20261028.md §7.7).

The home, its outposts, the storage plan and what players said is theirs come
from the life profile, as the Agent wrote them.  What the containers held,
which places the Body used or placed, the respawn point and the portal pairs
are the Body's own record (``world_record``).  This section puts them side by
side with where the Body is now; it judges nothing.
"""
from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping
from typing import Any

from minebot.app.world_record import block_name, block_pos, dimension_id, dimension_words, is_container, is_place_block

HOME_SECTION_CHARS = 3000
# Kinds listed per container, most first.
KINDS_SHOWN = 8
# Kinds listed for all of home's containers together, in the notice on waking after a death.
STORAGE_KINDS_SHOWN = 32
_LINE_CHARS = 300
OVERWORLD = "minecraft:overworld"
NO_HOME = "HOME: You have no home in this world yet."
_COMPASS = ("north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west")


def home_section(profile: Mapping[str, Any], world: Mapping[str, Any], *, here: Mapping[str, Any] | None = None,
                 own_places: Iterable[Mapping[str, Any]] = (), now: float | None = None) -> str:
    """The HOME section; one sentence while there is no home."""
    home = profile.get("home") if isinstance(profile.get("home"), Mapping) else None
    if home is None or block_pos(home.get("pos")) is None:
        return NO_HOME
    now = time.time() if now is None else now
    here = here or {}
    position = (dimension_id(here.get("dimension")), block_pos(here.get("pos")))
    portals = [link for link in world.get("portals") or () if isinstance(link, Mapping)]
    places = _places(world, own_places)
    respawn = world.get("respawn") if isinstance(world.get("respawn"), Mapping) else None

    sites = [("Home", home)] + [
        (f"Outpost “{outpost.get('name')}”", outpost)
        for outpost in profile.get("outposts") or () if isinstance(outpost, Mapping)]
    grouped: list[list[dict[str, Any]]] = [[] for _ in sites]
    for _label, site in sites:
        for row in site.get("storage") or ():
            key = (_dimension(site), *row["pos"])
            place = places.setdefault(key, {"dimension": key[0], "pos": list(row["pos"])})
            place["plan"] = row
    other: list[dict[str, Any]] = []
    for place in places.values():
        index = next((index for index, (_label, site) in enumerate(sites) if _belongs(place, site)), None)
        if index is not None:
            grouped[index].append(place)
        elif not place.get("gone"):
            other.append(place)
    home_dimension, home_pos = _dimension(home), block_pos(home["pos"])
    other.sort(key=lambda place: (place["dimension"] != home_dimension,
                                  math.dist(place["pos"], home_pos) if place["dimension"] == home_dimension else 0))

    head = ["HOME (your places; contents, use and the respawn point are the Body's own record):",
            _site_line("Home", home, position, portals), _respawn_line(respawn, world.get("world_spawn"), now)]
    # [rank, header, lines, omitted]: the highest rank loses lines first when the section is too long.
    sections: list[list[Any]] = [[1, "- At home:" if grouped[0] else None,
                                  [_place_line(place, respawn, now) for place in grouped[0]], 0]]
    for (label, site), group in zip(sites[1:], grouped[1:]):
        sections.append([3, _site_line(label, site, position, portals),
                         [_place_line(place, respawn, now) for place in group], 0])
    belongings = [_belonging_line(item) for item in profile.get("belongings") or () if isinstance(item, Mapping)]
    sections.append([2, "- What players told you is theirs or shared:" if belongings else None, belongings, 0])
    sections.append([4, "- Other places the Body knows:" if other else None,
                     [_place_line(place, respawn, now, home=(home_dimension, home_pos)) for place in other], 0])
    while True:
        text = _render(head, sections)
        if len(text) <= HOME_SECTION_CHARS:
            return text
        trimmable = [section for section in sections if section[2]]
        if not trimmable:
            return text[:HOME_SECTION_CHARS - 40] + "\n- ... cut; read_life shows the rest."
        section = max(trimmable, key=lambda item: item[0])
        section[2].pop()
        section[3] += 1


def _render(head: list[str], sections: list[list[Any]]) -> str:
    lines = list(head)
    for _rank, header, rows, omitted in sections:
        if header is not None:
            lines.append(header)
        lines.extend(rows)
        if omitted:
            lines.append(f"  - ... {omitted} more not shown.")
    return "\n".join(lines)


def _places(world: Mapping[str, Any], own_places: Iterable[Mapping[str, Any]]) -> dict[tuple, dict[str, Any]]:
    """The record's places, most recently touched first, then own placed blocks it has not seen used."""
    places: dict[tuple, dict[str, Any]] = {}
    for place in world.get("places") or ():
        if isinstance(place, Mapping) and block_pos(place.get("pos")) is not None:
            places[(place["dimension"], *place["pos"])] = dict(place)
    for own in own_places:
        where = block_pos(own.get("pos"))
        if where is None or not is_place_block(own.get("block")):
            continue
        key = (dimension_id(own.get("dimension")) or OVERWORLD, *where)
        place = places.setdefault(key, {"dimension": key[0], "pos": where, "block": block_name(own["block"])})
        place["placed"] = True
    return places


def _belongs(place: Mapping[str, Any], site: Mapping[str, Any]) -> bool:
    if place["dimension"] != _dimension(site):
        return False
    if any(row["pos"] == place["pos"] for row in site.get("storage") or ()):
        return True
    area = site.get("area")
    if not isinstance(area, Mapping):
        return False
    low = [min(a, b) for a, b in zip(area["from"], area["to"])]
    high = [max(a, b) for a, b in zip(area["from"], area["to"])]
    return all(lo <= value <= hi for lo, value, hi in zip(low, place["pos"], high))


def _site_line(label: str, site: Mapping[str, Any], position: tuple, portals: list[Mapping[str, Any]]) -> str:
    dimension, pos = _dimension(site), block_pos(site["pos"])
    name = f" “{site['name']}”" if site.get("name") and label == "Home" else ""
    text = f"- {label}{name}: {_xyz(pos)} in {_dim(dimension)}"
    if not site.get("dimension"):
        text += " (no dimension written, so the overworld)"
    if isinstance(site.get("area"), Mapping):
        text += f", area {_xyz(site['area']['from'])} to {_xyz(site['area']['to'])}"
    text += "; " + _way(dimension, pos, position, portals) + "."
    if site.get("note"):
        text += f" Note: {site['note']}"
    return text[:_LINE_CHARS]


def _way(dimension: str, pos: list[int], position: tuple, portals: list[Mapping[str, Any]]) -> str:
    """How far, or through which known portal; never a guess."""
    here_dimension, here = position
    if here_dimension is None or here is None:
        return "where the Body is now is not known"
    if here_dimension == dimension:
        offset = _offset(here, pos)
        return offset if offset.startswith("right here") else offset + " from you"
    ends = [(link[a], link[b]) for link in portals for a, b in (("from", "to"), ("to", "from"))
            if link[a]["dimension"] == here_dimension and link[b]["dimension"] == dimension]
    if not ends:
        return f"you are in {_dim(here_dimension)} and know no portal from here to {_dim(dimension)}"
    near, far = min(ends, key=lambda pair: math.dist(here, pair[0]["pos"]))
    return (f"you are in {_dim(here_dimension)}; the portal at {_xyz(near['pos'])}, {_offset(here, near['pos'])} "
            f"from you, comes out at {_xyz(far['pos'])} in {_dim(dimension)}, "
            f"{round(math.dist(far['pos'], pos))} blocks from there")


def _place_line(place: Mapping[str, Any], respawn: Mapping[str, Any] | None, now: float,
                home: tuple | None = None) -> str:
    block = place.get("block") or "container"
    text = f"  - {block} {_xyz(place['pos'])}"
    if home is not None:
        text += (f", {round(math.dist(place['pos'], home[1]))} blocks from home" if place["dimension"] == home[0]
                 else f" in {_dim(place['dimension'])}")
    facts: list[str] = []
    gone = place.get("gone")
    if isinstance(gone, Mapping):
        facts.append(f"gone {_ago(gone.get('t'), now)} ({gone.get('reason')})")
    else:
        if isinstance(place.get("items"), Mapping):
            seen = _contents(place["items"])
            if not place.get("complete"):
                seen += " (not every slot read)"
            facts.append(f"{seen}, looked {_ago((place.get('looked') or {}).get('t'), now)}")
        elif is_container(block) or place.get("plan"):
            facts.append("not looked into yet")
        if isinstance(place.get("used"), Mapping):
            facts.append(f"last used {_ago(place['used'].get('t'), now)} by {place['used'].get('source')}")
        if place.get("placed"):
            facts.append("you placed it")
        if respawn and not respawn.get("lost") and respawn.get("dimension") == place["dimension"] \
                and respawn.get("pos") == place["pos"]:
            facts.append("your respawn point")
    plan = place.get("plan")
    if isinstance(plan, Mapping):
        facts.append("planned for " + ", ".join(plan["holds"]) + (f" ({plan['note']})" if plan.get("note") else ""))
    return (text + (": " + "; ".join(facts) if facts else "") + ".")[:_LINE_CHARS]


FURNACE_LINES_SHOWN = 6
# Furnaces this close together are told as one group once there are many.
FURNACE_GROUP_BLOCKS = 8.0
FURNACES_HEAD = ("FURNACES with something in them (the Body's own record of what it put in and last saw; a "
                 "furnace turns one item every 10 seconds while its area is loaded, that is while you are "
                 "within a few chunks of it; clear_furnace(take='output') takes out what has finished):")


def furnace_section(world: Mapping[str, Any], *, here: Mapping[str, Any] | None = None,
                    now: float | None = None) -> str:
    """Every furnace the record says still holds input or output; empty text when none does."""
    now = time.time() if now is None else now
    here = here or {}
    position = (dimension_id(here.get("dimension")), block_pos(here.get("pos")))
    portals = [link for link in world.get("portals") or () if isinstance(link, Mapping)]
    held = [place for place in world.get("places") or ()
            if isinstance(place, Mapping) and not place.get("gone") and _furnace_holds(place)]
    if not held:
        return ""
    if len(held) <= FURNACE_LINES_SHOWN:
        lines = [_furnace_line(place, position, portals, now) for place in held]
    else:
        lines = [_furnace_group_line(group, position, portals, now) for group in _furnace_groups(held)]
        if len(lines) > FURNACE_LINES_SHOWN:
            lines = lines[:FURNACE_LINES_SHOWN] + [f"- ... {len(lines) - FURNACE_LINES_SHOWN} more groups not shown."]
    return "\n".join([FURNACES_HEAD, *lines])


def _furnace_holds(place: Mapping[str, Any]) -> bool:
    furnace = place.get("furnace")
    slots = furnace.get("slots") if isinstance(furnace, Mapping) else None
    return isinstance(slots, Mapping) and bool(slots.get("input") or slots.get("output"))


def _slot_text(slot: Any) -> str:
    return f"{slot['count']} {slot['item']}" if isinstance(slot, Mapping) else ""


def _furnace_line(place: Mapping[str, Any], position: tuple, portals: list[Mapping[str, Any]], now: float) -> str:
    furnace = place["furnace"]
    slots = furnace.get("slots") or {}
    loaded = furnace.get("loaded") if isinstance(furnace.get("loaded"), Mapping) else None
    text = f"- {place.get('block') or 'furnace'} {_xyz(place['pos'])}, " + _way(place["dimension"], place["pos"], position, portals)
    facts: list[str] = []
    if loaded is not None:
        put = loaded.get("slots") or {}
        what = _slot_text(put.get("input")) + (f" with {_slot_text(put.get('fuel'))}" if put.get("fuel") else "")
        facts.append(f"{what} put in {_ago(loaded.get('t'), now)}")
    if loaded is not None and furnace.get("last_was_load"):
        facts.append("not looked into since")
    else:
        seen = [part for part in (
            f"{_slot_text(slots.get('output'))} finished" if slots.get("output") else "",
            f"{_slot_text(slots.get('input'))} not yet" if slots.get("input") else "",
            f"{_slot_text(slots.get('fuel'))} fuel left" if slots.get("fuel") else "") if part]
        facts.append(f"looked {_ago(furnace.get('t'), now)}: " + ", ".join(seen))
    return (text + ": " + "; ".join(facts) + ".")[:_LINE_CHARS]


def _furnace_groups(held: list[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    groups: list[list[Mapping[str, Any]]] = []
    for place in held:
        for group in groups:
            if any(other["dimension"] == place["dimension"]
                   and math.dist(other["pos"], place["pos"]) <= FURNACE_GROUP_BLOCKS for other in group):
                group.append(place)
                break
        else:
            groups.append([place])
    return groups


def _furnace_group_line(group: list[Mapping[str, Any]], position: tuple, portals: list[Mapping[str, Any]],
                        now: float) -> str:
    if len(group) == 1:
        return _furnace_line(group[0], position, portals, now)
    first = group[0]
    put: dict[str, int] = {}
    finished: dict[str, int] = {}
    last_put = None
    unlooked = 0
    for place in group:
        furnace = place["furnace"]
        loaded = furnace.get("loaded") if isinstance(furnace.get("loaded"), Mapping) else None
        if loaded is not None:
            slot = (loaded.get("slots") or {}).get("input")
            if isinstance(slot, Mapping):
                put[slot["item"]] = put.get(slot["item"], 0) + slot["count"]
            last_put = max(last_put or 0, loaded.get("t") or 0)
            unlooked += bool(furnace.get("last_was_load"))
        output = (furnace.get("slots") or {}).get("output")
        if isinstance(output, Mapping):
            finished[output["item"]] = finished.get(output["item"], 0) + output["count"]
    text = (f"- {len(group)} furnaces around {_xyz(first['pos'])}, "
            + _way(first["dimension"], first["pos"], position, portals) + ": ")
    facts = []
    if put:
        facts.append(", ".join(f"{count} {item}" for item, count in put.items()) + " put in"
                     + (f", the last {_ago(last_put, now)}" if last_put else ""))
    if finished:
        facts.append(", ".join(f"{count} {item}" for item, count in finished.items()) + " seen finished")
    if unlooked:
        facts.append(f"{unlooked} not looked into since loading")
    return (text + "; ".join(facts) + ".")[:_LINE_CHARS]


def _respawn_line(respawn: Mapping[str, Any] | None, spawn: Any, now: float) -> str:
    world_spawn = "the world spawn"
    if isinstance(spawn, Mapping) and block_pos(spawn.get("pos")) is not None:
        world_spawn += f" {_xyz(spawn['pos'])} in {_dim(spawn.get('dimension'))}"
    if respawn is None:
        return f"- Respawn: the Body has seen no bed or anchor set as your respawn point; dying wakes you at {world_spawn}."
    if respawn.get("unknown"):
        return (f"- Respawn: {_ago(respawn.get('t'), now)} the game said your respawn point was set, but the Body did "
                f"not see at which bed or anchor.")
    if respawn.get("lost"):
        was = respawn.get("was") if isinstance(respawn.get("was"), Mapping) else {}
        where = f" at {_xyz(was['pos'])}" if block_pos(was.get("pos")) is not None else ""
        return (f"- Respawn: {_ago(respawn.get('t'), now)} the game said your bed or anchor{where} was missing or "
                f"blocked; dying wakes you at {world_spawn}.")
    line = (f"- Respawn: the {respawn.get('block') or 'bed'} at {_xyz(respawn['pos'])} in "
            f"{_dim(respawn['dimension'])}, set {_ago(respawn.get('t'), now)}")
    gone = respawn.get("gone")
    if isinstance(gone, Mapping):
        return (f"{line}; it is no longer there ({gone.get('reason')}, {_ago(gone.get('t'), now)}): unless you "
                f"sleep in another bed, dying wakes you at {world_spawn}.")
    return line + "; dying wakes you there."


def waking_notice(profile: Mapping[str, Any], world: Mapping[str, Any], *, here: Mapping[str, Any],
                  died: tuple[Any, Any] | None = None, now: float | None = None) -> dict[str, str]:
    """What the Agent is told on waking after a death (vision-and-home-20261028.md §7.7, death notice).

    Which point the Body woke at, by the game's own words in the record; the home and the way there;
    the death place and how far it is; what the containers at home held when last looked into.
    """
    now = time.time() if now is None else now
    position = (dimension_id(here.get("dimension")), block_pos(here.get("position", here.get("pos"))))
    portals = [link for link in world.get("portals") or () if isinstance(link, Mapping)]
    respawn = world.get("respawn") if isinstance(world.get("respawn"), Mapping) else None
    notice = {"respawn_point": _woke_line(respawn, world.get("world_spawn"), now)}
    home = profile.get("home") if isinstance(profile.get("home"), Mapping) else None
    if home is None or block_pos(home.get("pos")) is None:
        notice["home"] = "You have no home in this world yet."
    else:
        notice["home"] = _site_line("Home", home, position, portals).removeprefix("- ")
        notice["home_storage"] = _home_storage(home, world)
    if died is not None and block_pos(died[1]) is not None:
        dimension = dimension_id(died[0]) or OVERWORLD
        notice["death_place"] = (f"You died at {_xyz(died[1])} in {_dim(dimension)}; "
                                 f"{_way(dimension, block_pos(died[1]), position, portals)}.")
    return notice


def _woke_line(respawn: Mapping[str, Any] | None, spawn: Any, now: float) -> str:
    """The point the game woke the Body at, by its rule: a set bed or anchor still there, else the world spawn."""
    world_spawn = "the world spawn"
    if isinstance(spawn, Mapping) and block_pos(spawn.get("pos")) is not None:
        world_spawn += f" {_xyz(spawn['pos'])} in {_dim(spawn.get('dimension'))}"
    if respawn is None:
        return f"The Body has seen no bed or anchor set as your respawn point, so you woke at {world_spawn}."
    if respawn.get("unknown"):
        return (f"The game said your respawn point was set ({_ago(respawn.get('t'), now)}), but the Body did not "
                f"see at which bed or anchor; you woke there.")
    if respawn.get("lost"):
        was = respawn.get("was") if isinstance(respawn.get("was"), Mapping) else {}
        where = f" at {_xyz(was['pos'])}" if block_pos(was.get("pos")) is not None else ""
        return (f"The game said your bed or anchor{where} was missing or blocked "
                f"({_ago(respawn.get('t'), now)}), so you woke at {world_spawn}.")
    block = respawn.get("block") or "bed"
    gone = respawn.get("gone")
    if isinstance(gone, Mapping):
        return (f"Your respawn {block} at {_xyz(respawn['pos'])} is no longer there ({gone.get('reason')}, "
                f"{_ago(gone.get('t'), now)}), so you woke at {world_spawn}.")
    return (f"You woke at your respawn point: the {block} at {_xyz(respawn['pos'])} in "
            f"{_dim(respawn['dimension'])}, set {_ago(respawn.get('t'), now)}.")


def _home_storage(home: Mapping[str, Any], world: Mapping[str, Any]) -> str:
    """What the containers at home held, summed over them, as each was last looked into."""
    places = _places(world, ())
    for row in home.get("storage") or ():
        key = (_dimension(home), *row["pos"])
        places.setdefault(key, {"dimension": key[0], "pos": list(row["pos"])})["plan"] = row
    boxes = [place for place in places.values() if _belongs(place, home) and not place.get("gone")
             and (is_container(place.get("block") or "") or place.get("plan") or isinstance(place.get("items"), Mapping))]
    looked = [place for place in boxes if isinstance(place.get("items"), Mapping)]
    unseen = len(boxes) - len(looked)
    if not looked:
        return ("The Body has not looked into any container at home"
                + (f"; {unseen} there not looked into yet." if unseen else "."))
    total: dict[str, int] = {}
    for place in looked:
        for item, count in place["items"].items():
            total[item] = total.get(item, 0) + int(count)
    text = (f"The {len(looked)} container{'s' if len(looked) > 1 else ''} at home held, as last looked into: "
            f"{_contents(total, shown=STORAGE_KINDS_SHOWN)}")
    partial = sum(1 for place in looked if not place.get("complete"))
    if partial:
        text += f" ({partial} not read in full)"
    if unseen:
        text += f"; {unseen} more not looked into yet"
    return text + "."


def _belonging_line(item: Mapping[str, Any]) -> str:
    owner = str(item.get("owner") or "")
    whose = "shared" if owner.lower() == "shared" else f"{owner}'s"
    area = item.get("area")
    where = (_xyz(item["pos"]) if item.get("pos") is not None
             else f"the area {_xyz(area['from'])} to {_xyz(area['to'])}" if isinstance(area, Mapping) else "?")
    text = f"  - {where} in {_dim(dimension_id(item.get('dimension')) or OVERWORLD)}: {whose}"
    text += f", {item.get('said_by')} said ({item.get('said_at') or 'time not written'})"
    if item.get("note"):
        text += f"; {item['note']}"
    return (text + ".")[:_LINE_CHARS]


def _contents(items: Mapping[str, Any], shown: int = KINDS_SHOWN) -> str:
    if not items:
        return "empty"
    ranked = sorted(items.items(), key=lambda pair: (-int(pair[1]), pair[0]))
    text = ", ".join(f"{item} {count}" for item, count in ranked[:shown])
    if len(ranked) > shown:
        text += f" and {len(ranked) - shown} more kinds"
    return text


def _offset(here: list[int], there: list[int]) -> str:
    dx, dz = there[0] - here[0], there[2] - here[2]
    flat = round(math.hypot(dx, dz))
    rise = there[1] - here[1]
    if flat == 0:
        text = "right here"
    else:
        heading = _COMPASS[int((math.degrees(math.atan2(dx, -dz)) % 360 + 22.5) // 45) % 8]
        text = f"{flat} blocks {heading}"
    if rise:
        text += f", {abs(rise)} {'up' if rise > 0 else 'down'}"
    return text


def _dimension(site: Mapping[str, Any]) -> str:
    return dimension_id(site.get("dimension")) or OVERWORLD


def _dim(dimension: Any) -> str:
    return dimension_words(dimension)


def _xyz(pos: Any) -> str:
    where = block_pos(pos)
    return f"({where[0]}, {where[1]}, {where[2]})" if where else "(?)"


def _ago(t: Any, now: float) -> str:
    if not isinstance(t, (int, float)):
        return "at a time not recorded"
    seconds = max(0, int(now - t))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m ago"
    return f"{seconds // 86400}d ago"


__all__ = ["HOME_SECTION_CHARS", "NO_HOME", "furnace_section", "home_section", "waking_notice"]
