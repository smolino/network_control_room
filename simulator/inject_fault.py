"""Triggers one optical corridor fault on demand - for demos and for testing
the root-cause engine against a known ground truth. Same alarms as the
simulator's own random faults (see optical_faults.py), sent the same way.

Run inside the simulator container, e.g.:

    docker compose exec simulator python inject_fault.py cable_cut --span 4
    docker compose exec simulator python inject_fault.py fiber_cut --span 2 --direction ZA
    docker compose exec simulator python inject_fault.py amp_failure --ila a-0004-MAD01-BIO01 --direction AZ
    docker compose exec simulator python inject_fault.py trunk_failure --path OCH-1560.61-AZ
    docker compose exec simulator python inject_fault.py patch_cord --circuit 3 --side A
    docker compose exec simulator python inject_fault.py random --duration 60

--corridor picks a corridor by name (default: the first one). The repair
(all clears) follows after --duration seconds (default 180); --no-repair
leaves the fault in place.
"""

import argparse
import asyncio
import logging

import optical_faults as of
import trap_simulator as ts


def build(args, topo: of.Topology) -> of.Scenario:
    if args.kind == "cable_cut":
        return of.cable_cut(topo, args.span)
    if args.kind == "fiber_cut":
        return of.fiber_cut(topo, args.span, args.direction)
    if args.kind == "amp_failure":
        return of.amp_failure(topo, args.ila, args.direction)
    if args.kind == "trunk_failure":
        return of.trunk_failure(topo, args.path)
    if args.kind == "patch_cord":
        return of.patch_cord(topo, args.circuit, args.side)
    return of.random_scenario(topo)


async def main(args) -> None:
    primaries, customers, adjacency, bundles = ts.fetch_fleet_from_backend()
    corridors, devices = ts.fetch_corridors_from_backend()
    if not corridors:
        raise SystemExit("no optical corridor in the backend - import one first (scripts/import-corridor.py)")
    corridor = next((c for c in corridors if c["name"] == args.corridor), None) if args.corridor else corridors[0]
    if corridor is None:
        raise SystemExit(f"no corridor named {args.corridor!r}; have {[c['name'] for c in corridors]}")
    scenario = build(args, of.Topology(corridor, devices, bundles))
    for e in scenario.raise_events:
        print(f"  +{e.delay:5.1f}s {e.who:22} {e.oid.rsplit('.', 2)[-2] + '.' + e.oid.rsplit('.', 1)[-1]:8} {e.interface or '':22} {e.condition or ''}")
    engine = ts.SnmpEngine()
    target = ts.UdpTransportTarget((ts.TRAP_HOST, ts.TRAP_PORT))
    if args.no_repair:
        logging.info("GROUND TRUTH [%s] %s (no repair)", scenario.corridor, scenario.summary)
        await ts.play_events(engine, target, scenario.raise_events)
    else:
        await ts.run_optical_scenario(engine, target, scenario, args.duration)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("kind", choices=[*of.SCENARIOS, "random"])
    p.add_argument("--corridor")
    p.add_argument("--span", type=int, default=1)
    p.add_argument("--direction", choices=["AZ", "ZA"], default="AZ")
    p.add_argument("--ila")
    p.add_argument("--path")
    p.add_argument("--circuit", type=int, default=1)
    p.add_argument("--side", choices=["A", "Z"], default="A")
    p.add_argument("--duration", type=float, default=180)
    p.add_argument("--no-repair", action="store_true")
    asyncio.run(main(p.parse_args()))
