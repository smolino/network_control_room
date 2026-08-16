"""Interface-bundle (Port-channel/EtherChannel) derivation and state
tracking for BGP peering links.

Both this module and the simulator (simulator/trap_simulator.py -
build_bgp_bundles, a separate process that can't import this package)
independently derive identical bundle/member interface names from the same
topology data (peers sorted by mgmt_ip) - the same "agree by construction,
not by shared lookup table" pattern the codebase already uses for the
plain (pre-bundle) FortyGigE/GigabitEthernet numbering.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import BundleStatus, Interface, InterfaceBundle, Router

BUNDLE_MEMBER_COUNT = 2
MEMBER_BANDWIDTH_MBPS = 40000

# IS-IS process tag used in simulated config backups (router isis BACKBONE) -
# a single level-2-only process across the whole backbone mesh, since this
# is one flat area, not a multi-area design.
ISIS_PROCESS_TAG = "BACKBONE"


def isis_net(router: Router) -> str:
    """Simplified OSI NET (Network Entity Title) for this router's IS-IS
    process: "49" (private/demo area prefix) + "0001" area + a system ID
    derived from the router's own id (zero-padded into the conventional
    three 4-hex-digit groups) + "00" NSEL. Good enough for a demo IS-IS
    config block, not meant to model real address planning."""
    return f"49.0001.0000.0000.{router.id:04x}.00"


def bundle_and_member_names(peer_index: int) -> tuple[str, list[str]]:
    """peer_index is this router's peer's position in its own
    mgmt_ip-sorted peer list (same index build_bgp_interfaces/
    _bgp_peers_sorted already use for plain interface numbering)."""
    base = peer_index * BUNDLE_MEMBER_COUNT
    members = [f"FortyGigE0/0/{base + m}" for m in range(BUNDLE_MEMBER_COUNT)]
    return f"Port-channel{peer_index + 1}", members


def build_peer_index(router_id_to_peer_ids: dict[int, list[int]]) -> dict[int, dict[int, int]]:
    """router_id -> {peer_id: index}, peers ordered by mgmt_ip is the
    caller's job (this just assigns 0..N-1 in whatever order it receives)."""
    return {router_id: {peer_id: i for i, peer_id in enumerate(peer_ids)} for router_id, peer_ids in router_id_to_peer_ids.items()}


def ensure_bundles_for_peering(
    db: Session,
    peering_id: int,
    router: Router,
    peer_index: int,
) -> InterfaceBundle:
    """Idempotently creates (or returns the existing) InterfaceBundle + its
    member Interfaces for one side of a peering."""
    bundle_name, member_names = bundle_and_member_names(peer_index)

    bundle = (
        db.query(InterfaceBundle)
        .filter(InterfaceBundle.router_id == router.id, InterfaceBundle.name == bundle_name)
        .first()
    )
    if bundle is not None:
        return bundle

    bundle = InterfaceBundle(router_id=router.id, peering_id=peering_id, name=bundle_name, status=BundleStatus.UP)
    db.add(bundle)
    db.flush()

    for member_name in member_names:
        db.add(
            Interface(
                router_id=router.id,
                bundle_id=bundle.id,
                name=member_name,
                bandwidth_mbps=MEMBER_BANDWIDTH_MBPS,
                isis_adjacency_up=True,
            )
        )
    db.flush()
    return bundle


def recompute_bundle_status(bundle: InterfaceBundle) -> BundleStatus:
    return BundleStatus.UP if any(m.isis_adjacency_up for m in bundle.members) else BundleStatus.DOWN


def update_member_and_bundle(
    db: Session, router: Router, interface_name: str | None, adjacency_up: bool, now: datetime
) -> tuple[InterfaceBundle | None, bool]:
    """Flips one member Interface's IS-IS adjacency state and recomputes its
    parent bundle. Returns (bundle, status_changed) - bundle is None if
    interface_name doesn't name a known bundle member on this router (e.g.
    a customer-facing or not-yet-seeded interface)."""
    if not interface_name:
        return None, False

    iface = (
        db.query(Interface)
        .filter(Interface.router_id == router.id, Interface.name == interface_name)
        .first()
    )
    if iface is None:
        return None, False

    iface.isis_adjacency_up = adjacency_up
    iface.last_changed_at = now
    db.flush()

    bundle = iface.bundle
    old_status = bundle.status
    new_status = recompute_bundle_status(bundle)
    changed = old_status != new_status
    if changed:
        bundle.status = new_status
        bundle.last_changed_at = now
        db.flush()
    return bundle, changed
