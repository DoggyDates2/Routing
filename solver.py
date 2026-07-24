"""
solver.py — OR-Tools route optimization for Doggy Dates
Handles simple trips (pickup-only, dropoff-only) and interleaved trips
(simultaneous dropoff/pickup with capacity constraints).
"""

from ortools.constraint_solver import routing_enums_pb2, pywrapcp


def build_trip_matrix(matrix, all_ids):
    """Build integer distance matrix for a subset of location IDs."""
    n = len(all_ids)
    dist = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j:
                val = matrix.get(all_ids[i], {}).get(all_ids[j], 9999)
                dist[i][j] = int(val * 100)
    return dist


def solve_simple_trip(matrix, stop_ids, start_id, end_id, time_limit=1):
    """
    Solve a simple TSP: visit all stops between a fixed start and end.
    Used for first leg (pickup only) and last leg (dropoff only).
    Returns: (ordered_stop_ids, total_minutes) or None
    """
    if not stop_ids:
        return [], 0.0

    all_ids = [start_id] + stop_ids + [end_id]
    dist = build_trip_matrix(matrix, all_ids)
    n = len(all_ids)

    manager = pywrapcp.RoutingIndexManager(n, 1, [0], [n - 1])
    routing = pywrapcp.RoutingModel(manager)

    def distance_cb(fi, ti):
        return dist[manager.IndexToNode(fi)][manager.IndexToNode(ti)]

    cb = routing.RegisterTransitCallback(distance_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(cb)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.FromSeconds(time_limit)

    solution = routing.SolveWithParameters(params)
    if not solution:
        return None

    route = []
    total = 0
    idx = routing.Start(0)
    while not routing.IsEnd(idx):
        node = manager.IndexToNode(idx)
        nxt = solution.Value(routing.NextVar(idx))
        total += dist[node][manager.IndexToNode(nxt)]
        route.append(all_ids[node])
        idx = nxt
    route.append(all_ids[manager.IndexToNode(idx)])
    return route, total / 100


def solve_interleaved_trip(matrix, dropoff_customers, pickup_customers,
                           start_id, end_id, capacity, initial_load,
                           time_limit=3,
                           front_drop_ids=None, front_pick_ids=None,
                           front_initial=0):
    """
    Solve an interleaved trip: drop off one group while picking up the next.
    
    Args:
        matrix: distance lookup dict
        dropoff_customers: list of (customer_id, dog_count) to drop off
        pickup_customers: list of (customer_id, dog_count) to pick up
        start_id: field ID (start)
        end_id: field ID (end)
        capacity: max dogs allowed in vehicle
        initial_load: total dogs on board when leaving field
        
    Returns: (route_with_loads, total_minutes) or None
        route_with_loads: list of (location_id, dogs_on_board, action)
    """
    if not dropoff_customers and not pickup_customers:
        return [], 0.0

    drop_ids = [c[0] for c in dropoff_customers]
    pick_ids = [c[0] for c in pickup_customers]
    all_ids = [start_id] + drop_ids + pick_ids + [end_id]
    n = len(all_ids)
    n_drop = len(drop_ids)

    dist = build_trip_matrix(matrix, all_ids)

    # Build demand array
    demands = [0] * n
    demands[0] = initial_load

    for i, (_, cnt) in enumerate(dropoff_customers):
        demands[1 + i] = -cnt
    for i, (_, cnt) in enumerate(pickup_customers):
        demands[1 + n_drop + i] = cnt

    manager = pywrapcp.RoutingIndexManager(n, 1, [0], [n - 1])
    routing = pywrapcp.RoutingModel(manager)

    def distance_cb(fi, ti):
        return dist[manager.IndexToNode(fi)][manager.IndexToNode(ti)]

    cb = routing.RegisterTransitCallback(distance_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(cb)

    def demand_cb(fi):
        return demands[manager.IndexToNode(fi)]

    dcb = routing.RegisterUnaryTransitCallback(demand_cb)
    routing.AddDimension(dcb, 0, capacity, True, 'Capacity')

    # Front seat: FRNT dogs ride up front and there is only ONE front seat.
    # A second capacity-1 dimension forces any FRNT drop-off to happen before
    # the next FRNT pickup. Only added when the caller passes front info.
    front_drop_ids = front_drop_ids or set()
    front_pick_ids = front_pick_ids or set()
    if front_initial or front_drop_ids or front_pick_ids:
        front_demands = [0] * n
        front_demands[0] = front_initial
        for i, (cid, _) in enumerate(dropoff_customers):
            if cid in front_drop_ids:
                front_demands[1 + i] = -1
        for i, (cid, _) in enumerate(pickup_customers):
            if cid in front_pick_ids:
                front_demands[1 + n_drop + i] = 1

        def front_cb(fi):
            return front_demands[manager.IndexToNode(fi)]

        fcb = routing.RegisterUnaryTransitCallback(front_cb)
        routing.AddDimension(fcb, 0, 1, True, 'FrontSeat')

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.FromSeconds(time_limit)

    solution = routing.SolveWithParameters(params)
    if not solution:
        return None

    route = []
    total = 0
    load = 0
    idx = routing.Start(0)
    while not routing.IsEnd(idx):
        node = manager.IndexToNode(idx)
        load += demands[node]
        nxt = solution.Value(routing.NextVar(idx))
        total += dist[node][manager.IndexToNode(nxt)]

        if node == 0:
            action = 'LEAVE FIELD'
        elif node <= n_drop:
            action = 'DROP OFF'
        else:
            action = 'PICK UP'
        route.append((all_ids[node], load, action))
        idx = nxt

    node = manager.IndexToNode(idx)
    load += demands[node]
    route.append((all_ids[node], load, 'ARRIVE FIELD'))
    return route, total / 100
