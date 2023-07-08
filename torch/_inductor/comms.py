from collections import defaultdict
from typing import List
import os

import torch.fx as fx
from . import ir, scheduler
from .analysis import get_runtime_snode
from .dependencies import WeakDep
from .debug import printd


# Used to ensure that iterating over a set is deterministic
def tuple_sorted(x):
    return sorted(x, key=lambda x: x.get_name())


def sink_waits(result: List["scheduler.BaseSchedulerNode"]) -> List["scheduler.BaseSchedulerNode"]:
    """
    Greedily moves waits as late as possible (i.e. until we reach an use). Optimal in terms of
    communication overlap.
    """
    new_result = []
    cur_waits = set()
    for snode in result:
        if isinstance(snode.node, ir.Wait):
            cur_waits.add(snode)
        else:
            for wait in tuple_sorted(cur_waits):
                if snode in wait.node_users:
                    new_result.append(wait)
                    cur_waits.remove(wait)

            new_result.append(node)
    for node in tuple_sorted(cur_waits):
        new_result.append(node)
    return new_result


def raise_comms(result: List["scheduler.BaseSchedulerNode"]) -> List["scheduler.BaseSchedulerNode"]:
    """
    Greedily moves comms as early as possible (i.e. until we reach an input).
    Optimal in terms of communication overlap.
    """
    new_result = []
    cur_comms = []
    for snode in reversed(result):
        if isinstance(snode.node, ir.CollectiveKernel):
            cur_comms.append(snode)
        else:
            while len(cur_comms) > 0 and any([snode in comm.args for comm in cur_comms]):
                comm = cur_comms.pop(0)
                new_result.append(comm)
            new_result.append(snode)
    assert len(cur_comms) <= 1
    for snode in tuple_sorted(cur_comms):
        new_result.append(snode)
    result = new_result[::-1]
    return result


def get_ancestors(node):
    ancestors = set()
    cur_nodes = [node]
    while len(cur_nodes) > 0:
        new_nodes = []
        for node in cur_nodes:
            for inp in node.inverse_users:
                if inp not in ancestors:
                    ancestors.add(inp)
                    new_nodes.append(inp)
        cur_nodes = new_nodes
    return ancestors


def get_descendants(node):
    descendants = set()
    cur_nodes = [node]
    while len(cur_nodes) > 0:
        new_nodes = []
        for node in cur_nodes:
            for inp in node.node_users:
                if inp not in descendants:
                    descendants.add(inp)
                    new_nodes.append(inp)
        cur_nodes = new_nodes
    return descendants


def decide_global_ordering_comms(nodes: List["scheduler.BaseSchedulerNode"]):
    """
    Just enforces the ordering that's in the input graph.
    TODO: Come up with a better approach
    """
    comm_nodes = [n for n in nodes if isinstance(n.node, ir.CollectiveKernel)]
    for i in range(1, len(comm_nodes)):
        comm_nodes[i].add_mutation_dep(WeakDep(comm_nodes[i - 1].get_name()))


def dumb_reordering(nodes: List[fx.Node]) -> List[fx.Node]:
    """
    Sinks waits and raises comms. Does not try to reorder compute in order to
    maximize overlap.
    """
    nodes = [node for node in nodes if "fusion_meta" in node.meta]
    nodes = sink_waits(nodes)
    nodes = raise_comms(nodes)
    return [node.meta["fusion_meta"].snode for node in nodes]


def debug_print(s=""):

    if os.environ.get("INDUCTOR_COMM_DEBUG") == "1":
        printd(s)


