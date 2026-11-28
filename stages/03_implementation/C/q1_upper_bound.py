"""Independent cardinality upper bound for a closed whole-task Q1 instance.

This enumerates completion subsets, not routes, times, or crane motions.
It does not import the schedule generator or any A audit routines.
"""

from collections import Counter, defaultdict
from itertools import combinations


def _ordered_previous(groups, order_key, field):
    """Rebuild each sequence and verify that the recorded graph agrees."""
    previous = {}
    for group in groups.values():
        order_values = [order_key(t) for t in group]
        if len(set(order_values)) != len(order_values):
            raise ValueError(f"Ambiguous frozen {field} order")
        earlier = None
        for task in sorted(group, key=order_key):
            if task.get(field) != earlier:
                raise ValueError(f"Frozen {field} differs from the reconstructed sequence")
            previous[task["id"]] = earlier
            earlier = task["id"]
    return previous


def audit_bound(case: dict) -> dict:
    """Check necessary conditions and return the retained completion subsets' statistics.

    The exchange model is closed. Every accepted EF task uses one whole transport
    and ends in a documented destination process. It cannot empty a holder via
    an unfinished transport spanning the horizon or an unapproved handoff.
    """
    all_tasks = {t["id"]: t for t in case["tasks"]}
    if len(all_tasks) != len(case["tasks"]):
        raise ValueError("Frozen roster contains duplicate IDs")
    if case.get("locked_trajectories") or case.get("initial_occupancy"):
        raise ValueError("Reconstruct initial handoffs before applying the closed-instance bound")

    def confirmed(task):
        return (task["kind"] == "ef" and task["scope"] == "ef_crane"
                and task["scope_status"] == "confirmed")

    if any(confirmed(t) != (t["required_in_ef_scope"] is True) for t in all_tasks.values()):
        raise ValueError("Frozen scope fields disagree")
    order = tuple(tid for tid, t in all_tasks.items() if confirmed(t))
    if len(order) > 20:
        raise ValueError("Exact closed-instance enumeration is limited to 20 confirmed tasks")
    omitted = set(all_tasks) - set(order)
    if any(not all_tasks[tid]["blocks"] for tid in omitted):
        raise ValueError("The roster includes an external release candidate without a blocking decision")

    aliases = {tid: f"T{i:02d}" for i, tid in enumerate(order, 1)}
    capacity = case["capacity"]
    authorized_pickups = defaultdict(set)
    blocked_pickups = defaultdict(set)
    stock_groups = defaultdict(list)
    casters = defaultdict(list)
    converters = defaultdict(list)
    for task in all_tasks.values():
        pid = task.get("predecessor")
        if pid is None:
            continue
        if pid not in all_tasks:
            raise ValueError("Referenced physical predecessor is missing")
        prior = all_tasks[pid]
        same_holder = prior["ladle"] and prior["ladle"] == task["ladle"]
        same_station = prior["destination"] and prior["destination"] == task["origin"]
        if confirmed(task) and (not same_holder or not same_station):
            raise ValueError("A confirmed physical predecessor fails the same-ladle, same-position test")
        if same_holder and same_station:
            pickup_group = authorized_pickups if confirmed(task) and not task["blocks"] else blocked_pickups
            pickup_group[pid].add(task["id"])

    resources = {}
    for tid in order:
        task = all_tasks[tid]
        service = task.get("service")
        if not task["ladle"] or not service:
            raise ValueError("Confirmed EF task lacks ladle identity or terminal service")
        kind = service["kind"]
        if kind not in {"refine", "cast", "slag"}:
            raise ValueError("Terminal service lacks a documented stock model")
        resource = f"{'return' if kind == 'cast' else kind}/{task['destination']}"
        if capacity.get(resource, 0) <= 0:
            raise ValueError("Terminal resources require an explicit positive capacity")
        resources[tid] = resource
        stock_groups[resource].append(tid)
        if kind == "cast":
            if not task["campaign"] or task["order"] is None or service["duration_s"] <= 0:
                raise ValueError("Casting turnover requires an identified sequence and positive service duration")
            casters[task["destination"], task["campaign"]].append(task)
        if task["origin"].startswith("BOF") and task["predecessor"] is None:
            if task["rho_s"] is None or task["order"] is None:
                raise ValueError("Converter FIFO requires a release and order")
            converters[task["origin"]].append(task)

    previous_cast = _ordered_previous(casters, lambda t: t["order"], "previous_cast")
    previous_bof = _ordered_previous(converters, lambda t: (t["rho_s"], t["order"]), "previous_bof")
    dependencies = {}
    for tid in order:
        task = all_tasks[tid]
        deps = {task["predecessor"], task["reuse_predecessor"],
                previous_cast.get(tid), previous_bof.get(tid)} - {None}
        if not deps <= all_tasks.keys():
            raise ValueError("Frozen reuse or workflow dependency is missing")
        dependencies[tid] = deps
    denied = {tid for tid in order if all_tasks[tid]["blocks"] or dependencies[tid] & omitted}

    def stock_ok(selected, prefixes):
        # A served transport creates one unit of stock. The matching physical
        # pickup moves that same ladle away, even if it occurs at the horizon.
        for resource, members in stock_groups.items():
            if resource.startswith(prefixes):
                occupied = sum(tid in selected and authorized_pickups[tid].isdisjoint(selected)
                               for tid in members)
                if occupied > capacity[resource]:
                    return False
        return True

    def cast_turn_ok(selected):
        # At unit-capacity return slots the former caster's output must leave
        # before the latter cast ends and enters the same return slot.
        for later, earlier in previous_cast.items():
            if earlier is not None and later in selected:
                resource = resources[earlier]
                if capacity[resource] == 1 and authorized_pickups[earlier].isdisjoint(selected):
                    return False
        return True

    gates = {label: {"subsets": 0, "upper": 0} for label in
             ("external", "slag", "dependencies", "caster_turnover", "terminal_stock")}
    possible = set()
    maxima = []
    for count in range(len(order) + 1):
        have_maximum_at_count = False
        for subset in combinations(order, count):
            selected = set(subset)
            if selected & denied:
                continue
            gates["external"]["subsets"] += 1
            gates["external"]["upper"] = count
            if not stock_ok(selected, "slag/"):
                continue
            gates["slag"]["subsets"] += 1
            gates["slag"]["upper"] = count
            if any(not dependencies[tid] <= selected for tid in selected):
                continue
            gates["dependencies"]["subsets"] += 1
            gates["dependencies"]["upper"] = count
            if not cast_turn_ok(selected):
                continue
            gates["caster_turnover"]["subsets"] += 1
            gates["caster_turnover"]["upper"] = count
            if not stock_ok(selected, ("refine/", "return/")):
                continue
            gates["terminal_stock"]["subsets"] += 1
            gates["terminal_stock"]["upper"] = count
            possible.update(selected)
            if not have_maximum_at_count:
                maxima = []
                have_maximum_at_count = True
            maxima.append(list(subset))

    def in_roster_order(items):
        return [tid for tid in all_tasks if tid in items]

    return {
        "tested_subsets": 2 ** len(order),
        "gates": gates,
        "maximum_subsets": maxima,
        "never_feasible_in_relaxation": [tid for tid in order if tid not in possible],
        "task_aliases": aliases,
        "roster": [
            {
                "id": tid, "alias": aliases[tid], "ladle": all_tasks[tid]["ladle"],
                "origin": all_tasks[tid]["origin"], "destination": all_tasks[tid]["destination"],
                "predecessors": in_roster_order(dependencies[tid]), "terminal_resource": resources[tid],
                "scope_blocks": all_tasks[tid]["blocks"], "externally_blocked": tid in denied,
            }
            for tid in order
        ],
        "release_edges": [
            {
                "completed_task": tid, "resource": resources[tid],
                "eligible_pickups": in_roster_order(authorized_pickups[tid]),
                "blocked_external_candidates": in_roster_order(blocked_pickups[tid] & omitted),
            }
            for tid in order
        ],
        "boundary_task_counts": dict(Counter(all_tasks[tid]["scope_status"] for tid in all_tasks if tid in omitted)),
    }