def reorder_computation_for_overlap(snodes: List["scheduler.BaseSchedulerNode"]) -> List["scheduler.BaseSchedulerNode"]:
    """
    Decides a global ordering of all nodes. Assumes that we already have a global ordering of communication nodes.

    Overall strategy is:
    Priority 1. Given that we've currently scheduled comm N, we now schedule all compute nodes that are required for comm N + 1, but do not depend on comm N.
    Priority 2. Now, if all those compute nodes are sufficient to overlap comm N, we're done. Otherwise, we now need to look elsewhere to find compute that overlaps with comm N. We prioritize compute nodes that are needed sooner.
    Priority 3. Now, we schedule the compute nodes dependent on comm N and required for comm N + 1.

    Repeat.
    """
    comm_nodes = []
    for snode in snodes:
        if isinstance(snode.node, ir.CollectiveKernel):
            comm_nodes.append(snode)

    if len(comm_nodes) == 0:
        return snodes

    comm_ancestors = {node: get_ancestors(node) for node in comm_nodes}
    comm_descendants = {node: get_descendants(node) for node in comm_nodes}

    indeg = {k: 0 for k in snodes}
    for snode in snodes:
        for user in snode.node_users:
            if user in indeg:
                indeg[user] += 1
    breakpoint()
    free_nodes = set([node for node in snodes if indeg[node] == 0])

    result = []
    unused_nodes = set(snodes)

    def add_node(node):
        assert node in unused_nodes
        assert node in free_nodes
        debug_print(f"adding {node}")
        free_nodes.remove(node)
        unused_nodes.remove(node)
        result.append(node)
        for user in node.node_users:
            if user in indeg:
                indeg[user] -= 1
                if indeg[user] == 0:
                    free_nodes.add(user)

    def add_all_nodes(snodes):
        """
        Schedules all snodes in an arbitrary topologically valid order.
        """
        all_nodes = set(snodes)
        assert all([node in unused_nodes for node in all_nodes])
        while len(all_nodes) > 0:
            progress = False
            for node in tuple_sorted(all_nodes):
                if node in free_nodes:
                    add_node(node)
                    all_nodes.remove(node)
                    progress = True
            if not progress:
                breakpoint()
                print()

    add_all_nodes(list(comm_ancestors[comm_nodes[0]]) + [comm_nodes[0]])

    rolled_over_compute = 0
    for idx in range(1, len(comm_ancestors)):
        is_comm_blocking = (
            len(comm_descendants[comm_nodes[idx - 1]] & comm_ancestors[comm_nodes[idx]])
            > 0
        )
        debug_print(
            f"Start {comm_nodes[idx - 1]} -> {comm_nodes[idx]} ({is_comm_blocking}, {rolled_over_compute if not is_comm_blocking else ''})"
        )
        debug_print("Priority 1")
        # Priority 1: Nodes that are required for the next comm, but are not dependent on the current comm
        priority1 = unused_nodes & (
            comm_ancestors[comm_nodes[idx]] - comm_descendants[comm_nodes[idx - 1]]
        )
        total_cost = rolled_over_compute + sum(
            [get_runtime_snode(node) for node in priority1]
        )
        comm_cost = get_runtime_snode(comm_nodes[idx - 1])
        add_all_nodes(tuple_sorted(priority1))

        debug_print("Priority 2")
        # Priority 2: These are nodes that we're only allocating here for overlap reasons. We prioritize nodes that are needed sooner. This component is the main area with nontrivial decisions.
        group1_cost = total_cost
        if total_cost >= comm_cost:
            pass
        else:
            overlappable_nodes = tuple_sorted(
                free_nodes - comm_descendants[comm_nodes[idx - 1]]
            )

            def earliest_comm_descendant(node):
                for idx in range(len(comm_nodes)):
                    if node in comm_ancestors[comm_nodes[idx]]:
                        return idx
                return len(comm_nodes)

            overlappable_nodes = sorted(
                overlappable_nodes, key=earliest_comm_descendant
            )

            for snode in overlappable_nodes:
                if total_cost >= comm_cost:
                    break
                if not isinstance(
                    snode.node, ir.CollectiveKernel
                ):
                    runtime_cost = get_runtime_snode(snode)
                    # If we're not able to leverage more than half of this
                    # node's compute to overlap, we skip it.
                    # TODO: Smarter heuristics for packing the cost here
                    if (comm_cost - total_cost) <= runtime_cost / 2:
                        continue
                    add_node(snode)
                    total_cost += get_runtime_snode(snode)
        rollable_compute = total_cost - group1_cost
        # The idea here is that if there are no compute nodes in priority 3, we
        # can roll over the compute nodes in priority 2 to the next comm, since
        # they're not required to finish before the next comm starts

        # We can extend our ability to roll over compute if we leverage low
        # priority streams here, since that would lift us from the requirement
        # to finish priority 2 compute before the next comm starts.
        if is_comm_blocking:
            rolled_over_compute = 0
        else:
            rolled_over_compute = rollable_compute
        debug_print(f"{comm_nodes[idx-1]} overlap: {total_cost}/{comm_cost}")
        debug_print("priority 3")
        # Priority 3: Now, we schedule everything else required for comm N + 1, which also includes compute nodes dependent on comm N.
        priority3 = unused_nodes & comm_ancestors[comm_nodes[idx]]
        add_all_nodes(list(priority3) + [comm_nodes[idx]])
        debug_print()

    add_all_nodes(unused_nodes)

    result = sink_waits(result)
    result = raise_comms(result)

    printd(result)
    return result